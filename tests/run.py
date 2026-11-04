#!/usr/bin/env python3
"""Runs every test_*.py next to this file. No pytest required.

    python3 tests/run.py
"""

import importlib.util
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def main():
    failures = []
    total = 0
    for name in sorted(os.listdir(HERE)):
        if not (name.startswith("test_") and name.endswith(".py")):
            continue
        spec = importlib.util.spec_from_file_location(
            name[:-3], os.path.join(HERE, name))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        print(f"\n{name}")
        for case in sorted(d for d in dir(mod) if d.startswith("test_")):
            total += 1
            try:
                getattr(mod, case)()
                print(f"  pass  {case}")
            except Exception:
                failures.append((name, case, traceback.format_exc()))
                print(f"  FAIL  {case}")

    print(f"\n{total - len(failures)}/{total} passed")
    for name, case, tb in failures:
        print(f"\n--- {name}::{case}\n{tb}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
