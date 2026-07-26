#!/usr/bin/env python3
"""Fetches real, live credit-market context for the dashboard's ticker bar —
genuinely real data, no sample/demo disclaimer needed (unlike the Holdings
table / KPI cards, which are illustrative).

All five series come from FRED's free CSV endpoint (fredgraph.csv) — no API
key required, verified working. CDX.NA.IG, CDX.NA.HY, AAA CLO spread, and
BSL CLO new-issue spread are NOT included: they're proprietary index/research
data (S&P DJI-licensed CDS indices; JPMorgan/Palmer Square/S&P CLO research)
with no legitimate free daily API found after checking — see conversation
history for the diligence. The ticker bar surfaces this gap explicitly
rather than silently omitting it.

Run manually or via the daily GitHub Actions workflow:
    python3 fetch_market_data.py
"""
import csv
import io
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen, Request

BASE_DIR = Path(__file__).parent
OUTPUT_FILE = BASE_DIR / "market_ticker.json"

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
# for simplicity; FRED's free endpoint has no rate limit that matters at
# this volume (4 requests/day).
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


FRED_HEADERS = {
    # Deliberately an honest, plainly-non-browser User-Agent, NOT a spoofed
    # browser string. Tested both ways: a Chrome-spoofing UA makes Cloudflare
    # (which fronts fred.stlouisfed.org) reject the request outright and
    # instantly (a TLS/JA3 fingerprint mismatch is a classic bot-detection
    # trigger — the request claims to be Chrome but the TLS handshake
    # doesn't match one), whereas this honest string works reliably. The
    # GitHub-Actions-specific timeouts this project hit are most likely
    # Cloudflare rate-limiting/blocking the shared runner IP range rather
    # than anything about the UA — if retries below don't resolve it,
    # switching to FRED's official API (api.stlouisfeed.org, needs a free
    # key) is the more robust next step.
    "User-Agent": "ABS-Credit-News-Monitor/1.0 (market-data-ticker)",
}


def fetch_series(series_id: str, retries: int = 2, timeout: int = 30):
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    last_error = None
    for attempt in range(retries + 1):
        try:
            req = Request(url, headers=FRED_HEADERS)
            with urlopen(req, timeout=timeout) as resp:
                text = resp.read().decode("utf-8")
            break
        except Exception as e:
            last_error = e
            if attempt < retries:
                time.sleep(3 * (attempt + 1))
    else:
        raise last_error
    rows = list(csv.reader(io.StringIO(text)))
    # rows: [["DATE", series_id], [date, value], ...] — FRED uses "." for missing days
    data_rows = [r for r in rows[1:] if len(r) == 2 and r[1] not in (".", "")]
    if not data_rows:
        return None
    latest = data_rows[-1]
    prior = data_rows[-2] if len(data_rows) > 1 else None
    return {
        "date": latest[0],
        "value": float(latest[1]),
        "prior_date": prior[0] if prior else None,
        "prior_value": float(prior[1]) if prior else None,
    }


def main():
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
