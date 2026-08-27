#!/usr/bin/env python3
"""Sandbox self-test for trustless-bench (CI smoke + local verification).

Checks that run_code_sandboxed contains untrusted model code: good code
passes, fork bombs / subprocess exfil / infinite loops are contained.
"""
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "trustless_bench", os.path.join(HERE, "..", "trustless-bench.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

TESTS = [("has_close_elements([1.0, 2.0, 3.0], 0.5)", "False")]

GOOD = (
    "def has_close_elements(numbers, threshold):\n"
    "    for i in range(len(numbers)):\n"
    "        for j in range(i+1, len(numbers)):\n"
    "            if abs(numbers[i]-numbers[j]) < threshold:\n"
    "                return True\n"
    "    return False\n"
)
FORK_BOMB = "import os\nwhile True: os.fork()\n"
EXFIL = (
    "def has_close_elements(numbers, threshold):\n"
    "    import subprocess\n"
    "    subprocess.run(['cat', '/etc/hostname'])\n"
    "    return False\n"
)
LOOP = "def has_close_elements(numbers, threshold):\n    while True:\n        pass\n"


def main() -> int:
    p, t, out = m.run_code_sandboxed(GOOD, TESTS)
    assert p == 1 and t == 1, f"good code should pass: {out}"
    print("✓ good code passes")

    p, t, out = m.run_code_sandboxed(FORK_BOMB, TESTS)
    assert out.get("fatal") or out.get("tests", [{}])[0].get("got", "").startswith("ERR"), out
    print("✓ fork bomb contained")

    p, t, out = m.run_code_sandboxed(EXFIL, TESTS)
    got = out.get("tests", [{}])[0].get("got", "")
    assert "hostname" not in json.dumps(out) or got.startswith("ERR"), out
    print("✓ subprocess exfil contained")

    p, t, out = m.run_code_sandboxed(LOOP, TESTS)
    assert p == 0 and (out.get("fatal") or out == {}), out
    print("✓ infinite loop contained")

    print("ALL SANDBOX TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
