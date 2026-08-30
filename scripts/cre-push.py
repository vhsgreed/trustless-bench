#!/usr/bin/env python3
"""cre-push.py — push latest attested benchmark run to the CRE oracle.

Last integration step: hub trustless-bench.py → CRE HTTP trigger → EVM write
on Base Sepolia. Reads the latest run from the local trustless.db, builds the
HTTP payload the workflow expects (benchId, model, scorePct, promptHash,
responseHash), POSTs it to the CRE HTTP trigger URL.

Usage:
    export CRE_PUSH_URL=https://...          # workflow HTTP trigger URL (from cre workflow deploy)
    python3 cre-push.py [--run-id ID]        # push a specific run (default: latest attested)
    python3 cre-push.py --dry-run            # print payload + target, no POST
    python3 cre-push.py --json               # machine-readable result (for digest/wiring)

Env:
    CRE_PUSH_URL    — required for real POST (dry-run without it prints target)
    CRE_PUSH_TIMEOUT— request timeout seconds (default 30)

Exit codes:
    0 pushed OK / dry-run printed
    1 no attestable run found
    2 HTTP/network failure
    3 payload schema mismatch (workflow would reject)
"""

import argparse
import datetime
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request

WORKSPACE = os.environ.get("WORKSPACE",
                           os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BENCH_DB = os.path.join(WORKSPACE, "benchmarks", "trustless.db")

# Model name → canonical slug used in workflow reports (keep in sync with
# DEFAULT_QUEUE / OpenRouter slugs; normalized on push).
MODEL_SLUGS = {}


def latest_attested_run(run_id: str | None) -> dict | None:
    """Return the latest attested run row (or a specific one by run_id).

    Attested = run is verified AND has prompt_hash/response_hash AND has a
    per-bench .sig file in RESULTS_DIR (the attestation column does not
    exist in the schema; publish_result writes run_id-<bench>.sig).
    """
    if not os.path.exists(BENCH_DB):
        print(f"no bench DB at {BENCH_DB}", file=sys.stderr)
        return None
    conn = sqlite3.connect(BENCH_DB)
    conn.row_factory = sqlite3.Row
    try:
        if run_id:
            cur = conn.execute(
                "SELECT * FROM runs WHERE run_id = ? ORDER BY created_at DESC LIMIT 1",
                (run_id,))
        else:
            cur = conn.execute(
                "SELECT * FROM runs WHERE verified = 1 "
                "AND prompt_hash IS NOT NULL AND response_hash IS NOT NULL "
                "ORDER BY created_at DESC LIMIT 1")
        row = cur.fetchone()
        if row is None:
            return None
        d = dict(row)
        # attestation lives as a per-bench .sig file next to the result JSON
        sig_file = os.path.join(
            WORKSPACE, "benchmarks", "results",
            f"{d['run_id']}-{d['benchmark']}.sig")
        d["attestation"] = None
        if os.path.exists(sig_file):
            d["attestation"] = open(sig_file).read().strip()
        return d
    finally:
        conn.close()


def build_payload(run: dict) -> dict:
    """Build the HTTP payload the CRE workflow's attestationSchema expects."""
    # score stored as fraction 0..1 in DB? normalize to percent 0..100.
    score = run.get("score")
    if score is None:
        raise ValueError(f"run {run['run_id']} has no score")
    score_f = float(score)
    if score_f <= 1.0:  # stored as fraction → percent
        score_pct = round(score_f * 100, 4)
    else:
        score_pct = round(score_f, 4)
    model = MODEL_SLUGS.get(run.get("model", ""), run.get("model", ""))

    def norm_hash(h: str | None) -> str:
        """DB stores bare hex (64 chars); the workflow schema + EVM bytes32
        require 0x-prefixed 32-byte hex. Normalize defensively."""
        if not h:
            return h
        h = h.strip()
        if h.startswith("0x"):
            return h
        return "0x" + h

    return {
        "benchId": run["run_id"],
        "model": model,
        "scorePct": score_pct,
        "promptHash": norm_hash(run.get("prompt_hash")),
        "responseHash": norm_hash(run.get("response_hash")),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-id", help="push specific run id (default: latest attested)")
    ap.add_argument("--dry-run", action="store_true", help="print payload, no POST")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    run = latest_attested_run(args.run_id)
    if run is None:
        if args.json:
            print(json.dumps({"ok": False, "reason": "no_attested_run"}))
        else:
            print("no attestable run found (need verified=1 + hashes + attestation)")
        return 1

    try:
        payload = build_payload(run)
    except ValueError as e:
        if args.json:
            print(json.dumps({"ok": False, "reason": str(e)}))
        else:
            print(f"payload error: {e}", file=sys.stderr)
        return 3

    url = os.environ.get("CRE_PUSH_URL", "")
    timeout = float(os.environ.get("CRE_PUSH_TIMEOUT", "30"))
    body = json.dumps(payload).encode()

    if args.dry_run or not url:
        if args.json:
            print(json.dumps({"ok": True, "dry_run": True, "url": url,
                              "payload": payload, "run": run["run_id"]}))
        else:
            print(f"DRY-RUN  target={url or '<CRE_PUSH_URL unset>'}")
            print(f"  run_id       {run['run_id']}")
            print(f"  model        {payload['model']}")
            print(f"  scorePct     {payload['scorePct']}")
            print(f"  promptHash   {payload['promptHash']}")
            print(f"  responseHash {payload['responseHash']}")
        return 0

    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            out = resp.read().decode()
    except urllib.error.HTTPError as e:
        if args.json:
            print(json.dumps({"ok": False, "reason": f"HTTP {e.code}",
                              "body": e.read().decode(errors="replace")[:500]}))
        else:
            print(f"HTTP {e.code}: {e.read().decode(errors='replace')[:500]}",
                  file=sys.stderr)
        return 2
    except urllib.error.URLError as e:
        if args.json:
            print(json.dumps({"ok": False, "reason": str(e.reason)}))
        else:
            print(f"network error: {e.reason}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps({"ok": True, "run": run["run_id"],
                          "response": out[:500]}))
    else:
        print(f"PUSHED run={run['run_id']} → {url}")
        print(out[:500])
    return 0


if __name__ == "__main__":
    sys.exit(main())
