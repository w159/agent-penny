#!/usr/bin/env python3
"""
Operator tool: measure the real cosine-similarity distribution behind
cron.trend.similarity_threshold (see cron/trend_cluster_embed.py) against a
supplied set of real ticket summaries, so the default (0.58) can be tuned
against actual ConnectWise history later.

Makes NO ConnectWise API calls itself - the caller supplies a JSON or CSV
file of already-exported ticket summaries, grouped by which ones a human
has confirmed describe the same underlying issue ("cluster"). Only talks to
the local embedding endpoint (cron/trend_vectors.py's embed_texts).

Input shape (JSON): a list of {"id": ..., "cluster": "<label>", "summary": "..."}
objects. CSV: columns id,cluster,summary. Two tickets sharing a `cluster`
label are treated as a true intra-cluster pair; two tickets with different
labels are a true inter-cluster pair.

Usage:
    .venv/bin/python scripts/calibrate_trend_threshold.py tickets.json
    .venv/bin/python scripts/calibrate_trend_threshold.py tickets.csv --model bge-m3
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cron.trend_vectors import cosine_similarity, embed_texts  # noqa: E402


def _load_rows(path: Path) -> list[dict]:
    if path.suffix.lower() == ".json":
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise SystemExit(f"{path}: expected a JSON list of ticket objects")
        return raw

    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    raise SystemExit(f"{path}: unsupported extension, expected .json or .csv")


def _validate_rows(rows: list[dict]) -> list[dict]:
    cleaned = []
    for row in rows:
        cluster = str(row.get("cluster") or "").strip()
        summary = str(row.get("summary") or "").strip()
        if not cluster or not summary:
            continue
        cleaned.append({"id": row.get("id"), "cluster": cluster, "summary": summary})
    if len(cleaned) < 2:
        raise SystemExit("need at least 2 rows with both 'cluster' and 'summary' set")
    return cleaned


def _pairwise_scores(rows: list[dict], vectors: list) -> tuple[list[float], list[float]]:
    """Returns (intra_cluster_scores, inter_cluster_scores) over every
    unordered pair of rows."""
    intra, inter = [], []
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            score = cosine_similarity(vectors[i], vectors[j])
            if rows[i]["cluster"] == rows[j]["cluster"]:
                intra.append(score)
            else:
                inter.append(score)
    return intra, inter


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round(pct * (len(ordered) - 1))))
    return ordered[idx]


def _print_distribution(label: str, values: list[float]) -> None:
    if not values:
        print(f"{label}: no pairs")
        return
    print(
        f"{label}: n={len(values)} "
        f"min={min(values):.4f} median={statistics.median(values):.4f} "
        f"p90={_percentile(values, 0.90):.4f} max={max(values):.4f}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", type=Path, help="JSON or CSV file of {id, cluster, summary} ticket rows")
    parser.add_argument("--model", default="bge-m3", help="Ollama embedding model id (default: bge-m3)")
    parser.add_argument("--base-url", default="http://localhost:11434", help="Ollama base URL")
    args = parser.parse_args()

    rows = _validate_rows(_load_rows(args.input))
    vectors = embed_texts([r["summary"] for r in rows], model=args.model, base_url=args.base_url)

    intra, inter = _pairwise_scores(rows, vectors)
    print(f"{len(rows)} tickets, {len({r['cluster'] for r in rows})} clusters")
    _print_distribution("intra-cluster (should merge)", intra)
    _print_distribution("inter-cluster (should NOT merge)", inter)

    if intra and inter:
        gap_low, gap_high = max(inter), min(intra)
        if gap_high > gap_low:
            print(f"suggested threshold range: {gap_low:.4f} .. {gap_high:.4f}")
        else:
            print(
                f"WARNING: inter-cluster max ({gap_low:.4f}) overlaps intra-cluster "
                f"min ({gap_high:.4f}) - no threshold cleanly separates these pairs"
            )


if __name__ == "__main__":
    main()
