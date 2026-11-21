"""Select sqlglot tests from checked-in baseline impact maps."""

from __future__ import annotations

import json
import os
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
            current["hunks"][-1]["old_lines"] = current["hunks"][-1].get("old_lines", []) + [old_line]
            old_line += 1
        elif line.startswith("+") and not line.startswith("+++"):
            current["hunks"][-1]["added"] = True
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


def _tests_in_symbol(symbols: dict, path: str, line: int) -> set[int]:
    matches = [
        (end - start, tests)
        for start, end, tests in symbols.get(path, [])
        if start <= line <= end
    ]
    if not matches:
        return set()
    smallest = min(span for span, _ in matches)
    return {number for span, tests in matches if span == smallest for number in tests}


def _inside_function(function_ranges: dict, path: str, line: int, added_indent: int = 0) -> bool:
    ranges = function_ranges.get(path, [])
    return any(
        start <= line <= end or (added_indent > 0 and start == line + 1)
        for start, end in ranges
    )


def _path_tests(path_map: dict, path: str) -> set[int]:
    return set(path_map.get(path, []))


def _normalize_patch_path(path: str) -> str:
    return path.replace("\\", "/").removeprefix("./")


def _read_json(path: Path):
    data = path.read_bytes()
    return orjson.loads(data) if orjson is not None else json.loads(data)


def select_tests(files: list[dict], tests: list[dict], impact: dict) -> set[int]:
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
                selected.update(api_consumers.get("optimizer_pipeline", []))
                if Path(path).name == "eliminate_joins.py":
                    selected.update(api_consumers.get("optimizer_joins", []))
                    selected.update(api_consumers.get("executor_large_datasets", []))
                    selected.update(fixture_map.get("tests/fixtures/optimizer/tpc-ds/tpc-ds.sql", []))
                    selected.update(fixture_map.get("tests/fixtures/optimizer/tpc-h/tpc-h.sql", []))
                elif "subquer" in Path(path).name.lower():
                    selected.update(api_consumers.get("optimizer_subqueries", []))
            if path.startswith(("sqlglot/typing/", "sqlglot/expressions/")):
                selected.update(suites.get("typing", []))
            if not file_hits and path.startswith("sqlglot/"):
                selected.update(all_tests)
            hunk_tests: set[int] = set()
            for hunk in changed.get("hunks", []):
                old_lines = hunk.get("old_lines", [])
                if old_lines:
                    for line in old_lines:
                        line_hits = _line_tests(line_map, path, line)
                        hunk_tests.update(line_hits)
                        if len(line_hits) < SYMBOL_FALLBACK_MAX_LINE_TESTS:
                            # A thin line witness set may miss other callers
                            # exercising branches in the same changed function.
                            hunk_tests.update(_tests_in_symbol(symbol_map, path, line))
                else:
                    # Pure additions have no baseline line coverage. Anchor to
                    # the nearest parent lines and the enclosing baseline symbol.
                    anchor = max(1, hunk["old_start"])
                    hunk_tests.update(_tests_in_symbol(symbol_map, path, anchor))
                    for line in range(max(1, anchor - 2), anchor + 3):
                        hunk_tests.update(_line_tests(line_map, path, line))
            selected.update(hunk_tests)
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
                selected.update(test_file_map.get(f"tests/dialects/test_{stem}.py", []))
            elif path.startswith("sqlglot/dialects/"):
                stem = Path(path).stem.lower()
                if len(hunk_tests) <= 10:
                    selected.update(dialect_fixture_map.get(stem, []))
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
    try:
        impact = _read_json(MAP_PATH)
    except (OSError, json.JSONDecodeError) as error:
        print(f"could not read impact map: {error}; selecting every test", file=sys.stderr)
        impact = {}
    if impact.get("base_commit") != BASE_COMMIT:
        print("impact map base commit mismatch; selecting every test", file=sys.stderr)
    patches = sorted((DATA / "commits").glob("*.patch"))
    for patch in patches:
        files = parse_patch(patch.read_text(encoding="utf-8", errors="replace"))
        if impact.get("base_commit") == BASE_COMMIT and files:
            selected = select_tests(files, tests, impact)
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
