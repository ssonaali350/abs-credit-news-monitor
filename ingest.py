#!/usr/bin/env python3
"""ABS/credit news ingestion + structuring proof-of-concept.

Pulls new items from free public feeds (feeds.py), asks Claude to structure
each one into a fixed schema tailored to ABS portfolio surveillance, and
appends the results to structured_news.jsonl. Re-running only spends API
calls on items not already recorded in seen.json.

Usage:
    python3 ingest.py            # process new items, print summary
    python3 ingest.py --limit 5  # cap items per feed (useful while testing)
"""
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import feedparser
from anthropic import Anthropic
from dotenv import load_dotenv

from feeds import (
    FEEDS, SEC_USER_AGENT, KEYWORD_FILTERED_FEEDS, matches_abs_keywords, is_junk_title,
    classify_action_group, classify_source_signal, match_holdings, SAMPLE_HOLDINGS,
)

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")  # explicit path so cron (different cwd) still finds it

SEEN_FILE = BASE_DIR / "seen.json"
OUTPUT_FILE = BASE_DIR / "structured_news.jsonl"
SUMMARY_FILE = BASE_DIR / "daily_summaries.jsonl"
HOLDINGS_FILE = BASE_DIR / "sample_holdings.json"
MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

# Matches the dashboard's default "Recent" window (index.html's 7-day option),
# so the exec summary never describes items the default view doesn't show.
SUMMARY_RECENCY_DAYS = 7

SECTORS = [
    "Auto ABS", "CLO", "Solar/Renewable ABS", "RMBS", "CMBS",
    "Credit Card ABS", "Student Loan ABS", "Esoteric/Whole-Business ABS",
    "Consumer ABS", "Corporate Credit", "Other",
]
ACTION_TYPES = [
    "Rating Upgrade", "Rating Downgrade", "Outlook Change", "New Issuance",
    "Filing/Disclosure", "Performance/Servicer Report", "Spread/Market Move",
    "Regulatory", "Other",
]
WATCHLIST_SECTORS = ["subprime auto", "prime auto", "solar"]

STRUCTURE_PROMPT = """You structure raw news/filing headlines for an ABS \
(asset-backed securities) portfolio surveillance team. Given the item below, \
return ONLY a JSON object (no prose, no markdown fences) with these fields:

- "issuer": best-guess issuer/entity name, or null if not identifiable
- "sector": exactly one of {sectors}
- "action_type": exactly one of {actions}
- "relevance_score": integer 1-5, how relevant this is to an ABS surveillance/\
asset management audience (5 = directly affects a specific deal or asset class \
they'd hold, 1 = generic/unrelated)
- "watchlist_sectors": array, subset of {watchlist} that this item pertains to \
(empty array if none)
- "summary": one-sentence plain-English summary of what happened

Item:
Title: {title}
Source: {source}
Published: {published}
Snippet: {snippet}
"""


def load_seen() -> set:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()))
    return set()


def save_seen(seen: set) -> None:
    SEEN_FILE.write_text(json.dumps(sorted(seen)))


def item_id(link: str) -> str:
    return hashlib.sha256(link.encode()).hexdigest()[:16]


def parse_published(raw: str):
    """Parses RFC-822 (Google News) or ISO (SEC EDGAR) date strings. Returns
    a tz-aware datetime, or None if unparseable."""
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        pass
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def is_recent_record(record: dict, days: int) -> bool:
    dt = parse_published(record.get("published")) or parse_published(record.get("ingested_at"))
    if dt is None:
        return False
    return (datetime.now(timezone.utc) - dt) <= timedelta(days=days)


def structure_item(client: Anthropic, title: str, source: str, published: str, snippet: str) -> dict:
    prompt = STRUCTURE_PROMPT.format(
        sectors=SECTORS, actions=ACTION_TYPES, watchlist=WATCHLIST_SECTORS,
        title=title, source=source, published=published, snippet=snippet[:500],
    )
    resp = client.messages.create(
        model=MODEL,
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    text = resp.content[0].text.strip()
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start:end + 1])


def fetch_feed(name: str, url: str, limit: int):
    headers = {"User-Agent": SEC_USER_AGENT} if "sec.gov" in url else {}
    parsed = feedparser.parse(url, request_headers=headers)
    entries = parsed.entries[:limit] if limit else parsed.entries
    return entries


