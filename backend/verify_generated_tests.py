"""
verify_generated_tests.py — post-generation smoke check.

Bulk-generating dozens of tests in one pass (see pipeline.generate_all_gaps)
means a lot of template-rendered pytest text gets written without a human
reading each one first. This is the cheap net that catches a template-
rendering regression (an AI-suggested value that breaks the surrounding
Python, a missing escape, etc.) before it reaches the catalog unnoticed --
it does NOT check semantic correctness, only that every generated pytest
stub is at least syntactically valid Python.

Usage:
    python verify_generated_tests.py
"""
import ast
import sys
from pathlib import Path

PYTEST_DIR = Path(__file__).resolve().parent / "generated_tests" / "pytest"


def main():
    files = sorted(PYTEST_DIR.glob("test_*.py"))
    if not files:
        print(f"No generated pytest files found in {PYTEST_DIR}")
        return 0

    failures = []
    for f in files:
        try:
            ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError as e:
            failures.append((f.name, str(e)))

    print(f"Checked {len(files)} generated pytest file(s).")
    if failures:
        print(f"\n{len(failures)} FAILED to parse:")
        for name, err in failures:
            print(f"  - {name}: {err}")
        return 1

    print("All generated pytest stubs are syntactically valid.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
