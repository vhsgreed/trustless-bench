#!/usr/bin/env python3
"""fetch-public-questions.py — regenerate benchmarks/questions.public.json.

Public benchmark question set for the open trustless-bench repo. Ships with
the repo so CI/nightly/demo runs use a REAL public set (not 3 hand-written
examples) while staying clearly labeled: these questions are public and
models may have seen them during training, so scores are comparable to
published leaderboards but NOT contamination-free.

Source: tinyBenchmarks/tinyMMLU (HF datasets-server API)
  https://huggingface.co/datasets/tinyBenchmarks/tinyMMLU
  License: MIT. Citation: Polo et al. "tinyBenchmarks: evaluating LLMs with
  fewer examples" (2024), arXiv:2402.14992.
  tinyMMLU = 100-question subset of cais/mmlu designed to approximate the
  full 14k-question MMLU score.

Output schema (matches engine's mmlu-pro question format):
  { "id": str, "domain": str, "question": str,
    "options": ["A) ...", "B) ...", "C) ...", "D) ..."], "answer": "A"|"B"|"C"|"D" }

Usage:
  python3 scripts/fetch-public-questions.py [--out benchmarks/questions.public.json]
"""
import argparse
import json
import sys
import urllib.request

API = ("https://datasets-server.huggingface.co/rows"
       "?dataset=tinyBenchmarks/tinyMMLU&config=all&split=test"
       "&offset=0&length=100")
SOURCE = "https://huggingface.co/datasets/tinyBenchmarks/tinyMMLU"
LICENSE = "MIT"
CITATION = "Polo et al., tinyBenchmarks (2024), arXiv:2402.14992"


def fetch() -> list[dict]:
    req = urllib.request.Request(API, headers={"User-Agent": "trustless-bench-fetch/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.load(r)
    rows = data.get("rows", [])
    if len(rows) != 100:
        raise RuntimeError(f"expected 100 rows, got {len(rows)}")
    return rows


def convert(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        row = r["row"]
        choices = row["choices"]
        if len(choices) != 4:
            raise RuntimeError(f"row {r['row_idx']}: expected 4 choices")
        out.append({
            "id": f"tinymmlu-{r['row_idx']:03d}",
            "domain": row["subject"],
            "question": row["question"],
            "options": [f"{chr(65+i)}) {c}" for i, c in enumerate(choices)],
            # ClassLabel comes back as int 0-3 from datasets-server; map to letter
            "answer": chr(65 + int(row["answer"])),
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="benchmarks/questions.public.json")
    args = ap.parse_args()

    rows = fetch()
    questions = convert(rows)
    doc = {
        "_meta": {
            "name": "tinyMMLU (public subset for CI/demo; NOT contamination-free)",
            "source": SOURCE,
            "license": LICENSE,
            "citation": CITATION,
            "n": len(questions),
            "note": "Public set: ships with repo, used when private questions.json "
                    "is absent (CI/nightly/demo). Scores are comparable to published "
                    "leaderboards; models may have seen these questions.",
        },
        "mmlu_pro": questions,
    }
    with open(args.out, "w") as f:
        json.dump(doc, f, indent=2, ensure_ascii=False)
    print(f"wrote {len(questions)} questions → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())