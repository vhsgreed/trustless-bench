// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title BenchRegistry — on-chain registry for trustless-bench attestations
/// @notice Phase 2 of trustless-bench: publish AI evaluation attestations
///         on-chain so anyone can verify a run without trusting the runner.
/// @dev    Design resolved 2026-08-29 (Karl + agent1): Sepolia testnet v1,
///         self-asserted ECDSA sigs + median aggregation, response_hash as
///         question-set proof, direct-tx publish. No AI in verification.
contract BenchRegistry {
    struct BenchResult {
        bytes32 runId;        // sha256(manifest content) first 32 bytes
        string model;         // e.g. "deepseek/deepseek-v4-flash"
        string bench;         // e.g. "mmlu-pro-subset"
        uint256 score;        // score * 10000 (integer scale)
        bytes32 promptHash;   // sha256(question set + prompt template)
        bytes32 responseHash; // sha256(raw model outputs)
        bytes sig;            // ECDSA-SHA256 over canonical payload
        address runner;       // EOA that signed
        uint256 publishedAt;  // block.timestamp
    }

    /// @dev runId => array of published results (multiple runners)
    mapping(bytes32 => BenchResult[]) private _results;
    /// @dev runId => dedup guard: runner => published already?
    mapping(bytes32 => mapping(address => bool)) private _published;
    /// @dev Optional runner pubkey registry (v2). v1: self-asserted.
    mapping(address => bytes32) public runnerPubkeyHash;

    event ResultPublished(
        bytes32 indexed runId,
        string model,
        string bench,
        uint256 score,
        address indexed runner,
        uint256 ts
    );

    error AlreadyPublished(bytes32 runId, address runner);
    error EmptyRunId();
    error EmptyModel();
    error EmptyBench();
    error TooManyResults(uint256 count);
    error SigMismatch(bytes32 runId, address runner);

    /// @dev Canonical payload hashed before signature verification.
    ///      Mirrors trustless-bench.py canonical_payload() ordering.
    function canonicalPayloadHash(
        bytes32 runId,
        string calldata model,
        string calldata bench,
        uint256 score,
        bytes32 promptHash,
        bytes32 responseHash
    ) public pure returns (bytes32) {
        return keccak256(
            abi.encode(runId, model, bench, score, promptHash, responseHash)
        );
    }

    /// @dev Publish one attested run. v1: any EOA may publish (self-asserted
    ///      signature). Runner identity = msg.sender. Signature must match
    ///      msg.sender (defense in depth even without registry).
    function publish(
        bytes32 runId,
        string calldata model,
        string calldata bench,
        uint256 score,
        bytes32 promptHash,
        bytes32 responseHash,
        bytes calldata sig
    ) external {
        if (runId == bytes32(0)) revert EmptyRunId();
        if (bytes(model).length == 0) revert EmptyModel();
        if (bytes(bench).length == 0) revert EmptyBench();
        if (_published[runId][msg.sender]) {
            revert AlreadyPublished(runId, msg.sender);
        }

        // Verify signature: recover signer from canonical payload.
        bytes32 payloadHash = canonicalPayloadHash(
            runId, model, bench, score, promptHash, responseHash
        );
        bytes32 ethSigned = keccak256(
            abi.encodePacked("\x19Ethereum Signed Message:\n32", payloadHash)
        );
        address signer = _recoverSigner(ethSigned, sig);
        if (signer != msg.sender) revert SigMismatch(runId, msg.sender);

        _results[runId].push(BenchResult({
            runId: runId,
            model: model,
            bench: bench,
            score: score,
            promptHash: promptHash,
            responseHash: responseHash,
            sig: sig,
            runner: msg.sender,
            publishedAt: block.timestamp
        }));
        _published[runId][msg.sender] = true;

        emit ResultPublished(runId, model, bench, score, msg.sender,
                             block.timestamp);
    }

    /// @dev Number of published results for a runId.
    function resultCount(bytes32 runId) external view returns (uint256) {
        return _results[runId].length;
    }

    /// @dev Get a single result by index.
    function getResult(bytes32 runId, uint256 index)
        external view returns (BenchResult memory)
    {
        return _results[runId][index];
    }

    /// @dev Median score across published results for a runId (v1: on-chain,
    ///      small N). Returns (median, iqr) scaled by 10000.
    ///      Only results with identical responseHash count (same question set).
    function aggregate(bytes32 runId)
        external view returns (uint256 median, uint256 iqr)
    {
        BenchResult[] storage rs = _results[runId];
        uint256 n = rs.length;
        if (n == 0) return (0, 0);
        if (n > 15) revert TooManyResults(n); // v2: CRE for N > 15

        // Collect distinct response-hash groups; use the largest group.
        uint256[] memory scores = new uint256[](n);
        uint256 count = 0;
        bytes32 dominantHash = rs[0].responseHash;
        for (uint256 i = 0; i < n; i++) {
            if (rs[i].responseHash == dominantHash) {
                scores[count] = rs[i].score;
                count++;
            }
        }
        if (count == 0) return (0, 0);

        // Simple insertion sort (small N).
        for (uint256 i = 1; i < count; i++) {
            uint256 key = scores[i];
            uint256 j = i;
            while (j > 0 && scores[j - 1] > key) {
                scores[j] = scores[j - 1];
                j--;
            }
            scores[j] = key;
        }

        median = _median(scores, count);
        // IQR: Q3 - Q1 over the same sorted array.
        uint256 q1 = _percentile(scores, count, 25);
        uint256 q3 = _percentile(scores, count, 75);
        iqr = q3 > q1 ? q3 - q1 : 0;
    }

    /// @dev v2 hook: register a runner pubkey. No-op in v1 (self-asserted).
    function registerRunner(bytes32 pubkeyHash) external {
        runnerPubkeyHash[msg.sender] = pubkeyHash;
    }

    // --- internal helpers ---

    function _median(uint256[] memory arr, uint256 n)
        private pure returns (uint256)
    {
        if (n % 2 == 1) return arr[n / 2];
        return (arr[n / 2 - 1] + arr[n / 2]) / 2;
    }

    function _percentile(uint256[] memory arr, uint256 n, uint256 p)
        private pure returns (uint256)
    {
        if (n == 1) return arr[0];
        uint256 idx = (n * p) / 100;
        if (idx >= n) idx = n - 1;
        return arr[idx];
    }

    function _recoverSigner(bytes32 hash, bytes memory sig)
        private pure returns (address)
    {
        if (sig.length != 65) return address(0);
        bytes32 r;
        bytes32 s;
        uint8 v;
        assembly {
            r := mload(add(sig, 32))
            s := mload(add(sig, 64))
            v := byte(0, mload(add(sig, 96)))
        }
        if (v < 27) v += 27;
        return ecrecover(hash, v, r, s);
    }
}
