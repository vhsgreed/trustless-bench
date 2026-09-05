# Changelog

## [1.1.0](https://github.com/vhsgreed/trustless-bench/compare/v1.0.0...v1.1.0) (2026-09-05)


### Features

* network-isolated sandbox (unshare user+net ns) for HumanEval ([51c4926](https://github.com/vhsgreed/trustless-bench/commit/51c49269913324757149d3f6511338fd67a8cfc8))
* score-steered nightly model queue ([d5d0a85](https://github.com/vhsgreed/trustless-bench/commit/d5d0a85ea18c8284781ac379a630b3c86f1e719e))


### Bug Fixes

* **attest:** make nightly runs attested + verified (CRE-pushable) ([9ce0c2e](https://github.com/vhsgreed/trustless-bench/commit/9ce0c2e21980d2f2272d30fecc77f33dfaa921e9))
* **bench:** honest humaneval scoring — never use reasoning scratchpad as code ([b428468](https://github.com/vhsgreed/trustless-bench/commit/b428468d14e84597f0175916696a54111f5e7273))
* **engine:** sync 3 known bugs from workspace — provider NameError, reasoning-model content fallback (max_tokens 128), pop_next_model queue consumption ([28f48ca](https://github.com/vhsgreed/trustless-bench/commit/28f48cab036578645bcc3eccc11e6be424bb06b7))
* **nightly:** set WORKSPACE=$PWD so db/results land in-repo (commit step was finding nothing) ([49d2d69](https://github.com/vhsgreed/trustless-bench/commit/49d2d69d49e442454ae08768da373a4a7dd78a32))

## 1.1.0 (2026-08-30)

### Features

* network-isolated sandbox for HumanEval execution: `unshare --user --net`
  netns (verified: socket connect -> ENETUNREACH), with graceful fallback
  to the non-netns sandbox where unshare is blocked. Result dicts now
  include `isolation` label.

## 1.0.0 (2026-08-27)


### Features

* multi-source aggregation — --provider param, provider column, --aggregate (median/Wilson CI/MAD outliers); Phase 2 kickoff ([82f8baa](https://github.com/vhsgreed/trustless-bench/commit/82f8baaeede2d00081c272ee896f7f6fbf3d2977))
