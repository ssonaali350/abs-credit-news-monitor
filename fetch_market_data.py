#!/usr/bin/env python3
"""Fetches real, live credit-market context for the dashboard's ticker bar —
genuinely real data, no sample/demo disclaimer needed (unlike the Holdings
table / KPI cards, which are illustrative).

Uses FRED's official API (api.stlouisfed.org), not the fredgraph.csv
graph-rendering endpoint this used to scrape. That endpoint worked fine when
tested locally but timed out 100% of the time when run from GitHub Actions
(confirmed across two separate runs, with and without a browser-spoofed
User-Agent) — almost certainly Cloudflare (which fronts fred.stlouisfed.org)
blocking/throttling the shared GitHub Actions runner IP range, which a
client-side header change can't work around. The official API is a proper
authenticated endpoint, confirmed to respond instantly even to a malformed
request (no timeout), which is the behavior of a real API rather than a
bot-protected page — the more robust, sanctioned path.

Needs a free FRED_API_KEY (https://fred.stlouisfed.org/docs/api/api_key.html)
set as an environment variable.

CDX.NA.IG, CDX.NA.HY, AAA CLO spread, and BSL CLO new-issue spread are NOT
included: they're proprietary index/research data (S&P DJI-licensed CDS
indices; JPMorgan/Palmer Square/S&P CLO research) with no legitimate free
daily API found after checking. The ticker bar surfaces this gap explicitly
rather than silently omitting it.

Run manually or via the daily GitHub Actions workflow:
    FRED_API_KEY=... python3 fetch_market_data.py
"""
import json
import os
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen, Request

from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")  # so this works standalone too, not just via ingest.py's side effect

OUTPUT_FILE = BASE_DIR / "market_ticker.json"
FRED_API_KEY = os.environ.get("FRED_API_KEY", "")
FRED_API_BASE = "https://api.stlouisfed.org/fred/series/observations"

# (FRED series ID, display label, unit) — shown in the top ticker bar
SERIES = [
    ("SOFR", "SOFR", "%"),
    ("BAMLC0A0CM", "IG Corporate OAS", "%"),
    ("BAMLH0A0HYM2", "HY Corporate OAS", "%"),
    ("DGS10", "10Y Treasury", "%"),
    ("DGS2", "2Y Treasury", "%"),
]

# Treasury yield curve points (FRED series ID, tenor label) — plotted as a
# line chart so the curve shape (normal/flat/inverted) is visible at a
# glance. DGS2 and DGS10 overlap with SERIES above but are re-fetched here
# for simplicity; the official API's free tier (120 req/min) has no rate
# limit that matters at this volume (9 requests/day).
YIELD_CURVE_SERIES = [
    ("DGS2", "2Y"),
    ("DGS5", "5Y"),
    ("DGS10", "10Y"),
    ("DGS30", "30Y"),
]

# Explicitly not available for free — shown in the UI so the gap is
# transparent rather than silently missing.
UNAVAILABLE = [
    {"label": "CDX.NA.IG", "reason": "Proprietary S&P DJI-licensed CDS index — no free daily source found"},
    {"label": "CDX.NA.HY", "reason": "Proprietary S&P DJI-licensed CDS index — no free daily source found"},
    {"label": "AAA CLO spread", "reason": "JPMorgan/Palmer Square CLO index research — no free daily API found"},
    {"label": "BSL CLO new-issue spread", "reason": "S&P/JPMorgan CLO research reports only — no free daily API found"},
]


def fetch_series(series_id: str, retries: int = 1, timeout: int = 15):
    params = {
        "series_id": series_id,
        "api_key": FRED_API_KEY,
        "file_type": "json",
        "sort_order": "desc",
        "limit": "5",  # a few, in case the latest day or two is "." (not yet published)
    }
    url = f"{FRED_API_BASE}?{urllib.parse.urlencode(params)}"
    last_error = None
    for attempt in range(retries + 1):
        try:
            req = Request(url, headers={"User-Agent": "ABS-Credit-News-Monitor/1.0 (market-data-ticker)"})
            with urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except Exception as e:
            last_error = e
            if attempt < retries:
                time.sleep(2)
    else:
        raise last_error

    observations = [o for o in data.get("observations", []) if o.get("value") not in (".", "", None)]
    if not observations:
        return None
    latest = observations[0]
    prior = observations[1] if len(observations) > 1 else None
    return {
        "date": latest["date"],
        "value": float(latest["value"]),
        "prior_date": prior["date"] if prior else None,
        "prior_value": float(prior["value"]) if prior else None,
    }


def main():
    if not FRED_API_KEY:
        sys.exit("FRED_API_KEY not set. Get a free key at "
                 "https://fred.stlouisfed.org/docs/api/api_key.html and add it to news_poc/.env "
                 "(local) or as a GitHub Actions secret (cloud).")

    items = []
    for series_id, label, unit in SERIES:
        try:
            point = fetch_series(series_id)
        except Exception as e:
            print(f"[warn] failed to fetch {series_id}: {e}", file=sys.stderr)
            continue
        if not point:
            print(f"[warn] no data returned for {series_id}", file=sys.stderr)
            continue
        change = None
        if point["prior_value"] is not None:
            change = round(point["value"] - point["prior_value"], 4)
        items.append({
            "series_id": series_id,
            "label": label,
            "unit": unit,
            "value": point["value"],
            "as_of": point["date"],
            "change": change,
        })

    yield_curve = []
    for series_id, tenor in YIELD_CURVE_SERIES:
        try:
            point = fetch_series(series_id)
        except Exception as e:
            print(f"[warn] failed to fetch {series_id}: {e}", file=sys.stderr)
            continue
        if not point:
            print(f"[warn] no data returned for {series_id}", file=sys.stderr)
            continue
        yield_curve.append({"tenor": tenor, "value": point["value"], "as_of": point["date"]})

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "items": items,
        "yield_curve": yield_curve,
        "unavailable": UNAVAILABLE,
    }
    OUTPUT_FILE.write_text(json.dumps(output, indent=2))
    print(f"Wrote {len(items)} live series to {OUTPUT_FILE.name}")
    for it in items:
        arrow = "" if it["change"] is None else ("▲" if it["change"] > 0 else "▼" if it["change"] < 0 else "·")
        print(f"  {it['label']}: {it['value']}{it['unit']} {arrow} ({it['as_of']})")
    print("Yield curve:", ", ".join(f"{p['tenor']}={p['value']}%" for p in yield_curve))


if __name__ == "__main__":
    main()
