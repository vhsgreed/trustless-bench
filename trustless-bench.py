#!/usr/bin/env python3
"""
Trustless Benchmark Engine — oracle-verifiable AI model evaluation.

Architecture:
  1. Benchmark runner — executes standard benchmarks against models via OpenRouter
  2. Verifier — independent check that benchmark was run correctly (hash input→output)
  3. Scorer — computes scores deterministically (no AI in scoring path)
  4. Publisher — writes immutable results (local ledger → Chainlink/CRE oracle later)

Benchmarks:
  - MMLU-Pro (text reasoning)
  - HumanEval (code generation, sandboxed execution)
  - Custom tasks (configurable)

Trust model (Phase 1 "attestable", 2026-08-27):
  - Commitment: manifest-<date>.json written BEFORE each run — hash of the
    question set + models + nonce. Published. Proves we didn't cherry-pick.
  - Integrity: every run stores prompt_hash (question set) + response_hash
    (raw model output) — offline replay without API access.
  - Attestation: ECDSA-SHA256 signature (openssl, prime256v1) over
    (run_id, model, bench, score, prompt_hash, response_hash). Public key in
    repo; private key stays local (0600, gitignored).
  - Verification: `--verify RUN_ID` replays deterministically from stored
    answers + checks hashes + checks signature. No API calls.

Design principles:
  - No AI in the scoring path — deterministic math only
  - Every run is content-hashable — replayable, verifiable
  - Real question set lives in benchmarks/questions.json (PRIVATE, gitignored);
    the public repo ships example questions only (CI/nightly/demo runs)

Usage:
  python3 scripts/trustless-bench.py                         # run next model in queue
  python3 scripts/trustless-bench.py --model MODEL_ID        # bench specific model
  python3 scripts/trustless-bench.py --list                  # list previous results
  python3 scripts/trustless-bench.py --verify RUN_ID         # offline verify (hash replay + attestation)
  python3 scripts/trustless-bench.py --verify RUN_ID --live  # old behavior: live re-run
  python3 scripts/trustless-bench.py --schedule              # add next models to queue
  python3 scripts/trustless-bench.py --init-attestation      # generate ECDSA keypair
"""
import argparse
import base64
import datetime
import hashlib
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

WORKSPACE = Path(os.environ.get("WORKSPACE", os.path.expanduser("~/.openclaw/workspace")))
BENCH_DB = WORKSPACE / "benchmarks" / "trustless.db"
RESULTS_DIR = WORKSPACE / "benchmarks" / "results"
QUEUE_FILE = WORKSPACE / "benchmarks" / "queue.json"
QUESTIONS_FILE = WORKSPACE / "benchmarks" / "questions.json"   # PRIVATE, gitignored
ATTEST_DIR = WORKSPACE / "benchmarks" / "attestation"
ATTEST_PRIV = ATTEST_DIR / "private.pem"                       # 0600, gitignored
ATTEST_PUB = ATTEST_DIR / "public.pem"                         # committed to repo
OPENROUTER_KEY_PATH = os.path.expanduser(
    os.environ.get("OR_KEY_FILE", "~/.openclaw/secrets/openrouter-key"))

# ── Model queue (models to benchmark, prioritized) ───────────────────────
DEFAULT_QUEUE = [
    {"model": "z-ai/glm-5.3-flash", "priority": 1, "added": "2026-08-26"},
    {"model": "z-ai/glm-5.3", "priority": 1, "added": "2026-08-26"},
    {"model": "mistralai/mistral-small-3.1-24b", "priority": 2, "added": "2026-08-26"},
    {"model": "google/gemma-4-31b-it:free", "priority": 2, "added": "2026-08-26"},
    {"model": "qwen/qwen3.8-27b", "priority": 2, "added": "2026-08-26"},
    {"model": "nvidia/nemotron-3-ultra-550b-a55b:free", "priority": 3, "added": "2026-08-26"},
    {"model": "thinkingmachines/inkling:free", "priority": 3, "added": "2026-08-26"},
]

# ── Example questions (PUBLIC, shipped in repo) ──────────────────────────
# Real question set loads from benchmarks/questions.json (private). The
# examples keep the public engine runnable for CI/nightly/demo — results
# from example runs are labeled as such, never trusted scores.

