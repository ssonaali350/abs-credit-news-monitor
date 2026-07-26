"""Free, public feed sources for ABS/credit-market news.

SEC EDGAR "getcurrent" feeds are official and near real-time (filings appear
within minutes). Google News RSS covers rating-agency and market news that
isn't filed with the SEC. Both require no API key.

Rating-agency coverage is domain-scoped (Google `site:` search) so it pulls
directly from S&P Global, KBRA, Fitch, and Moody's rather than generic
aggregator reposts. One broader, non-scoped feed is kept as lower-priority
"market color" — it's still useful for context but gets tagged low-signal by
classify_source_signal below. Coverage is topic/category-driven (sector +
rating-agency domains), never a static issuer list — new/unfamiliar issuers
are scored by the LLM on content, not filtered out for being unrecognized.
"""

# SEC requires a descriptive User-Agent with contact info on every request.
SEC_USER_AGENT = "ABS News PoC jena.so@northeastern.edu"

# Google News `site:` search restricts results to these domains' own
# published research/ratings pages (verified: returns on-domain S&P Global /
# KBRA / Fitch Ratings / Moody's articles, not generic reposts).
RATING_AGENCY_SITES = "(site:spglobal.com OR site:kbra.com OR site:fitchratings.com OR site:moodys.com)"

# One query term per structured-products category. Kept broad/topical
# (sector + generic securitization vocabulary), not tied to any issuer name,
# so coverage doesn't depend on recognizing a given deal sponsor.
CATEGORY_QUERIES = {
    "Auto ABS": "auto loan ABS",
    "Solar/Renewable ABS": "solar ABS securitization",
    "CLO": "CLO",
    "RMBS": '(RMBS OR "credit risk transfer" OR CAS OR STACR OR "non-QM")',
    "CMBS": '(CMBS OR SASB OR "K-series")',
    "Credit Card ABS": '"credit card" ABS',
    "Student Loan ABS": '"student loan" ABS',
    "Esoteric/Whole-Business ABS": '(timeshare OR "aircraft lease" OR "container lease" OR "franchise royalty" OR "whole business") securitization',
}


def _gnews_url(query: str) -> str:
    import urllib.parse
    return "https://news.google.com/rss/search?q=" + urllib.parse.quote(query) + "&hl=en-US&gl=US&ceid=US:en"


FEEDS = [
    (
        "SEC EDGAR - ABS-EE filings",
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=ABS-EE&company=&dateb=&owner=include&count=100&output=atom",
    ),
    (
        "SEC EDGAR - 8-K filings",
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&company=&dateb=&owner=include&count=100&output=atom",
    ),
]

# High-signal: rating-agency-domain-scoped, one per category.
DOMAIN_SCOPED_FEEDS = set()
for _category, _term in CATEGORY_QUERIES.items():
    _name = f"Google News - {_category} (Rating Agencies)"
    FEEDS.append((_name, _gnews_url(f"{RATING_AGENCY_SITES} {_term}")))
    DOMAIN_SCOPED_FEEDS.add(_name)

# Lower-priority: one broad, non-scoped feed for general market color/context.
FEEDS.append((
    "Google News - Market Color (General)",
    _gnews_url('"asset-backed securities" (downgrade OR upgrade OR "rating action" OR spread OR issuance)'),
))

# The 8-K feed is unfiltered (any public company files 8-Ks), so most items are
# irrelevant to ABS surveillance. Cheap keyword pre-filter on the title/entity
# name before spending a Claude call. ABS-EE filings are inherently ABS-related
# and Google News queries are already keyword-scoped, so only 8-K needs this.
KEYWORD_FILTERED_FEEDS = {"SEC EDGAR - 8-K filings"}

ABS_KEYWORDS = [
    "asset-backed", "asset backed", " abs ", "securitiz", "trust",
    "receivables", "auto loan", "auto lease", "servicer", "collateral",
    "solar loan", "clo ", "rmbs", "cmbs", "mortgage-backed", "mortgage backed",
    "credit card", "student loan", "timeshare", "aircraft lease", "container lease",
    "franchise royalty", "whole business", "conduit", "sasb", "k-series",
    " cas ", "stacr", "non-qm", "risk transfer",
]


def matches_abs_keywords(title: str) -> bool:
    t = f" {title.lower()} "
    return any(kw in t for kw in ABS_KEYWORDS)


