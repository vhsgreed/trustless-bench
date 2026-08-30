#!/usr/bin/env python3
"""Sandbox eval harness for trustless-bench HumanEval.

Runs INSIDE the resource-limited subprocess (see run_code_sandboxed in
trustless-bench.py). This file is TRUSTED code (ours), the model code is
untrusted. Usage: python3 -I harness.py <code_file> <tests_file>

Reads the model's generated code + JSON test cases, execs the code in a
fresh namespace, evaluates each test case, prints a JSON result line.
"""
import json
import re
import sys


def strip_fences(code: str) -> str:
    """Remove markdown code fences + surrounding whitespace from model output.

    Models almost always wrap generated code in ```python ... ``` fences;
    exec'ing the raw text fails with SyntaxError on the fence line (observed
    08-30: all 3 humaneval 0.0 runs were fence-SyntaxErrors, not model
    failures). Strip a leading ```lang line and trailing ``` if present,
    then trim surrounding blank lines. Leave the code itself untouched.
    """
    text = code.strip()
    m = re.match(r"^```[a-zA-Z0-9_+-]*\s*\n", text)
    if m:
        text = text[m.end():]
        # trailing fence (allow trailing whitespace)
        text = re.sub(r"\n?```\s*$", "", text)
    return text.strip()


def main() -> None:
    raw_code = open(sys.argv[1]).read()
    code = strip_fences(raw_code)
    tests = json.load(open(sys.argv[2]))
    ns: dict = {}
    try:
        exec(code, ns)
    except Exception as e:
        print(json.dumps({"fatal": f"code exec: {type(e).__name__}: {e}"}))
        return

    # first user-defined callable (skips imports/underscore internals)
    fns = {k: v for k, v in ns.items() if callable(v) and not k.startswith("_")}
    if not fns:
        print(json.dumps({"fatal": "no callable function defined",
                          "ns_keys": [k for k in list(ns)[:10]]}))
        return

    fn_name, fn = list(fns.items())[0]
    passed = 0
    out = []
    for t in tests:
        try:
            res = str(eval(t["call"], {"__builtins__": {}}, {fn_name: fn}))
        except Exception as e:
            res = f"ERR:{type(e).__name__}"
        ok = res == t["expected"]
        passed += 1 if ok else 0
        out.append({"call": t["call"], "got": res, "expected": t["expected"], "ok": ok})
    print(json.dumps({"passed": passed, "total": len(tests), "tests": out}))


if __name__ == "__main__":
    main()
