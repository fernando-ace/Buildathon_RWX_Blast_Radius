"""Build test-to-code coverage maps for the pinned sqlglot checkout.

Run this once on the sqlglot base commit, then commit the generated map. The
runtime selector never imports the target repository or runs its tests.
Requires pytest and coverage.py in the build environment.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

import coverage
import pytest


PINNED_COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"


class PerTestContext:
    """Attribute setup, call, and teardown lines to the active pytest item."""

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_runtest_protocol(self, item):
        self.coverage.switch_context(item.nodeid)
        self.active_nodeid = item.nodeid
        try:
            yield
        finally:
            self.active_nodeid = None

    def __init__(self, coverage_session: coverage.Coverage):
        self.coverage = coverage_session
        self.active_nodeid: str | None = None


def normalized_path(path: Path, root: Path) -> str | None:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return None


def compress_lines(per_line: dict[int, set[int]]) -> list[list]:
    result: list[list] = []
    start = previous = None
    current_tests: tuple[int, ...] = ()
    for line in sorted(per_line):
        test_set = tuple(sorted(per_line[line]))
        if start is not None and line == previous + 1 and test_set == current_tests:
            previous = line
            continue
        if start is not None:
            result.append([start, previous, list(current_tests)])
        start = previous = line
        current_tests = test_set
    if start is not None:
        result.append([start, previous, list(current_tests)])
    return result


def fixture_map(repo: Path, tests: list[dict]) -> dict[str, list[int]]:
    """Associate fixture basenames with tests that name their validation case."""
    normalized_tests = [(test, re.sub(r"[^a-z0-9]+", "", test["id"].lower())) for test in tests]
    result: dict[str, list[int]] = {}
    fixture_root = repo / "tests" / "fixtures"
    if not fixture_root.exists():
        return result
    for fixture in fixture_root.rglob("*"):
        if not fixture.is_file():
            continue
        key = fixture.relative_to(repo).as_posix()
        stem = re.sub(r"[^a-z0-9]+", "", fixture.stem.lower())
        if not stem:
            continue
        matches = [test["number"] for test, node in normalized_tests if stem in node]
        if matches:
            result[key] = matches
    return result


def dialect_fixture_map(repo: Path, fixtures: dict[str, list[int]]) -> dict[str, list[int]]:
    """Map dialect names to tests that consume matching SQL fixtures."""
    result: dict[str, set[int]] = defaultdict(set)
    dialect_line = re.compile(r"^\s*#\s*dialect:\s*([A-Za-z0-9_]+)", re.IGNORECASE | re.MULTILINE)
    for relative, numbers in fixtures.items():
        path = repo / relative
        if path.suffix.lower() not in {".sql", ".txt"}:
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for dialect in dialect_line.findall(content):
            result[dialect.lower()].update(numbers)
    return {dialect: sorted(numbers) for dialect, numbers in sorted(result.items())}


def api_consumer_map(repo: Path, tests: list[dict]) -> dict[str, list[int]]:
    """Index test functions that call core optimizer APIs by name."""
    by_file: dict[str, list[dict]] = defaultdict(list)
    for test in tests:
        by_file[test["file"]].append(test)
    groups: dict[str, set[int]] = defaultdict(set)
    for test_file, file_tests in by_file.items():
        if test_file not in {"tests/test_optimizer.py", "tests/test_executor.py"}:
            continue
        try:
            tree = ast.parse((repo / test_file).read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeError):
            continue
        by_name: dict[str, set[int]] = defaultdict(set)
        for test in file_tests:
            name = test["id"].split("::")[-1].split("[", 1)[0]
            by_name[name].add(test["number"])
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or not node.name.startswith("test_"):
                continue
            numbers = by_name.get(node.name, set())
            if not numbers:
                continue
            references = {
                ast.unparse(child)
                for child in ast.walk(node)
                if isinstance(child, (ast.Name, ast.Attribute))
            }
            if test_file == "tests/test_optimizer.py":
                if "optimizer.optimize" in references:
                    groups["optimizer_pipeline"].update(numbers)
                if any("eliminate_joins" in reference for reference in references):
                    groups["optimizer_joins"].update(numbers)
                if any("subquer" in node.name.lower() or "subquer" in ref.lower() for ref in references):
                    groups["optimizer_subqueries"].update(numbers)
            elif node.name in {"test_execute_tpcds", "test_execute_tpch"}:
                groups["executor_large_datasets"].update(numbers)
    return {name: sorted(numbers) for name, numbers in sorted(groups.items())}


def import_map(repo: Path, tests: list[dict]) -> dict[str, list[int]]:
    """Map direct test-module imports to their owning tests for import failures."""
    by_file: dict[str, list[dict]] = defaultdict(list)
    for test in tests:
        by_file[test["file"]].append(test)
    result: dict[str, set[int]] = defaultdict(set)

    def module_files(module: str) -> list[str]:
        if not module.startswith("sqlglot"):
            return []
        candidate = repo.joinpath(*module.split("."))
        found = []
        for path in (candidate.with_suffix(".py"), candidate / "__init__.py"):
            if path.is_file():
                relative = path.relative_to(repo).as_posix()
                if relative.startswith("sqlglot/"):
                    found.append(relative)
        return found

    for test_file, file_tests in by_file.items():
        source = repo / test_file
        try:
            tree = ast.parse(source.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeError):
            continue
        names: dict[str, set[int]] = defaultdict(set)
        for test in file_tests:
            names[test["id"].split("::")[-1].split("[", 1)[0]].add(test["number"])
        package = test_file[:-3].replace("/", ".").rpartition(".")[0]

        def record_import(node: ast.AST, target_tests: set[int]) -> None:
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    pieces = package.split(".")
                    base = ".".join(pieces[: len(pieces) - node.level + 1])
                    base = ".".join(part for part in (base, node.module or "") if part)
                else:
                    base = node.module or ""
                modules.append(base)
                modules.extend(
                    ".".join(part for part in (base, alias.name) if part)
                    for alias in node.names
                    if alias.name != "*"
                )
            for module in modules:
                parts = module.split(".")
                for length in range(1, len(parts) + 1):
                    for relative in module_files(".".join(parts[:length])):
                        result[relative].update(target_tests)

        def visit(node: ast.AST, active_tests: set[int]) -> None:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                active_tests = names.get(node.name, active_tests)
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                record_import(node, active_tests)
            for child in ast.iter_child_nodes(node):
                visit(child, active_tests)

        visit(tree, {test["number"] for test in file_tests})
    return {path: sorted(numbers) for path, numbers in sorted(result.items())}


def symbol_map(repo: Path, line_tests: dict[str, list[list]]) -> dict[str, list[list]]:
    """Group baseline tests by enclosing Python class/function ranges."""
    output: dict[str, list[list]] = {}
    for relative, compressed in line_tests.items():
        if not relative.startswith("sqlglot/") or not relative.endswith(".py"):
            continue
        source = repo / relative
        try:
            tree = ast.parse(source.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeError):
            continue
        line_to_tests: dict[int, set[int]] = {}
        for start, end, numbers in compressed:
            for line in range(start, end + 1):
                line_to_tests[line] = set(numbers)
        ranges: list[tuple[int, int, set[int]]] = []

        def walk(node: ast.AST) -> None:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                start = node.lineno
                end = getattr(node, "end_lineno", start)
                touched: set[int] = set()
                for line in range(start, end + 1):
                    touched.update(line_to_tests.get(line, ()))
                if touched:
                    ranges.append((start, end, touched))
            for child in ast.iter_child_nodes(node):
                walk(child)

        walk(tree)
        if ranges:
            output[relative] = [[start, end, sorted(numbers)] for start, end, numbers in ranges]
    return output


def build(repo: Path, tests_path: Path, output_path: Path) -> None:
    tests = json.loads(tests_path.read_text(encoding="utf-8"))["tests"]
    number_by_node = {test["id"]: test["number"] for test in tests}
    file_tests: dict[str, set[int]] = defaultdict(set)
    line_hits: dict[str, dict[int, set[int]]] = defaultdict(lambda: defaultdict(set))
    fixture_reads: dict[str, set[int]] = defaultdict(set)
    fixture_root = os.path.normcase(os.path.abspath(repo / "tests" / "fixtures"))
    fixture_prefix = fixture_root + os.sep

    with tempfile.TemporaryDirectory(prefix="blast-radius-coverage-") as temporary:
        data_path = Path(temporary) / ".coverage"
        coverage_session = coverage.Coverage(
            data_file=str(data_path),
            source=[str(repo / "sqlglot")],
            config_file=False,
            branch=False,
        )
        plugin = PerTestContext(coverage_session)
        def audit_file_open(event: str, args: tuple) -> None:
            if event != "open" or plugin.active_nodeid is None or not args:
                return
            raw = args[0]
            if not isinstance(raw, (str, bytes, os.PathLike)):
                return
            value = os.fsdecode(raw)
            if "fixtures" not in value.replace("\\", "/"):
                return
            candidate = os.path.normcase(os.path.abspath(value))
            if candidate.startswith(fixture_prefix):
                number = number_by_node.get(plugin.active_nodeid)
                if number is not None:
                    relative = Path(candidate).relative_to(repo.resolve()).as_posix()
                    fixture_reads[relative].add(number)

        sys.addaudithook(audit_file_open)
        coverage_session.start()
        old_skip = os.environ.get("SKIP_INTEGRATION")
        old_cwd = Path.cwd()
        os.environ["SKIP_INTEGRATION"] = "0"
        sys.path.insert(0, str(repo))
        try:
            os.chdir(repo)
            exit_code = pytest.main(["-q", "tests"], plugins=[plugin])
        finally:
            os.chdir(old_cwd)
            if old_skip is None:
                os.environ.pop("SKIP_INTEGRATION", None)
            else:
                os.environ["SKIP_INTEGRATION"] = old_skip
            coverage_session.stop()
            coverage_session.save()
        if exit_code not in (pytest.ExitCode.OK, pytest.ExitCode.TESTS_FAILED):
            raise SystemExit(f"pytest could not complete (exit {int(exit_code)})")

        data = coverage.CoverageData(basename=str(data_path))
        data.read()
        for measured in data.measured_files():
            relative = normalized_path(Path(measured), repo)
            if relative is None or not relative.startswith("sqlglot/"):
                continue
            for line, contexts in data.contexts_by_lineno(measured).items():
                for context in contexts:
                    number = number_by_node.get(context)
                    if number is not None:
                        line_hits[relative][line].add(number)
                        file_tests[relative].add(number)

    compact_lines = {path: compress_lines(lines) for path, lines in sorted(line_hits.items())}
    file_lists = {path: sorted(numbers) for path, numbers in sorted(file_tests.items())}
    by_test_file: dict[str, list[int]] = defaultdict(list)
    for test in tests:
        by_test_file[test["file"]].append(test["number"])
    fixture_tests = fixture_map(repo, tests)
    for path, numbers in fixture_reads.items():
        fixture_tests[path] = sorted(set(fixture_tests.get(path, [])) | numbers)
    result = {
        "version": 1,
        "base_commit": PINNED_COMMIT,
        "python_version": platform.python_version(),
        "coverage_version": coverage.__version__,
        "mapped_test_count": len({number for numbers in file_lists.values() for number in numbers}),
        "line_tests": compact_lines,
        "file_tests": file_lists,
        "import_tests": import_map(repo, tests),
        "symbol_tests": symbol_map(repo, compact_lines),
        "test_file_tests": dict(sorted(by_test_file.items())),
        "suite_tests": {
            "optimizer": by_test_file.get("tests/test_optimizer.py", []),
            "executor": by_test_file.get("tests/test_executor.py", []),
            "dialect": by_test_file.get("tests/dialects/test_dialect.py", []),
            "typing": by_test_file.get("tests/test_typing.py", []),
            "sqlglotc": by_test_file.get("tests/test_sqlglotc.py", []),
        },
        "fixture_tests": fixture_tests,
        "dialect_fixture_tests": dialect_fixture_map(repo, fixture_tests),
        "api_consumer_tests": api_consumer_map(repo, tests),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(result, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 20_000_000:
        raise SystemExit("impact map exceeds the track's 20 MB repository limit")
    output_path.write_text(encoded, encoding="utf-8")
    print(
        f"wrote {output_path} ({output_path.stat().st_size:,} bytes; "
        f"{len(file_lists)} source files, {sum(len(v) for v in file_lists.values())} file/test links)",
        file=sys.stderr,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("repository", type=Path, help="sqlglot checkout at the pinned commit")
    parser.add_argument("--tests", type=Path, default=Path("sample/tests.json"))
    parser.add_argument("--output", type=Path, default=Path("impact_map.json"))
    args = parser.parse_args()
    repo = args.repository.resolve()
    actual = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if actual != PINNED_COMMIT:
        raise SystemExit(f"expected sqlglot {PINNED_COMMIT}, got {actual or 'no git commit'}")
    build(repo, args.tests.resolve(), args.output.resolve())


if __name__ == "__main__":
    main()