SUMMARY_PROMPT = """You write a single-sentence executive summary for an ABS \
(asset-backed securities) portfolio surveillance team, covering the batch of \
recent structured news items below (already filtered to the last {days} days \
by publish date, matching the dashboard's default view — do not describe \
anything as more recent or older than that). Prioritize the highest-relevance \
items. Exactly {match_count} of these items match the sample portfolio-watch \
list — only mention a portfolio-watch match if this number is greater than 0, \
and never name a deal as a portfolio match unless its own line below says \
matches=[...] with a non-empty list. Return ONLY the one sentence, no \
preamble, no quotes.

Items (relevance/sector/action/portfolio-matches/title):
{items}
"""


def generate_daily_summary(client: Anthropic, records: list) -> str:
    match_count = sum(1 for r in records if r.get("portfolio_matches"))
    lines = [
        f"- {r['relevance_score']}/5 | {r['sector']} | {r['action_type']} | "
        f"matches={r.get('portfolio_matches') or []} | {r['title']}"
        for r in sorted(records, key=lambda r: -r["relevance_score"])[:40]
    ]
    prompt = SUMMARY_PROMPT.format(
        days=SUMMARY_RECENCY_DAYS, match_count=match_count, items="\n".join(lines),
    )
    resp = client.messages.create(
        model=MODEL, max_tokens=150,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text.strip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=10, help="max items per feed")
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY not set. Add it to news_poc/.env (see .env.example).")

    client = Anthropic()
    seen = load_seen()
    new_records = []
    filtered_out = 0

    for name, url in FEEDS:
        try:
            entries = fetch_feed(name, url, args.limit)
        except Exception as e:
            print(f"[warn] failed to fetch {name}: {e}", file=sys.stderr)
            continue

        for entry in entries:
            link = entry.get("link", "")
            if not link:
                continue
            iid = item_id(link)
            if iid in seen:
                continue

            title = entry.get("title", "")
            published = entry.get("published", entry.get("updated", ""))
            snippet = entry.get("summary", "")

            if is_junk_title(title):
                seen.add(iid)
                filtered_out += 1
                continue

            if name in KEYWORD_FILTERED_FEEDS and not matches_abs_keywords(title):
                seen.add(iid)  # mark seen so we don't re-check it next run
                filtered_out += 1
                continue

            try:
                structured = structure_item(client, title, name, published, snippet)
            except Exception as e:
                print(f"[warn] failed to structure '{title[:60]}': {e}", file=sys.stderr)
                continue

            record = {
                "id": iid,
                "title": title,
                "link": link,
                "source_feed": name,
                "published": published,
                "ingested_at": datetime.now(timezone.utc).isoformat(),
                **structured,
            }
            record["action_group"] = classify_action_group(record.get("action_type", ""))
            record["source_signal"] = classify_source_signal(title, name)
            record["portfolio_matches"] = match_holdings(
                f"{title} {record.get('issuer') or ''} {record.get('summary') or ''}"
            )
            new_records.append(record)
            seen.add(iid)

    if new_records:
        with OUTPUT_FILE.open("a") as f:
            for r in new_records:
                f.write(json.dumps(r) + "\n")
        save_seen(seen)

        recent_new = [r for r in new_records if is_recent_record(r, SUMMARY_RECENCY_DAYS)]
        if recent_new:
            try:
                summary = generate_daily_summary(client, recent_new)
                with SUMMARY_FILE.open("a") as f:
                    f.write(json.dumps({
                        "date": datetime.now(timezone.utc).date().isoformat(),
                        "summary": summary,
                        "item_count": len(recent_new),
                        "generated_at": datetime.now(timezone.utc).isoformat(),
                    }) + "\n")
            except Exception as e:
                print(f"[warn] failed to generate daily summary: {e}", file=sys.stderr)
        else:
            print(f"[info] {len(new_records)} new item(s) ingested, but none published within "
                  f"the last {SUMMARY_RECENCY_DAYS} days — skipping summary update this run.")

    # Keep the frontend's copy of the sample holdings list in sync (no API cost).
    HOLDINGS_FILE.write_text(json.dumps(SAMPLE_HOLDINGS, indent=2))

    print(f"\n{len(new_records)} new item(s) processed ({filtered_out} skipped by keyword pre-filter, no API cost).")
    flagged = [r for r in new_records if r.get("watchlist_sectors")]
    if flagged:
        print(f"{len(flagged)} matched watchlist sectors:")
        for r in flagged:
            print(f"  [{r['relevance_score']}/5] ({', '.join(r['watchlist_sectors'])}) {r['title'][:90]}")
    if new_records:
        print(f"\nFull structured output appended to {OUTPUT_FILE.name}")


if __name__ == "__main__":
    main()
