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
    is_near_duplicate, is_dedup_scoped, DEDUP_WINDOW_DAYS, normalize_action_type,
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
- "action_type": exactly one of {actions}. Classify by the UNDERLYING EVENT, \
not the specific verb the headline happens to use — different outlets \
describe the same kind of event differently, and near-duplicate coverage of \
one event must still land in the same bucket. Rule of thumb: if the article \
names a specific issuer, a specific dollar amount, AND a concrete \
transactional milestone (pricing, rate-setting, closing, settlement), it is \
"New Issuance" regardless of whether it says "sets", "announces", "prices", \
"closes", or "completes" — those are the same event type worded \
differently. Reserve "Other" for genuinely generic commentary that isn't \
tied to one specific, named transaction (sector overviews, trend pieces, \
market-wide observations). Separately: "assigns", "affirms", "raises", or \
"cuts" a rating map to the matching Rating Upgrade/Downgrade/Outlook Change \
type; "reports on", "publishes performance for", and "servicer update on" a \
deal all map to "Performance/Servicer Report".
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


def is_duplicate(pub_dt, title: str, fingerprints: list, window_days: int = DEDUP_WINDOW_DAYS) -> bool:
    """Near-duplicate check against a list of (published_dt, title) pairs.
    Only compares items whose published dates are close together — two
    unrelated deals that happen to use similar boilerplate language shouldn't
    collide just because they share a template, so date proximity is
    required in addition to headline similarity."""
    for other_dt, other_title in fingerprints:
        if pub_dt and other_dt and abs((pub_dt - other_dt).days) > window_days:
            continue
        if is_near_duplicate(title, other_title):
            return True
    return False


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


# Rolling window for the "is today quiet or busy" volume comparison.
VOLUME_BASELINE_DAYS = 30

NARRATIVE_PROMPT = """You write ONE outcome-oriented sentence for an ABS \
(asset-backed securities) portfolio surveillance team, covering today's batch \
of {count} new items below (already filtered to the last {days} days by \
publish date, matching the dashboard's default view). This sentence runs \
AFTER an "Action Needed" line and a volume-context line that already state \
explicitly whether there are downgrades or sample-portfolio matches today — \
do not repeat those facts verbatim, but you may reference them briefly if \
central to the day's story. Focus on OUTCOMES and WHAT IT MEANS, not a recap \
of what was filed — e.g. "no deterioration signals across CLO coverage" or \
"flagged rising delinquencies in subprime auto", not "several CLO ratings \
were published". Items below are already sorted by priority (downgrades and \
sample-portfolio matches first, then by relevance) — reflect that ordering \
in what you choose to lead with. Return ONLY the one sentence, no preamble, \
no quotes.

Items (relevance/sector/action/portfolio-matches/title):
{items}
"""


# The domain-scoped rating-agency feeds + expanded categories (RMBS, CMBS,
# credit card, student loan, esoteric) went live at this point - before it,
# only 6 narrower feeds existed. A baseline computed from data straddling
# this line compares today's volume under the new, much broader feed set
# against a rate collected under the old one - exactly the apples-to-oranges
# comparison that made ordinary post-expansion volume look artificially
# "busier than usual." Update this if the feed mix changes again materially.
FEED_EXPANSION_CUTOFF = datetime(2026, 7, 25, 17, 30, tzinfo=timezone.utc)
MIN_BASELINE_DAYS = 5  # need this many distinct post-cutoff days before trusting a ratio
BACKFILL_LAG_DAYS = 3  # published-to-ingested gap beyond this reads as "just discovered", not "just happened"
BACKFILL_FRACTION_THRESHOLD = 0.5  # if more than half of today's batch is laggy, say so


def compute_volume_baseline(days: int = VOLUME_BASELINE_DAYS):
    """Average items ingested per day over the trailing `days`, counted by
    ingested_at (not published date) so it's measuring the same thing as
    "today's count" does: pipeline throughput, not publish-date clustering
    (which the near-duplicate feeds' backfill discovery would otherwise
    contaminate for arbitrary past dates, not just today). Only counts days
    on/after FEED_EXPANSION_CUTOFF, and reports insufficient=True until
    there's enough post-cutoff history to trust a ratio."""
    if not OUTPUT_FILE.exists():
        return {"avg": None, "window_days": 0, "insufficient": True}
    now = datetime.now(timezone.utc)
    days_since_cutoff = max(1, (now - FEED_EXPANSION_CUTOFF).days)
    window = min(days, days_since_cutoff)

    daily_counts = {}
    for line in OUTPUT_FILE.read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        ingested = parse_published(r.get("ingested_at"))
        if not ingested or ingested < FEED_EXPANSION_CUTOFF:
            continue
        age_days = (now - ingested).days
        if 0 < age_days <= window:  # exclude "today" (age 0) from its own baseline
            key = ingested.date()
            daily_counts[key] = daily_counts.get(key, 0) + 1

    if days_since_cutoff < MIN_BASELINE_DAYS:
        return {"avg": None, "window_days": window, "insufficient": True}
    avg = sum(daily_counts.values()) / window
    return {"avg": avg, "window_days": window, "insufficient": False}


