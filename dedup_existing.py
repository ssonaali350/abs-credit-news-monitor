#!/usr/bin/env python3
"""One-off cleanup: removes near-duplicate articles about the same
underlying event that already ended up as separate records in
structured_news.jsonl (e.g. two different Google News feeds surfacing the
same story with slightly different wording). Keeps the earliest-ingested
copy of each duplicate cluster. Pure token-overlap logic — no API calls.

Only considers records from is_dedup_scoped() feeds (excludes SEC EDGAR and
rating-agency domain-scoped feeds — see feeds.py for why); records outside
that scope are always kept untouched. Safe to re-run any time.
"""
import json
from pathlib import Path

from feeds import dedup_tokens, is_near_duplicate, is_dedup_scoped, DEDUP_WINDOW_DAYS
from ingest import parse_published

BASE_DIR = Path(__file__).parent
OUTPUT_FILE = BASE_DIR / "structured_news.jsonl"


def main():
    records = [json.loads(l) for l in OUTPUT_FILE.read_text().splitlines() if l.strip()]
    records.sort(key=lambda r: r.get("ingested_at", ""))  # keep earliest-ingested on conflict

    kept, kept_meta, removed = [], [], []
    for r in records:
        if not is_dedup_scoped(r.get("source_feed", "")):
            kept.append(r)
            continue

        pub_dt = parse_published(r.get("published")) or parse_published(r.get("ingested_at"))
        is_dup = False
        for other_dt, other_title in kept_meta:
            if pub_dt and other_dt and abs((pub_dt - other_dt).days) > DEDUP_WINDOW_DAYS:
                continue
            if is_near_duplicate(r["title"], other_title):
                is_dup = True
                break
        if is_dup:
            removed.append(r)
        else:
            kept.append(r)
            kept_meta.append((pub_dt, r["title"]))

    with OUTPUT_FILE.open("w") as f:
        for r in kept:
            f.write(json.dumps(r) + "\n")

    print(f"Kept {len(kept)}, removed {len(removed)} near-duplicate(s):")
    for r in removed:
        print(f"  - [{r['source_feed']}] {r['title'][:90]}")


if __name__ == "__main__":
    main()