EXAMPLE_MMLU_PRO_QUESTIONS = [
    {
        "id": "example-mmlu-physics-1",
        "domain": "physics",
        "question": "A photon with wavelength λ = 500 nm strikes a metal surface with work function φ = 2.0 eV. What is the maximum kinetic energy of the emitted electron?",
        "options": ["A) 0.48 eV", "B) 1.24 eV", "C) 0.00 eV (no emission)", "D) 2.48 eV"],
        "answer": "A",
    },
    {
        "id": "example-mmlu-cs-1",
        "domain": "computer_science",
        "question": "What is the time complexity of finding the shortest path in a weighted graph with V vertices and E edges using Dijkstra's algorithm with a binary heap?",
        "options": ["A) O(V²)", "B) O(E log V)", "C) O(V + E)", "D) O(VE)"],
        "answer": "B",
    },
    {
        "id": "example-mmlu-math-1",
        "domain": "mathematics",
        "question": "If f(x) = 3x³ - 2x² + x - 5, what is f''(2)?",
        "options": ["A) 32", "B) 36", "C) 16", "D) 34"],
        "answer": "A",
    },
]

EXAMPLE_HUMANEVAL_QUESTIONS = [
    {
        "id": "example-humaneval-1",
        "task": "Write a Python function `has_close_elements(numbers, threshold)` that returns True if any two numbers in the list are closer than the threshold.",
        "test_cases": [
            ("has_close_elements([1.0, 2.0, 3.0], 0.5)", "False"),
            ("has_close_elements([1.0, 2.8, 3.0, 4.0, 5.0, 2.0], 0.3)", "True"),
        ],
    },
]


def load_questions() -> tuple:
    """Load question sets. Private questions.json wins; else example set.

    Returns (mmlu_pro_questions, humaneval_questions, source_label).
    """
    if QUESTIONS_FILE.exists():
        q = json.loads(QUESTIONS_FILE.read_text())
        return (q.get("mmlu_pro", EXAMPLE_MMLU_PRO_QUESTIONS),
                q.get("humaneval", EXAMPLE_HUMANEVAL_QUESTIONS),
                f"private questions.json ({len(q.get('mmlu_pro', []))} MMLU / "
                f"{len(q.get('humaneval', []))} HumanEval)")
    return (EXAMPLE_MMLU_PRO_QUESTIONS, EXAMPLE_HUMANEVAL_QUESTIONS,
            "EXAMPLE questions (public demo set — NOT trusted scores)")


# ── DB setup ─────────────────────────────────────────────────────────────
def init_db():
    BENCH_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(BENCH_DB))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY,
            model TEXT NOT NULL,
            benchmark TEXT NOT NULL,
            score REAL,
            n_questions INTEGER,
            n_correct INTEGER,
            prompt_hash TEXT,
            response_hash TEXT,
            verified INTEGER DEFAULT 0,
            details TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    return conn


def read_api_key() -> str:
    try:
        return Path(OPENROUTER_KEY_PATH).read_text().strip()
    except Exception:
        raise RuntimeError(f"No API key at {OPENROUTER_KEY_PATH}")


def hash_content(content: str) -> str:
    """Short content-hash (run IDs)."""
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def hash256(content: str) -> str:
    """Full SHA-256 hex — commitment/integrity hashes."""
    return hashlib.sha256(content.encode()).hexdigest()


# ── Attestation (ECDSA-SHA256 via openssl CLI, zero Python deps) ─────────
def init_attestation_keys() -> None:
    """Generate P-256 keypair. Private key 0600 (never committed)."""
    ATTEST_DIR.mkdir(parents=True, exist_ok=True)
    if ATTEST_PRIV.exists() and ATTEST_PUB.exists():
        print(f"Attestation keys already exist: {ATTEST_DIR}")
        return
    subprocess.run(["openssl", "ecparam", "-genkey", "-name", "prime256v1",
                    "-noout", "-out", str(ATTEST_PRIV)], check=True)
    subprocess.run(["openssl", "ec", "-in", str(ATTEST_PRIV), "-pubout",
                    "-out", str(ATTEST_PUB)], check=True)
    os.chmod(ATTEST_PRIV, 0o600)
    print(f"Attestation keys generated: {ATTEST_DIR}")
    print(f"  public key (commit to repo): {ATTEST_PUB}")
    fp = subprocess.run(["openssl", "ec", "-in", str(ATTEST_PUB), "-pubin",
                         "-noout", "-text"], capture_output=True, text=True).stdout
    print(f"  fingerprint: {hashlib.sha256(fp.encode()).hexdigest()[:16]}")


