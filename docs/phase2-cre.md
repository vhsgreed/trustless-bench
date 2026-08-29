# trustless-bench Phase 2 — On-chain verification (direct-tx v1, CRE v2)

Design doc. Status: DRAFT (sprint S3, 2026-08-29). Author: agent1,
review: Karl.

## Goal

Publish AI evaluation results on-chain so anyone can verify a benchmark
run without trusting the runner. v1: direct transactions to a registry
contract (no external dependencies beyond the chain). v2: Chainlink
Runtime Environment (CRE) for aggregation at scale. Extends Phase 1
(attestable, offline replay) to Phase 2 (public, tamper-evident,
multi-source).

## Problem being solved

Phase 1 proves integrity to anyone with the repo: manifest-before-run,
SHA-256 hashes, ECDSA signatures, deterministic offline replay. The gap:
the attestation lives in a local ledger. Trusting the result still means
trusting that the publisher ran the published process. Phase 2 moves the
attestation on-chain: a signed commitment that cannot be silently
retracted or edited, plus multiple independent runners agreeing on the
same benchmark.

## Design principles

1. Question sets stay OFF-chain (secret sets prevent training-data
   contamination, already in Phase 1).
2. What goes on-chain is minimal and self-contained: run_id, model,
   bench, score, prompt_hash, response_hash, signature, runner pubkey.
3. No AI in the verification path. On-chain logic checks signatures and
   hashes, never re-scores.
4. Multi-source aggregation: N independent runners, median + interquartile
   range. One compromised runner cannot move the aggregate.

## Chainlink CRE (what it is, why)

CRE = Chainlink Runtime Environment, the successor to Chainlink Functions.
It executes a request off-chain (in a trust-minimized runtime) and
delivers the result on-chain, removing the need for the contract to run
computation itself. Compared to Functions: lower cost, longer runtime
budgets, more languages. Relevant because the verification computation
(signature check over a small payload) is tiny, so the CRE request stays
cheap.

## On-chain payload

Each attested run publishes:

```
struct BenchResult {
  bytes32 runId;          // sha256(manifest content) first 32 bytes
  string model;           // e.g. "deepseek/deepseek-v4-flash"
  string bench;           // e.g. "mmlu-pro-subset"
  uint256 score;          // scaled integer (score * 10000)
  bytes32 promptHash;     // sha256(question set + prompt template)
  bytes32 responseHash;   // sha256(raw model outputs)
  bytes  sig;             // ECDSA-SHA256 over canonical payload
  address runner;         // runner identity (EOA whose key signed)
}
```

Plus per-run metadata (block timestamp, chain) supplied by the contract.

## Contract surface (v1)

```
contract BenchRegistry {
  event ResultPublished(bytes32 runId, string model, string bench,
                        uint256 score, address runner, uint256 ts);

  function publish(BenchResult calldata r) external;       // any runner
  function getResult(bytes32 runId) external view returns (BenchResult);
  function runnerCount(bytes32 runId) external view returns (uint);
  // aggregation
  function aggregate(bytes32 runId) external view returns (uint256 median,
                      uint256 iqr);
}
```

Aggregation can be on-chain (read all published results for a runId,
compute median in Solidity — small N) or via CRE (off-chain compute,
delivered on-chain). v1: on-chain median for N <= 15, CRE for larger.

## Runner registration

Optional but recommended: runners register a pubkey on-chain
(address -> pubkey hash). publish() then requires sig to verify against
the registered key. Without registration, sig is self-asserted (still
tamper-evident, just not sybil-resistant). Multi-source median is the
real protection; registration is defense in depth.

## The flow (end to end)

1. Runner commits: manifest written locally (phase 1, unchanged).
2. Runner executes benchmark locally (phase 1, unchanged).
3. Runner builds canonical payload + signs (phase 1, unchanged).
4. Runner publishes to BenchRegistry.publish() via CRE request
   (new: phase 2).
5. Anyone reads: getResult(runId) — full attestation on-chain.
6. With 3+ runners: aggregate(runId) -> median + IQR. A verifier can
   also replay offline (phase 1 --verify) to confirm the hashes match
   what is published.

## Multi-source aggregation (3-runner model)

- hub, workstation, compute each run the SAME bench (same question set,
  same models) and publish independently.
- Aggregation rule: median score per model per bench; report IQR as
  dispersion. Reject runs whose response_hash differs (someone used
  different questions = not the same benchmark).
- A runner going offline does not block the others (each publishes
  independently; aggregate reads what exists).

## Testnet plan

1. Deploy BenchRegistry on Sepolia testnet (public RPC, faucet ETH).
   No CRE required for v1.
2. Publish one real attested Phase 1 run via a direct transaction.
   (CRE wiring is a v2 task, only for aggregation at scale.)
3. Verify externally: raw scan of the contract shows runId, score,
   hashes, sig; offline --verify replays to the same score.
4. Two-runner test: hub + workstation publish the same bench; aggregate
   returns median; response_hash mismatch case tested (one runner with
   different questions gets excluded).

## Open questions — RESOLVED (Karl 18:36, agent1 recommendations accepted)

1. **Chain: Sepolia testnet first, mainnet later.** PoC proves the
   mechanism without spend. Mainnet = later decision post-validation.
2. **Runner registration: self-asserted sigs + median for v1.** No
   pubkey registry yet; we control all runners. Registration is v2 when
   untrusted runners join. Median already protects against one bad actor.
3. **Same question set: response_hash comparison is the proof.**
   Different secret sets → different hashes → excluded from aggregate.
   Documented as a known limitation (implicit, not explicit).
4. **Direct tx for publish; CRE for aggregation at scale.** Plain
   contract call for one runner publishing. CRE reserved for N > 15 /
   complex aggregation. v1 = direct tx.

## Success criteria

- [ ] BenchRegistry deployed on testnet
- [ ] One real attested run published and verified on-chain (score + hashes match offline replay)
- [ ] Two-runner median aggregation working
- [ ] Writeup: "Verifiable AI evaluation on-chain" (Medium + bsky + Show HN)

## Repo layout

```
trustless-bench/
  contracts/BenchRegistry.sol
  scripts/publish_cre.js      # CRE request builder (v2, optional)
  scripts/publish_tx.js       # direct-tx publisher
  scripts/aggregate.js
  docs/phase2-cre.md          # this doc
```
