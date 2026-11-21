"""Select sqlglot tests from checked-in baseline impact maps."""

from __future__ import annotations

import json
import os
import pickle
import re
import sys
from pathlib import Path

try:
    import orjson
except ImportError:
    orjson = None


DATA = Path(os.environ.get("BLAST_RADIUS_DATA", "/data"))
MAP_PATH = Path(os.environ.get("BLAST_RADIUS_MAP", Path(__file__).with_name("impact_map.json")))
BASE_COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
DIFF_PATH_RE = re.compile(r"^diff --git a/(.*?) b/(.*?)$")
SYMBOL_FALLBACK_MAX_LINE_TESTS = 1
SYMBOL_FALLBACK_MAX_TESTS = 32
OPTIMIZER_CONSUMER_MAX_TESTS = 40
DIRECT_LINE_MAX_TESTS = 24
PYTHON_COMMENT_DIRECTIVE_RE = re.compile(
    r"^#\s*(?:type:|noqa|pragma:|coverage:|fmt:|isort:|pytest:|encoding[:=])", re.IGNORECASE
)


def parse_patch(text: str) -> list[dict]:
    """Return changed paths and baseline line ranges from a unified diff."""
    files: list[dict] = []
    current: dict | None = None
    old_line = new_line = 0
    for line in text.splitlines():
        match = DIFF_PATH_RE.match(line)
        if match:
            old_path, new_path = match.groups()
            current = {"old_path": old_path, "new_path": new_path, "hunks": []}
            files.append(current)
            continue
        if current is None:
            continue
        if not current["hunks"] and line.startswith("--- "):
            path = line[4:].split("\t", 1)[0]
            if path != "/dev/null":
                current["old_path"] = path.removeprefix("a/")
            continue
        if not current["hunks"] and line.startswith("+++ "):
            path = line[4:].split("\t", 1)[0]
            if path != "/dev/null":
                current["new_path"] = path.removeprefix("b/")
            continue
        hunk = HUNK_RE.match(line)
        if hunk:
            old_line = int(hunk.group(1))
            old_count = int(hunk.group(2) or 1)
            new_line = int(hunk.group(3))
            current["hunks"].append({"old_start": old_line, "old_count": old_count, "new_start": new_line})
            continue
        if not current["hunks"]:
            continue
        if line.startswith(" "):
            old_line += 1
            new_line += 1
        elif line.startswith("-") and not line.startswith("---"):
            current["hunks"][-1].setdefault("old_text", []).append(line[1:])
            current["hunks"][-1]["old_lines"] = current["hunks"][-1].get("old_lines", []) + [old_line]
            old_line += 1
        elif line.startswith("+") and not line.startswith("+++"):
            current["hunks"][-1]["added"] = True
            current["hunks"][-1].setdefault("new_text", []).append(line[1:])
            indent = len(line[1:]) - len(line[1:].lstrip(" \t"))
            current["hunks"][-1]["added_indent"] = max(
                current["hunks"][-1].get("added_indent", 0), indent
            )
            current["hunks"][-1].setdefault("added_points", []).append((old_line, indent))
            new_line += 1
    return files


def _line_tests(line_map: dict, path: str, line: int) -> set[int]:
    result: set[int] = set()
    for start, end, tests in line_map.get(path, []):
        if start <= line <= end:
            result.update(tests)
            break
    return result


def _ranked_candidates(
    candidates: set[int],
    line_coverage: dict[int, int],
    durations: dict[int, int],
    test_files: dict[int, str],
    limit: int,
) -> set[int]:
    if len(candidates) <= limit:
        return candidates

    ordered = sorted(
        candidates,
        key=lambda number: (-line_coverage[number], durations[number], number),
    )
    selected: list[int] = []
    seen_files: set[str] = set()
    for number in ordered:
        test_file = test_files[number]
        if test_file not in seen_files:
            selected.append(number)
            seen_files.add(test_file)
            if len(selected) == limit:
                return set(selected)

    for number in ordered:
        if number not in selected:
            selected.append(number)
            if len(selected) == limit:
                break
    return set(selected)