def detect_backfill_artifact(records: list) -> bool:
    """True if today's batch looks like a backlog catch-up (old published
    dates just now being discovered) rather than genuinely fresh same-day
    news - e.g. right after a feed expansion starts surfacing years of a
    rating agency's back catalog all at once."""
    if not records:
        return False
    laggy = 0
    for r in records:
        pub = parse_published(r.get("published"))
        ing = parse_published(r.get("ingested_at"))
        if not pub or not ing:
            continue
        if (ing - pub).days >= BACKFILL_LAG_DAYS:
            laggy += 1
    return (laggy / len(records)) >= BACKFILL_FRACTION_THRESHOLD


def build_action_line(records: list):
    """Deterministic, not LLM-generated — a PM needs to trust this line is
    always accurate, so severity/downgrade/portfolio-match detection is
    plain Python, not left to model discretion."""
    downgrades = [r for r in records if "downgrade" in (r.get("action_type") or "").lower()]
    portfolio_hits = [r for r in records if r.get("portfolio_matches")]
    if not downgrades and not portfolio_hits:
        return "🟢 No action needed today.", False

    parts = []
    if downgrades:
        n = len(downgrades)
        parts.append(f"{n} downgrade{'s' if n != 1 else ''} require{'s' if n == 1 else ''} review")
    if portfolio_hits:
        names = sorted({m for r in portfolio_hits for m in (r.get("portfolio_matches") or [])})
        n = len(portfolio_hits)
        deal_plural = "s" if len(names) != 1 else ""
        parts.append(
            f"{n} item{'s' if n != 1 else ''} touch{'es' if n == 1 else ''} your sample-portfolio "
            f"deal{deal_plural} {', '.join(names)} — demo holdings, not real positions"
        )
    return "🔴 Action Needed: " + "; ".join(parts) + ".", True


def build_action_items(records: list):
    """The specific downgrade/portfolio-match items behind the Action Needed
    line — a count alone ("3 downgrades") isn't actionable; a PM needs the
    issuer names and a link to click through to each one. Downgrades sort
    first, matching the priority ordering used elsewhere."""
    items = []
    seen_ids = set()
    for r in records:
        is_downgrade = "downgrade" in (r.get("action_type") or "").lower()
        portfolio_matches = r.get("portfolio_matches") or []
        if not is_downgrade and not portfolio_matches:
            continue
        if r["id"] in seen_ids:
            continue
        seen_ids.add(r["id"])
        items.append({
            "id": r["id"],
            "label": r.get("issuer") or r["title"],
            "link": r.get("link"),
            "is_downgrade": is_downgrade,
            "portfolio_matches": portfolio_matches,
        })
    items.sort(key=lambda x: 0 if x["is_downgrade"] else 1)
    return items


def build_volume_line(today_count: int, baseline_info: dict, is_backfill_like: bool):
    plural = "s" if today_count != 1 else ""
    backfill_note = (
        " — today's volume looks inflated by backlog discovery from the recent feed expansion, not organic activity"
        if is_backfill_like else ""
    )
    if baseline_info["insufficient"] or baseline_info["avg"] is None:
        return (f"{today_count} item{plural} today — {VOLUME_BASELINE_DAYS}-day baseline still building since the "
                f"July 25 feed expansion (comparison not yet meaningful){backfill_note}.")
    avg = baseline_info["avg"]
    if avg == 0:
        return f"{today_count} item{plural} today — no comparable baseline yet{backfill_note}."
    ratio = today_count / avg
    if ratio >= 1.5:
        tone = "busier than usual"
    elif ratio <= 0.5:
        tone = "quieter than usual"
    else:
        tone = "a typical day"
    return (f"{today_count} item{plural} today vs. {avg:.1f}/day recent average "
            f"({baseline_info['window_days']}-day window since the feed expansion) — {tone}{backfill_note}.")