# Google News occasionally surfaces nav/category pages instead of articles
# (e.g. "Ratings News - Moody's", "Whole Business - Moody's", a bare
# "- capitaliq.spglobal.com"). These have a short, generic headline in front
# of a known publisher name. Only strip the publisher suffix for names we
# recognize, so SEC EDGAR titles ("ABS-EE - Company (CIK) (Filer)") — a
# different format entirely — are never mistaken for nav pages.
JUNK_TITLE_MARKERS = ["essential intelligence", "ratings news", "market intelligence"]
KNOWN_PUBLISHER_SUFFIXES = ["S&P Global", "KBRA", "Moody's", "Moodys", "Fitch Ratings", "Fitch"]


def is_junk_title(title: str) -> bool:
    t = (title or "").strip()
    if not t or t.startswith("-"):
        return True
    headline = t
    for pub in KNOWN_PUBLISHER_SUFFIXES:
        suffix = f" - {pub}"
        if t.endswith(suffix):
            headline = t[: -len(suffix)].strip()
            break
    if len(headline) < 20:
        return True
    return any(m in headline.lower() for m in JUNK_TITLE_MARKERS)


# --- Action grouping -------------------------------------------------------
# Buckets the free-text action_type Claude returns into the 3 fixed sections
# the dashboard displays (plus a catch-all). Keyword-based, not another API
# call, so it applies uniformly to old and new records alike.
ACTION_GROUPS_ORDER = [
    "Rating Actions",
    "Performance/Servicer Reports",
    "New Issuance/Market Color",
    "Other",
]


def classify_action_group(action_type: str) -> str:
    t = (action_type or "").lower()
    if any(k in t for k in ("rating", "upgrade", "downgrade", "outlook")):
        return "Rating Actions"
    if any(k in t for k in ("performance", "servicer")):
        return "Performance/Servicer Reports"
    if any(k in t for k in ("issuance", "market", "spread", "trend")):
        return "New Issuance/Market Color"
    return "Other"


# --- Source credibility ------------------------------------------------
# Rating agencies / regulators are primary sources; everything else
# (aggregators, blogs, trade press) is lower-signal secondary commentary.
HIGH_SIGNAL_MARKERS = [
    "s&p global", "s&p", "kbra", "moody's", "moodys", "fitch",
    "dbrs", "morningstar", "sec edgar", "sec.gov",
]


def classify_source_signal(title: str, source_feed: str) -> str:
    if "sec edgar" in source_feed.lower():
        return "high"
    if source_feed in DOMAIN_SCOPED_FEEDS:
        return "high"
    text = f"{title} {source_feed}".lower()
    return "high" if any(m in text for m in HIGH_SIGNAL_MARKERS) else "low"


# --- Sample portfolio-watch matching ---------------------------------------
# Illustrative demo holdings from ABS_Monitor.xlsx's Deal Master tab (8 deals).
# THIS IS SAMPLE/DEMO DATA, NOT REAL FUND POSITIONS. Sponsor names like
# "CarLend Fin" and "GreenLend Inc" are fictional, so real news will mostly
# only match on the two real-world originators (Westlake, CNH Industrial).
SAMPLE_HOLDINGS = [
    {"deal_ids": ["CARL-2024-1", "CARL-2024-2"], "name": "CarLend Auto Trust",
     "sector": "Prime Auto", "keywords": ["carlend"]},
    {"deal_ids": ["WEST-2024-1", "WEST-2024-2"], "name": "Westlake Auto Receivables",
     "sector": "Subprime Auto", "keywords": ["westlake"]},
    {"deal_ids": ["CNH-2024-1", "CNH-2024-2"], "name": "CNH Equipment Capital",
     "sector": "Equipment", "keywords": ["cnh"]},
    {"deal_ids": ["DCTR-2024-1"], "name": "DataCenter Infrastructure 2024-1",
     "sector": "Data Center", "keywords": ["hyperscale", "data center abs", "data center securit"]},
    {"deal_ids": ["SOLR-2023-1"], "name": "SolarLend Receivables",
     "sector": "Solar", "keywords": ["solarlend", "greenlend"]},
]


def match_holdings(text: str):
    t = text.lower()
    return [h["name"] for h in SAMPLE_HOLDINGS if any(kw in t for kw in h["keywords"])]