def _ranked_symbol_tests(
    symbols: dict,
    path: str,
    line: int,
    line_map: dict,
    durations: dict[int, int],
    test_files: dict[int, str],
) -> set[int]:
    matches = [
        (end - start, start, end, tests)
        for start, end, tests in symbols.get(path, [])
        if start <= line <= end
    ]
    if not matches:
        return set()

    smallest = min(span for span, _, _, _ in matches)
    matches = [match for match in matches if match[0] == smallest]
    start = min(match[1] for match in matches)
    end = max(match[2] for match in matches)
    candidates = {number for _, _, _, tests in matches for number in tests}
    line_coverage = {number: 0 for number in candidates}
    for line_start, line_end, tests in line_map.get(path, []):
        overlap = max(0, min(end, line_end) - max(start, line_start) + 1)
        if overlap:
            for number in tests:
                if number in line_coverage:
                    line_coverage[number] += overlap

    return _ranked_candidates(
        candidates,
        line_coverage,
        durations,
        test_files,
        SYMBOL_FALLBACK_MAX_TESTS,
    )


def _inside_function(function_ranges: dict, path: str, line: int, added_indent: int = 0) -> bool:
    ranges = function_ranges.get(path, [])
    return any(
        start <= line <= end or (added_indent > 0 and start == line + 1)
        for start, end in ranges
    )


def _path_tests(path_map: dict, path: str) -> set[int]:
    return set(path_map.get(path, []))


def _ranked_path_tests(
    test_numbers: list[int],
    path: str,
    line_map: dict,
    durations: dict[int, int],
    limit: int,
    test_files: dict[int, str],
) -> set[int]:
    candidates = set(test_numbers)
    if len(candidates) <= limit:
        return candidates

    line_coverage = {number: 0 for number in candidates}
    for start, end, tests in line_map.get(path, []):
        overlap = end - start + 1
        for number in tests:
            if number in line_coverage:
                line_coverage[number] += overlap

    return _ranked_candidates(candidates, line_coverage, durations, test_files, limit)


def _normalize_patch_path(path: str) -> str:
    return path.replace("\\", "/").removeprefix("./")


def _has_executable_change(hunks: list[dict]) -> bool:
    """Treat ordinary Python comments as non-runtime changes, conservatively."""
    changed_lines = [
        line
        for hunk in hunks
        for line in hunk.get("old_text", []) + hunk.get("new_text", [])
    ]
    if not changed_lines:
        return True

    for line in changed_lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#") and not PYTHON_COMMENT_DIRECTIVE_RE.match(stripped):
            continue
        return True
    return False


def _is_comment_only_sqlglot_patch(files: list[dict]) -> bool:
    if not files:
        return False
    for file in files:
        paths = {
            _normalize_patch_path(file.get("old_path", "")),
            _normalize_patch_path(file.get("new_path", "")),
        } - {"", "/dev/null"}
        if not paths or not all(
            path.startswith("sqlglot/") and path.endswith(".py") for path in paths
        ):
            return False
        if _has_executable_change(file.get("hunks", [])):
            return False
    return True


def _read_json(path: Path):
    data = path.read_bytes()
    return orjson.loads(data) if orjson is not None else json.loads(data)


def _read_impact_map(path: Path):
    data = path.read_bytes()
    if path.suffix == ".pickle":
        return pickle.loads(data)
    return orjson.loads(data) if orjson is not None else json.loads(data)