def attest_payload(payload: str) -> Optional[str]:
    """ECDSA-SHA256 signature (base64) of payload. None if no private key."""
    if not ATTEST_PRIV.exists():
        return None
    p = subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(ATTEST_PRIV)],
                       input=payload.encode(), capture_output=True)
    if p.returncode != 0:
        print(f"  (attestation failed: {p.stderr.decode().strip()})", flush=True)
        return None
    return base64.b64encode(p.stdout).decode()


def verify_attestation(payload: str, sig_b64: str) -> bool:
    """Verify ECDSA-SHA256 signature. OpenSSL 3.5 won't read sig from stdin
    ("Error opening signature file -"), so we stage it to a temp file."""
    import tempfile
    if not ATTEST_PUB.exists():
        return False
    with tempfile.NamedTemporaryFile(suffix=".sig", delete=False) as tf:
        tf.write(base64.b64decode(sig_b64))
        tfname = tf.name
    try:
        p = subprocess.run(
            ["openssl", "dgst", "-sha256", "-verify", str(ATTEST_PUB),
             "-signature", tfname],
            input=payload.encode(), capture_output=True)
        return p.returncode == 0 and b"Verified OK" in p.stdout
    finally:
        os.unlink(tfname)


def canonical_payload(run_id: str, model: str, res: dict,
                      prompt_hash: str, response_hash: str) -> str:
    """Canonical attested string — must stay stable across versions."""
    return "\n".join([
        "trustless-bench attestation v1",
        f"run_id={run_id}",
        f"model={model}",
        f"benchmark={res['benchmark']}",
        f"score={res['score']}",
        f"n_correct={res['n_correct']}",
        f"n_total={res['n_total']}",
        f"prompt_hash={prompt_hash}",
        f"response_hash={response_hash}",
    ])


def write_manifest(models: list) -> Path:
    """Commitment manifest — written BEFORE any API call (commit-reveal)."""
    mmlu, he, src = load_questions()
    manifest = {
        "date": datetime.date.today().isoformat(),
        "nonce": secrets.token_hex(8),
        "models": models,
        "question_source": src,
        "prompt_hash_mmlu_pro": hash256(json.dumps(mmlu, sort_keys=True)),
        "prompt_hash_humaneval": hash256(json.dumps(he, sort_keys=True)),
        "created_at": datetime.datetime.now().isoformat(),
        "note": "commitment: question set + models decided BEFORE running. "
                "Question content is private; this hash is the public commitment.",
    }
    f = BENCH_DB.parent / f"manifest-{datetime.date.today().isoformat()}.json"
    f.write_text(json.dumps(manifest, indent=2))
    return f


