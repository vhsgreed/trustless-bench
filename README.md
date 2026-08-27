# trustless-bench

Oracle-verifiable AI model evaluation. Benchmarks models via OpenRouter and
produces results anyone can verify **without trusting the runner** — and
without revealing the question sets, prompts, or methodology.

## Trust model (Phase 1: "attestable")

| Property | Mechanism |
|---|---|
| No cherry-picking | `manifest-<date>.json` written **before** each run — commits question-set hash + models + nonce |
| No tampering | every run stores `prompt_hash` + `response_hash` (SHA-256 of question set + raw model output) |
| Authenticity | ECDSA-SHA256 signature (P-256, openssl) over `(run_id, model, bench, score, prompt_hash, response_hash)` |
| Offline replay | `--verify RUN_ID` recomputes the score deterministically from stored answers — **no API calls** |

**No AI in the scoring path** — deterministic math only.

## Verify a run

```bash
# offline: replay score + check hashes + check signature
python3 trustless-bench.py --verify RUN_ID

# example, attested 2026-08-27 (deepseek/deepseek-v4-flash, private question set)
python3 trustless-bench.py --verify 4aec6312dfa3daea   # mmlu-pro 25.0%
python3 trustless-bench.py --verify 2d03f2ff67215938   # humaneval 50.0%
```

Expected output:

```
  replay: 25.0% (2/8) — ✓ matches stored
  prompt_hash ✓ — question set unchanged since run
  response_hash ✓ matches
  attestation ✓ — ECDSA signature valid
  ✓ VERIFIED
```

Public key: `attestation/public.pem` (fingerprint `9333800df00795a5`).
Private key never leaves the runner (0600, gitignored).

## Run a benchmark

```bash
python3 trustless-bench.py --init-attestation   # one-time: generate keypair
python3 trustless-bench.py                      # next queued models (max 3/day)
python3 trustless-bench.py --model MODEL_ID     # specific model
python3 trustless-bench.py --list               # previous runs
python3 trustless-bench.py --verify RUN_ID      # offline verify
```

API key: `~/.openclaw/secrets/openrouter-key` or env `OR_KEY_FILE`.

## Question sets (private by design)

The real question set lives in `benchmarks/questions.json` (gitignored) —
only its SHA-256 commitment is public. The repo ships a small **example
question set** (embedded in `trustless-bench.py`) so the public engine runs
for CI/nightly/demo. Example-set results are labeled as such and are **not**
trusted scores. Secret questions also double as an anti-contamination
measure (public benchmark questions get absorbed into training data).

## Sandboxed code execution

HumanEval runs model-generated code in a resource-limited subprocess
(`python3 -I` + RLIMIT_CPU/AS/NOFILE/FSIZE/NPROC + temp cwd + stripped env
+ timeout). Model output is untrusted — it never executes in the runner's
process. Full network isolation (bwrap/nsjail/container) is a documented
TODO.

## Benchmarks

- **MMLU-Pro subset** — multiple choice, 8 domains, letter-only answers, temp 0
- **HumanEval subset** — Python code generation, executed against test cases

## Roadmap

- **Phase 1 (this)**: attestable — commitments, hashes, signatures, replay
- **Phase 2**: on-chain via Chainlink **CRE** (Chainlink Runtime Environment,
  successor to retiring Chainlink Functions) + multi-source aggregation
  (same benchmark across 3-5 providers, median + CI) + independent verifier
- **Phase 3**: ZKML (prove inference in zero knowledge) — research-grade

## License

MIT © 2026 vhsgreed