def select_tests(
    files: list[dict],
    tests: list[dict],
    impact: dict,
    durations: dict[int, int] | None = None,
    test_files: dict[int, str] | None = None,
) -> set[int]:
    # A patch made entirely of ordinary comments in sqlglot source cannot
    # change runtime behavior, so it needs no test workers.
    if _is_comment_only_sqlglot_patch(files):
        return set()

    selected: set[int] = set()
    line_map = impact.get("line_tests", {})
    symbol_map = impact.get("symbol_tests", {})
    function_map = impact.get("function_ranges", {})
    file_map = impact.get("file_tests", {})
    import_map = impact.get("import_tests", {})
    test_file_map = impact.get("test_file_tests", {})
    fixture_map = impact.get("fixture_tests", {})
    runtime_fixture_map = impact.get("runtime_fixture_tests")
    dialect_fixture_map = impact.get("dialect_fixture_tests", {})
    suites = impact.get("suite_tests", {})
    api_consumers = impact.get("api_consumer_tests", {})
    all_tests = {test["number"] for test in tests}
    if durations is None:
        durations = {test["number"]: round(test["seconds"] * 1000) for test in tests}
    if test_files is None:
        test_files = {test["number"]: test["file"] for test in tests}

    for changed in files:
        old_path = _normalize_patch_path(changed.get("old_path", ""))
        new_path = _normalize_patch_path(changed.get("new_path", ""))
        relevant_paths = {path for path in (old_path, new_path) if path and path != "/dev/null"}
        for path in relevant_paths:
            if path.startswith("tests/fixtures/"):
                fixture_key = Path(path).as_posix()
                fixture_tests = _path_tests(
                    runtime_fixture_map if runtime_fixture_map is not None else fixture_map,
                    fixture_key,
                )
                fixture_tests.update(_path_tests(fixture_map, fixture_key))
                if fixture_tests:
                    selected.update(fixture_tests)
                else:
                    # No baseline consumer is known, so preserve recall with
                    # a full fallback rather than guessing from the filename.
                    selected.update(all_tests)
                continue
            if path.startswith("tests/") and path.endswith(".py"):
                selected.update(test_file_map.get(path, []))
                if path.endswith("conftest.py") or path == "tests/__init__.py":
                    selected.update(all_tests)
                elif path not in test_file_map:
                    selected.update(all_tests)
                continue
            if path.startswith("tests/"):
                selected.update(all_tests)
                continue
            if not path.startswith("sqlglot/") or not path.endswith(".py"):
                if path.startswith("sqlglot/"):
                    selected.update(all_tests)
                elif path in {"setup.py", "setup.cfg", "pyproject.toml", "setup_helpers.py"} or path.startswith("sqlglotc/"):
                    selected.update(suites.get("sqlglotc", []))
                continue

            # File-level execution captures imports and module initialization.
            file_hits = _path_tests(file_map, path)
            if path.startswith("sqlglot/optimizer/"):
                if Path(path).name == "eliminate_joins.py":
                    selected.update(
                        _ranked_path_tests(
                            api_consumers.get("optimizer_joins", []),
                            path,
                            line_map,
                            durations,
                            OPTIMIZER_CONSUMER_MAX_TESTS,
                            test_files,
                        )
                    )
                    selected.update(
                        _ranked_path_tests(
                            api_consumers.get("executor_large_datasets", []),
                            path,
                            line_map,
                            durations,
                            OPTIMIZER_CONSUMER_MAX_TESTS,
                            test_files,
                        )
                    )
                    selected.update(fixture_map.get("tests/fixtures/optimizer/tpc-ds/tpc-ds.sql", []))
                    selected.update(fixture_map.get("tests/fixtures/optimizer/tpc-h/tpc-h.sql", []))
                elif "subquer" in Path(path).name.lower():
                    selected.update(
                        _ranked_path_tests(
                            api_consumers.get("optimizer_subqueries", []),
                            path,
                            line_map,
                            durations,
                            OPTIMIZER_CONSUMER_MAX_TESTS,
                            test_files,
                        )
                    )
            if path.startswith(("sqlglot/typing/", "sqlglot/expressions/")):
                selected.update(suites.get("typing", []))
            if not file_hits and path.startswith("sqlglot/"):
                selected.update(all_tests)
            hunk_tests: set[int] = set()
            has_uncovered_hunk = False
            for hunk in changed.get("hunks", []):
                hunk_tests_for_hunk: set[int] = set()
                old_lines = hunk.get("old_lines", [])
                if old_lines:
                    for line in old_lines:
                        line_hits = _line_tests(line_map, path, line)
                        if len(line_hits) > DIRECT_LINE_MAX_TESTS:
                            symbol_hits = _ranked_symbol_tests(
                                symbol_map, path, line, line_map, durations, test_files
                            )
                            if symbol_hits:
                                line_hits.intersection_update(symbol_hits)
                        hunk_tests_for_hunk.update(line_hits)
                        if len(line_hits) < SYMBOL_FALLBACK_MAX_LINE_TESTS:
                            # A thin line witness set may miss other callers
                            # exercising branches in the same changed function.
                            hunk_tests_for_hunk.update(
                                _ranked_symbol_tests(
                                    symbol_map,
                                    path,
                                    line,
                                    line_map,
                                    durations,
                                    test_files,
                                )
                            )
                else:
                    # Pure additions have no baseline line coverage. Anchor to
                    # the nearest parent lines and the enclosing baseline symbol.
                    anchor = max(1, hunk["old_start"])
                    hunk_tests_for_hunk.update(
                        _ranked_symbol_tests(
                            symbol_map,
                            path,
                            anchor,
                            line_map,
                            durations,
                            test_files,
                        )
                    )
                    for line in range(max(1, anchor - 2), anchor + 3):
                        hunk_tests_for_hunk.update(_line_tests(line_map, path, line))
                if not hunk_tests_for_hunk:
                    has_uncovered_hunk = True
                hunk_tests.update(hunk_tests_for_hunk)
            selected.update(hunk_tests)
            if path.startswith("sqlglot/optimizer/") and len(hunk_tests) <= 10:
                selected.update(
                    _ranked_path_tests(
                        api_consumers.get("optimizer_pipeline", []),
                        path,
                        line_map,
                        durations,
                        OPTIMIZER_CONSUMER_MAX_TESTS,
                        test_files,
                    )
                )
            if path.startswith("sqlglot/optimizer/") and not hunk_tests:
                # Use the full optimizer suite only when the baseline has no
                # line or symbol evidence for the changed code.
                selected.update(suites.get("optimizer", []))
            if not hunk_tests:
                changed_lines = []
                for hunk in changed.get("hunks", []):
                    old_lines = hunk.get("old_lines", [])
                    if old_lines:
                        changed_lines.extend((line, 0) for line in old_lines)
                    else:
                        changed_lines.extend(
                            (max(1, line), indent)
                            for line, indent in hunk.get("added_points", [])
                        )
                if not changed_lines or not all(
                    _inside_function(function_map, path, line, indent)
                    for line, indent in changed_lines
                ):
                    selected.update(_path_tests(import_map, path))
            if path.startswith("sqlglot/parsers/"):
                stem = Path(path).stem.lower()
                selected.update(dialect_fixture_map.get(stem, []))
                if has_uncovered_hunk:
                    selected.update(test_file_map.get(f"tests/dialects/test_{stem}.py", []))
            elif path.startswith("sqlglot/dialects/"):
                stem = Path(path).stem.lower()
                selected.update(dialect_fixture_map.get(stem, []))
                if has_uncovered_hunk:
                    selected.update(test_file_map.get(f"tests/dialects/test_{stem}.py", []))
                if stem == "dialect":
                    selected.update(suites.get("dialect", []))
            # Broad file coverage is useful for import-time changes, but a
            # module imported by most of the suite should not drown out a
            # precise function-level witness. Keep it when the candidate set
            # is bounded or when it is close in size to the hunk witnesses.
            if len(file_hits) <= 40 and (
                not hunk_tests or len(file_hits) <= max(8, 2 * len(hunk_tests))
            ):
                selected.update(file_hits)

    return selected