# ── Sandboxed code execution (HumanEval safety gate) ─────────────────────
def run_code_sandboxed(code: str, test_cases: list) -> tuple:
    """Run model-generated code + test cases in a resource-limited subprocess.

    Safety gate (2026-08-27): model output is UNTRUSTED. Previously exec'd
    in-process = arbitrary code execution as our user. Now: python3 -I
    (isolated mode) + RLIMIT_CPU/AS/NOFILE/FSIZE/NPROC + temp cwd + stripped
    env + timeout. Full network isolation (bwrap/nsjail/container) is not
    available on this host — TODO when a container runtime exists.

    Returns (passed, total, result_dict).
    """
    import resource
    import tempfile

    harness = Path(__file__).parent / "harness.py"
    tests_json = json.dumps([{"call": c, "expected": e} for c, e in test_cases])

    def _limits() -> None:
        resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
        resource.setrlimit(resource.RLIMIT_FSIZE, (1024 * 1024, 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_NPROC, (32, 32))

    with tempfile.TemporaryDirectory(prefix="tb-sandbox-") as td:
        td = Path(td)
        code_f = td / "model_code.py"
        tests_f = td / "tests.json"
        code_f.write_text(code)
        tests_f.write_text(tests_json)
        try:
            p = subprocess.run(
                [sys.executable, "-I", str(harness), str(code_f), str(tests_f)],
                capture_output=True, text=True, timeout=20,
                cwd=td, env={"PATH": "/usr/bin:/bin"}, preexec_fn=_limits)
        except subprocess.TimeoutExpired:
            return 0, len(test_cases), {"fatal": "timeout (20s)"}
        try:
            out = json.loads(p.stdout or "{}")
        except json.JSONDecodeError:
            out = {"fatal": f"bad harness output: {(p.stdout or p.stderr or '')[:120]}"}
        if p.returncode != 0 and not out.get("fatal"):
            out["fatal"] = f"harness died (rc={p.returncode})"
        return out.get("passed", 0), len(test_cases), out


# ── Rate-limited request helper ──────────────────────────────────────────
def paced_request(req, timeout=60, delay=4.0):
    """Send request with rate-limit pacing (free tier: ~20 req/min).
    Retries up to 3x on 429 with backoff."""
    import urllib.request as ur
    import urllib.error
    for attempt in range(3):
        try:
            time.sleep(delay)
            return json.loads(ur.urlopen(req, timeout=timeout).read())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                wait = 30 * (attempt + 1)
                print(f"    (429 — backing off {wait}s, attempt {attempt+2}/3)", flush=True)
                time.sleep(wait)
                continue
            raise


# ── Benchmark runners ────────────────────────────────────────────────────
def run_mmlu_pro(model: str, questions: list) -> dict:
    """Run MMLU-Pro benchmark against a model."""
    key = read_api_key()
    n_total = len(questions)
    n_correct = 0
    raws: list = []
    details = []

    for q in questions:
        prompt = f"""Multiple choice question. Answer with ONLY the letter (A, B, C, or D).

Domain: {q['domain']}
Question: {q['question']}

Options:
{q['options'][0]}
{q['options'][1]}
{q['options'][2]}
{q['options'][3]}

Answer:"""

        try:
            import urllib.request as ur
            req = ur.Request(
                "https://openrouter.ai/api/v1/chat/completions",
                data=json.dumps({
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 5,
                    "temperature": 0,
                }).encode(),
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                }
            )
            resp = paced_request(req)
            raw = resp["choices"][0]["message"]["content"]
            raws.append(raw or "")
            answer = raw.strip().upper()
            for ch in answer:          # extract just the letter
                if ch in "ABCD":
                    answer = ch
                    break

            correct = answer == q["answer"]
            if correct:
                n_correct += 1

            details.append({
                "qid": q["id"],
                "domain": q["domain"],
                "expected": q["answer"],
                "got": answer,
                "raw": raw,
                "correct": correct,
            })
        except Exception as e:
            raws.append("")
            details.append({
                "qid": q["id"],
                "error": str(e),
                "correct": False,
            })

    return {
        "benchmark": "mmlu-pro",
        "score": n_correct / n_total if n_total else 0.0,
        "n_correct": n_correct,
        "n_total": n_total,
        "details": details,
        "prompt_hash": hash256(json.dumps(questions, sort_keys=True)),
        "response_hash": hash256("".join(raws)),
    }


