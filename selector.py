"""Select sqlglot tests from checked-in baseline impact maps."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path


DATA = Path(os.environ.get("BLAST_RADIUS_DATA", "/data"))
MAP_PATH = Path(os.environ.get("BLAST_RADIUS_MAP", Path(__file__).with_name("impact_map.json")))
BASE_COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
DIFF_PATH_RE = re.compile(r"^diff --git a/(.*?) b/(.*?)$")


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
    result: set[int] = set()
    for start, end, tests in symbols.get(path, []):
        if start <= line <= end:
            result.update(tests)
    return result


def _path_tests(path_map: dict, path: str) -> set[int]:
    return set(path_map.get(path, []))


def _normalize_patch_path(path: str) -> str:
    return path.replace("\\", "/").removeprefix("./")


def select_tests(files: list[dict], tests: list[dict], impact: dict) -> set[int]:
    selected: set[int] = set()
    line_map = impact.get("line_tests", {})
    symbol_map = impact.get("symbol_tests", {})
    file_map = impact.get("file_tests", {})
    import_map = impact.get("import_tests", {})
    test_file_map = impact.get("test_file_tests", {})
    fixture_map = impact.get("fixture_tests", {})
    suites = impact.get("suite_tests", {})
    all_tests = {test["number"] for test in tests}

    for changed in files:
        old_path = _normalize_patch_path(changed.get("old_path", ""))
        new_path = _normalize_patch_path(changed.get("new_path", ""))
        relevant_paths = {path for path in (old_path, new_path) if path and path != "/dev/null"}
        for path in relevant_paths:
            if path.startswith("tests/fixtures/"):
                fixture_key = Path(path).as_posix()
                fixture_tests = _path_tests(fixture_map, fixture_key)
                if fixture_tests:
                    selected.update(fixture_tests)
                # Fixture names usually identify one case, but fixtures can
                # participate in shared validation helpers. Include their
                # owning test suite as a recall guard.
                parts = Path(path).parts
                if len(parts) >= 3 and parts[2] == "optimizer":
                    selected.update(suites.get("optimizer", []))
                elif fixture_tests:
                    selected.update(fixture_tests)
                else:
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
            selected.update(file_hits)
            selected.update(_path_tests(import_map, path))
            if path.startswith("sqlglot/optimizer/"):
                selected.update(suites.get("optimizer", []))
                selected.update(suites.get("executor", []))
            elif path.startswith(("sqlglot/parsers/", "sqlglot/dialects/", "sqlglot/expressions/", "sqlglot/transforms.py")):
                selected.update(suites.get("optimizer", []))
                if path.startswith("sqlglot/dialects/"):
                    dialect = Path(path).stem
                    selected.update(test_file_map.get(f"tests/dialects/test_{dialect}.py", []))
                    selected.update(suites.get("dialect", []))
            if path.startswith(("sqlglot/typing/", "sqlglot/expressions/")):
                selected.update(suites.get("typing", []))
            if not file_hits and path.startswith("sqlglot/"):
                selected.update(all_tests)
            for hunk in changed.get("hunks", []):
                old_lines = hunk.get("old_lines", [])
                if old_lines:
                    for line in old_lines:
                        selected.update(_line_tests(line_map, path, line))
                        selected.update(_tests_in_symbol(symbol_map, path, line))
                else:
                    # Pure additions have no baseline line coverage. Anchor to
                    # the nearest parent lines and the enclosing baseline symbol.
                    anchor = max(1, hunk["old_start"])
                    selected.update(_tests_in_symbol(symbol_map, path, anchor))
                    for line in range(max(1, anchor - 2), anchor + 3):
                        selected.update(_line_tests(line_map, path, line))

    return selected


def assign_workers(selected: set[int], tests: list[dict]) -> list[list[int]]:
    """Choose the cheapest greedy LPT schedule across one to eight workers."""
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
    tests = json.loads((DATA / "tests.json").read_text(encoding="utf-8"))["tests"]
    try:
        impact = json.loads(MAP_PATH.read_text(encoding="utf-8"))
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
        workers = assign_workers(selected, tests)
        print(json.dumps({"commit": patch.stem, "workers": workers}, separators=(",", ":")))
    print(f"selected tests for {len(patches)} commits using baseline impact maps", file=sys.stderr)


if __name__ == "__main__":
    main()
