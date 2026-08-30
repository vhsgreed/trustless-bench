# Changelog

## 1.1.0 (2026-08-30)

### Features

* network-isolated sandbox for HumanEval execution: `unshare --user --net`
  netns (verified: socket connect -> ENETUNREACH), with graceful fallback
  to the non-netns sandbox where unshare is blocked. Result dicts now
  include `isolation` label.

## 1.0.0 (2026-08-27)


### Features

* multi-source aggregation — --provider param, provider column, --aggregate (median/Wilson CI/MAD outliers); Phase 2 kickoff ([82f8baa](https://github.com/vhsgreed/trustless-bench/commit/82f8baaeede2d00081c272ee896f7f6fbf3d2977))