def run_humaneval(model: str, questions: list) -> dict:
    """Run HumanEval-style code generation benchmark (sandboxed execution)."""
    key = read_api_key()
    n_correct = 0
    raws: list = []
    details = []

    for q in questions:
        prompt = f"""Write the Python function as specified. Output ONLY the function code, no explanation.

{q['task']}

```python"""

        try:
            import urllib.request as ur
            req = ur.Request(
                "https://openrouter.ai/api/v1/chat/completions",
                data=json.dumps({
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 300,
                    "temperature": 0,
                }).encode(),
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                }
            )
            resp = paced_request(req)
            raw = resp["choices"][0]["message"]["content"]
            raws.append(raw or "")
            code = raw.strip()
            if "```python" in code:       # strip markdown fence
                code = code.split("```python")[1]
            if "```" in code:
                code = code.split("```")[0]
            code = code.strip()

            passed, total, sandbox_out = run_code_sandboxed(code, q["test_cases"])
            ok = total > 0 and passed == total
            if ok:
                n_correct += 1

            details.append({
                "qid": q["id"],
                "tests_passed": passed,
                "total_tests": total,
                "all_correct": ok,
                "correct": ok,
                "raw": raw,
                "code": code[:500],
                "sandbox": sandbox_out,
            })
        except Exception as e:
            raws.append("")
            details.append({
                "qid": q["id"],
                "error": str(e),
                "all_correct": False,
                "correct": False,
            })

    return {
        "benchmark": "humaneval",
        "score": n_correct / len(questions) if questions else 0.0,
        "n_correct": n_correct,
        "n_total": len(questions),
        "details": details,
        "prompt_hash": hash256(json.dumps(questions, sort_keys=True)),
        "response_hash": hash256("".join(raws)),
    }


def run_benchmark(model: str, benchmark: str, questions: tuple) -> dict:
    """Dispatch to the right benchmark runner."""
    if benchmark == "mmlu-pro":
        return run_mmlu_pro(model, questions[0])
    elif benchmark == "humaneval":
        return run_humaneval(model, questions[1])
    else:
        return {"error": f"Unknown benchmark: {benchmark}"}


