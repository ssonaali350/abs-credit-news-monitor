#!/usr/bin/env python3
"""One-off migration: adds action_group/source_signal/portfolio_matches to
records already in structured_news.jsonl, computed the same way ingest.py
computes them for new records. Pure keyword logic — no API calls, no cost.
Safe to re-run any time the classification rules in feeds.py change.
"""
import json
from pathlib import Path

from feeds import (
    classify_action_group, classify_source_signal, match_holdings, SAMPLE_HOLDINGS,
    normalize_action_type,
)

BASE_DIR = Path(__file__).parent
OUTPUT_FILE = BASE_DIR / "structured_news.jsonl"
HOLDINGS_FILE = BASE_DIR / "sample_holdings.json"


def main():
    records = [json.loads(l) for l in OUTPUT_FILE.read_text().splitlines() if l.strip()]
    for r in records:
        r["action_type"] = normalize_action_type(r.get("action_type", ""), r.get("title", ""))
        r["action_group"] = classify_action_group(r.get("action_type", ""))
        r["source_signal"] = classify_source_signal(r.get("title", ""), r.get("source_feed", ""))
        r["portfolio_matches"] = match_holdings(
            f"{r.get('title','')} {r.get('issuer') or ''} {r.get('summary') or ''}"
        )
    with OUTPUT_FILE.open("w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    HOLDINGS_FILE.write_text(json.dumps(SAMPLE_HOLDINGS, indent=2))
    print(f"Backfilled {len(records)} records.")


if __name__ == "__main__":
    main()