def generate_daily_summary(client: Anthropic, records: list) -> dict:
    action_line, action_needed = build_action_line(records)
    action_items = build_action_items(records)
    baseline_info = compute_volume_baseline()
    is_backfill_like = detect_backfill_artifact(records)
    volume_line = build_volume_line(len(records), baseline_info, is_backfill_like)

    # Downgrades and portfolio-sample matches always lead, even if they're a
    # small fraction of today's volume — the narrative prompt is told this
    # ordering is deliberate and should shape what it leads with.
    priority_sorted = sorted(
        records,
        key=lambda r: (
            0 if "downgrade" in (r.get("action_type") or "").lower() else 1,
            0 if r.get("portfolio_matches") else 1,
            -r["relevance_score"],
        ),
    )
    lines = [
        f"- {r['relevance_score']}/5 | {r['sector']} | {r['action_type']} | "
        f"matches={r.get('portfolio_matches') or []} | {r['title']}"
        for r in priority_sorted[:40]
    ]
    prompt = NARRATIVE_PROMPT.format(count=len(records), days=SUMMARY_RECENCY_DAYS, items="\n".join(lines))
    resp = client.messages.create(model=MODEL, max_tokens=150, messages=[{"role": "user", "content": prompt}])
    narrative = resp.content[0].text.strip()

    return {
        "action_needed": action_needed,
        "action_line": action_line,
        "action_items": action_items,
        "volume_line": volume_line,
        "narrative": narrative,
    }


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
    duplicate_filtered = 0

    # Fingerprint index for near-duplicate detection, seeded from everything
    # already ingested (excluding SEC EDGAR and rating-agency domain-scoped
    # feeds — see is_dedup_scoped in feeds.py for why). New items get
    # appended as they're processed, so two near-duplicates fetched from
    # different feeds in the *same* run (the original bug report) are caught
    # too, not just cross-run repeats.
    fingerprints = []
    if OUTPUT_FILE.exists():
        for line in OUTPUT_FILE.read_text().splitlines():
            if not line.strip():
                continue
            existing = json.loads(line)
            if not is_dedup_scoped(existing.get("source_feed", "")):
                continue
            existing_dt = parse_published(existing.get("published")) or parse_published(existing.get("ingested_at"))
            fingerprints.append((existing_dt, existing["title"]))

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

            candidate_pub_dt = parse_published(published)
            if is_dedup_scoped(name) and is_duplicate(candidate_pub_dt, title, fingerprints):
                seen.add(iid)
                duplicate_filtered += 1
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
            record["action_type"] = normalize_action_type(record.get("action_type", ""), title, record.get("issuer"))
            record["action_group"] = classify_action_group(record.get("action_type", ""))
            record["source_signal"] = classify_source_signal(title, name)
            record["portfolio_matches"] = match_holdings(
                f"{title} {record.get('issuer') or ''} {record.get('summary') or ''}"
            )
            new_records.append(record)
            if is_dedup_scoped(name):
                fingerprints.append((candidate_pub_dt, title))
            seen.add(iid)

    if new_records:
        with OUTPUT_FILE.open("a") as f:
            for r in new_records:
                f.write(json.dumps(r) + "\n")
        save_seen(seen)

        recent_new = [r for r in new_records if is_recent_record(r, SUMMARY_RECENCY_DAYS)]
        if recent_new:
            try:
                parts = generate_daily_summary(client, recent_new)
                with SUMMARY_FILE.open("a") as f:
                    f.write(json.dumps({
                        "date": datetime.now(timezone.utc).date().isoformat(),
                        "item_count": len(recent_new),
                        "generated_at": datetime.now(timezone.utc).isoformat(),
                        **parts,
                    }) + "\n")
            except Exception as e:
                print(f"[warn] failed to generate daily summary: {e}", file=sys.stderr)
        else:
            print(f"[info] {len(new_records)} new item(s) ingested, but none published within "
                  f"the last {SUMMARY_RECENCY_DAYS} days — skipping summary update this run.")

    # Keep the frontend's copy of the sample holdings list in sync (no API cost).
    HOLDINGS_FILE.write_text(json.dumps(SAMPLE_HOLDINGS, indent=2))

    print(f"\n{len(new_records)} new item(s) processed ({filtered_out} skipped by keyword pre-filter, "
          f"{duplicate_filtered} skipped as near-duplicates, no API cost).")
    flagged = [r for r in new_records if r.get("watchlist_sectors")]
    if flagged:
        print(f"{len(flagged)} matched watchlist sectors:")
        for r in flagged:
            print(f"  [{r['relevance_score']}/5] ({', '.join(r['watchlist_sectors'])}) {r['title'][:90]}")
    if new_records:
        print(f"\nFull structured output appended to {OUTPUT_FILE.name}")


if __name__ == "__main__":
    main()