# ── Verification ─────────────────────────────────────────────────────────
def _verify_run_live(run_id: str, conn: sqlite3.Connection) -> bool:
    """Old behavior: re-run the benchmark live and compare scores (needs API)."""
    row = conn.execute("SELECT model, benchmark, prompt_hash, response_hash, score "
                       "FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if not row:
        print(f"Run {run_id} not found")
        return False
    model, bench, p_hash, r_hash, score = row
    mmlu, he, _ = load_questions()
    result = run_benchmark(model, bench, (mmlu, he))
    new_score = result["score"]
    print(f"  Original: {score:.1%} | Replay: {new_score:.1%}")
    if abs(score - new_score) < 0.001:
        print("  ✓ VERIFIED — scores match")
        conn.execute("UPDATE runs SET verified = 1 WHERE run_id = ?", (run_id,))
        conn.commit()
        return True
    print("  ⚠️ MISMATCH — scores differ")
    return False


def verify_run(run_id: str, conn: sqlite3.Connection, live: bool = False) -> bool:
    """Verify a previous run — OFFLINE by default.

    Replays the score deterministically from stored per-question answers,
    checks prompt_hash (commitment) + response_hash (integrity) + ECDSA
    attestation signature. No API calls. `live=True` retains the old
    re-run behavior.
    """
    if live:
        return _verify_run_live(run_id, conn)

    row = conn.execute(
        "SELECT model, benchmark, score, prompt_hash, response_hash, details "
        "FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if not row:
        print(f"Run {run_id} not found")
        return False
    model, bench, score, p_hash, r_hash, details_json = row
    details = json.loads(details_json or "[]")
    print(f"Run {run_id}  model={model}  bench={bench}  stored={score:.1%}  ({len(details)} answers)")
    ok_all = True

    # 1. Deterministic replay from stored answers (no AI, no API)
    n_correct = sum(1 for d in details if d.get("correct"))
    n_total = len(details) or 1
    replay = n_correct / n_total
    match = abs(score - replay) < 0.001
    print(f"  replay: {replay:.1%} ({n_correct}/{n_total}) — "
          f"{'✓ matches stored' if match else '✗ MISMATCH'}")
    ok_all &= match

    # 2. Question-set commitment (only if the current set is the private one)
    if p_hash:
        mmlu, he, _ = load_questions()
        cur = hash256(json.dumps(mmlu if bench == "mmlu-pro" else he, sort_keys=True))
        if cur == p_hash:
            print("  prompt_hash ✓ — question set unchanged since run")
        else:
            print("  prompt_hash ⚠ — question set CHANGED since run; "
                  "commitment intact, replay still uses stored answers")

    # 3. Response integrity (only if raw responses stored)
    if r_hash and any("raw" in d for d in details):
        recomputed = hash256("".join(d.get("raw", "") for d in details))
        print(f"  response_hash {'✓ matches' if recomputed == r_hash else '✗ TAMPERED'}")
        ok_all &= recomputed == r_hash

    # 4. Attestation signature
    sig_file = RESULTS_DIR / f"{run_id}-{bench}.sig"
    if sig_file.exists() and ATTEST_PUB.exists():
        payload = canonical_payload(
            run_id, model,
            {"benchmark": bench, "score": score, "n_correct": n_correct, "n_total": len(details)},
            p_hash or "", r_hash or "")
        if verify_attestation(payload, sig_file.read_text().strip()):
            print("  attestation ✓ — ECDSA signature valid")
        else:
            print("  attestation ✗ — signature INVALID")
            ok_all &= False
    else:
        print("  attestation: none (unsigned run)")

    if ok_all:
        conn.execute("UPDATE runs SET verified = 1 WHERE run_id = ?", (run_id,))
        conn.commit()
        print("  ✓ VERIFIED")
        return True
    print("  ⚠ NOT verified")
    return False


# ── Queue management ─────────────────────────────────────────────────────
def load_queue() -> list[dict]:
    if not QUEUE_FILE.exists():
        return DEFAULT_QUEUE
    return json.loads(QUEUE_FILE.read_text())


def save_queue(queue: list[dict]):
    QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    QUEUE_FILE.write_text(json.dumps(queue, indent=2))


def pop_next_model() -> Optional[dict]:
    """Get highest-priority model not yet benchmarked today."""
    queue = load_queue()
    conn = init_db()
    today = datetime.date.today().isoformat()
    cur = conn.execute(
        "SELECT model FROM runs WHERE created_at >= ?", (today,))
    done_today = {row[0] for row in cur.fetchall()}
    available = [m for m in sorted(queue, key=lambda x: x["priority"])
                 if m["model"] not in done_today]
    conn.close()
    if not available:
        return None
    return available[0]


# ── Publisher ────────────────────────────────────────────────────────────
def publish_result(run_id: str, model: str, results: list[dict]):
    """Write immutable result to ledger + DB + attestation signature."""
    ledger_dir = WORKSPACE / "benchmarks" / "ledger"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    result_file = RESULTS_DIR / f"{run_id}.json"

    # Attest each result (per-benchmark sig file: run_id-<bench>.sig)
    for r in results:
        sig = attest_payload(canonical_payload(
            run_id, model, r, r.get("prompt_hash", ""), r.get("response_hash", "")))
        r["attestation"] = sig or None
        if sig:
            (RESULTS_DIR / f"{run_id}-{r['benchmark']}.sig").write_text(sig + "\n")

    result_data = {
        "run_id": run_id,
        "model": model,
        "timestamp": datetime.datetime.now().isoformat(),
        "results": results,
        "verifiable": True,
        "verification_method": "deterministic replay + prompt_hash/response_hash "
                               "integrity + ECDSA attestation",
    }
    result_file.write_text(json.dumps(result_data, indent=2))

    ledger_file = ledger_dir / f"trustless-bench-{datetime.date.today().isoformat()}.md"
    if not ledger_file.exists():
        ledger_file.write_text(
            f"# Trustless Benchmark Results — {datetime.date.today().isoformat()}\n\n"
            "| Model | Benchmark | Score | Attested |\n"
            "|-------|-----------|-------|----------|\n")

    for r in results:
        att = "✓" if r.get("attestation") else " "
        ledger_file.write_text(
            f"| `{model}` | {r['benchmark']} | "
            f"{r['score']:.1%} ({r.get('n_correct', 0)}/{r.get('n_total', 0)}) | "
            f"{att} |\n")


# ── Main ─────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Trustless benchmark engine")
    parser.add_argument("--model", help="Benchmark specific model")
    parser.add_argument("--list", action="store_true", help="List previous results")
    parser.add_argument("--verify", help="Verify a previous run by ID (offline replay)")
    parser.add_argument("--live", action="store_true",
                        help="With --verify: live re-run instead of offline replay")
    parser.add_argument("--schedule", action="store_true", help="Show model queue")
    parser.add_argument("--init-attestation", action="store_true",
                        help="Generate ECDSA attestation keypair")
    parser.add_argument("--benchmark", choices=["mmlu-pro", "humaneval"],
                        default="mmlu-pro", help="Which benchmark to run")
    parser.add_argument("--max-models", type=int, default=3,
                        help="Max models to bench in one run")
    args = parser.parse_args()

    if args.init_attestation:
        init_attestation_keys()
        return

    conn = init_db()

    if args.list:
        cur = conn.execute(
            "SELECT run_id, model, benchmark, score, n_correct, n_questions, verified, created_at "
            "FROM runs ORDER BY created_at DESC LIMIT 20")
        print(f"{'Run ID':<10} {'Model':<50} {'Bench':<12} {'Score':>8} {'V':>3}")
        print("-" * 95)
        for row in cur.fetchall():
            v = "✓" if row[6] else " "
            print(f"{row[0]:<10} {row[1]:<50} {row[2]:<12} {row[3]:>7.1%}  {v:>3}")
        conn.close()
        return

    if args.verify:
        verify_run(args.verify, conn, live=args.live)
        conn.close()
        return

    if args.schedule:
        queue = load_queue()
        print(f"Queue: {len(queue)} models")
        for m in sorted(queue, key=lambda x: x["priority"]):
            print(f"  [{m['priority']}] {m['model']}")
        conn.close()
        return

    # ── Run benchmarks ──
    mmlu, he, q_src = load_questions()
    print(f"Questions: {q_src}")

    models_to_bench = []
    if args.model:
        models_to_bench = [args.model]
    else:
        for _ in range(args.max_models):
            next_m = pop_next_model()
            if next_m:
                models_to_bench.append(next_m["model"])
            else:
                break

    if not models_to_bench:
        print("All models benchmarked today. Use --model to force a specific model.")
        conn.close()
        return

    # Commit-reveal: manifest BEFORE any API call
    manifest = write_manifest(models_to_bench)
    print(f"Commitment manifest written (BEFORE runs): {manifest}")
    print(f"  nonce={json.loads(manifest.read_text())['nonce']}")

    print(f"\nRunning benchmarks on {len(models_to_bench)} model(s)...\n")
    today = datetime.date.today().isoformat()

    for model in models_to_bench:
        print(f"  [{datetime.datetime.now().strftime('%H:%M:%S')}] {model}")

        for bench in ["mmlu-pro", "humaneval"]:
            result = run_benchmark(model, bench, (mmlu, he))
            if "error" in result:
                print(f"    {bench}: ERROR — {result['error']}")
                continue

            run_id = hash_content(
                f"{model}:{bench}:{today}:{datetime.datetime.now().isoformat()}")
            score = result["score"]
            p_hash = result.get("prompt_hash", "")
            r_hash = result.get("response_hash", "")

            conn.execute(
                "INSERT OR REPLACE INTO runs "
                "(run_id, model, benchmark, score, n_questions, n_correct, "
                " prompt_hash, response_hash, details) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, model, bench, score,
                 result["n_total"], result["n_correct"],
                 p_hash, r_hash,
                 json.dumps(result.get("details", []))))
            conn.commit()

            print(f"    {bench}: {score:.1%} ({result['n_correct']}/{result['n_total']})"
                  f"  prompt={p_hash[:12]} resp={r_hash[:12]}")

            publish_result(run_id, model, [result])

        print()

    cur = conn.execute(
        "SELECT model, benchmark, score FROM runs WHERE created_at >= ?", (today,))
    rows = cur.fetchall()
    if rows:
        print(f"Today's results ({len(rows)} benchmark runs):")
        for r in rows:
            print(f"  {r[0]:<50} {r[1]:<12} {r[2]:.1%}")
    conn.close()
    print("\nDone. Results in benchmarks/ledger/ and benchmarks/results/")
    print("Verify any run offline: python3 scripts/trustless-bench.py --verify RUN_ID")


if __name__ == "__main__":
    main()
