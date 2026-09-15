#!/usr/bin/env python3
"""Run the locked, offline guardrail regression checks.

The checks and assertions live in tests/guardrail_checks.py so this command
and the acceptance suite use the same protected source.
"""

import runpy
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


if __name__ == "__main__":
    runpy.run_path(str(REPO / "tests" / "guardrail_checks.py"), run_name="__main__")