def assign_workers(
    selected: set[int], tests: list[dict], durations: dict[int, int] | None = None
) -> list[list[int]]:
    """Choose the cheapest greedy LPT schedule across one to eight workers."""
    if durations is None:
        durations = {test["number"]: round(test["seconds"] * 1000) for test in tests}
    ordered = sorted(selected, key=lambda number: (-durations[number], number))
    best_workers: list[list[int]] = []
    best_cost: int | None = None
    for count in range(1, min(8, len(ordered)) + 1):
        workers: list[list[int]] = [[] for _ in range(count)]
        loads = [0] * count
        for number in ordered:
            lightest = min(range(count), key=loads.__getitem__)
            workers[lightest].append(number)
            loads[lightest] += durations[number]
        cost = max(loads) + count * 2000
        if best_cost is None or cost < best_cost:
            best_cost, best_workers = cost, workers
    return best_workers


def main() -> None:
    tests = _read_json(DATA / "tests.json")["tests"]
    durations = {test["number"]: round(test["seconds"] * 1000) for test in tests}
    test_files = {test["number"]: test["file"] for test in tests}
    try:
        impact = _read_impact_map(MAP_PATH)
    except (EOFError, OSError, json.JSONDecodeError, pickle.PickleError) as error:
        print(f"could not read impact map: {error}; selecting every test", file=sys.stderr)
        impact = {}
    if impact.get("base_commit") != BASE_COMMIT:
        print("impact map base commit mismatch; selecting every test", file=sys.stderr)
    patches = sorted((DATA / "commits").glob("*.patch"))
    for patch in patches:
        files = parse_patch(patch.read_text(encoding="utf-8", errors="replace"))
        if impact.get("base_commit") == BASE_COMMIT and files:
            selected = select_tests(files, tests, impact, durations, test_files)
        else:
            selected = {test["number"] for test in tests}
        workers = assign_workers(selected, tests, durations)
        output = {"commit": patch.stem, "workers": workers}
        if orjson is None:
            print(json.dumps(output, separators=(",", ":")))
        else:
            sys.stdout.buffer.write(orjson.dumps(output) + b"\n")
    print(f"selected tests for {len(patches)} commits using baseline impact maps", file=sys.stderr)


if __name__ == "__main__":
    main()
