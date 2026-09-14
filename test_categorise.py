"""Regression tests for the RNS Monitor categoriser and watchlist wiring.

Run from the repo root:  python test_categorise.py

This file is the spec for classification behaviour. After changing any keyword list
in categorise(), or adding watchlist / read-across entries, run this and keep it green.
Add a case whenever a mis-tagged announcement turns up in the live feed.
"""
import app

# ── Headline classification ──────────────────────────────────────────────────
# (headline, expected_category)
CASES = [
    # Board change vs director dealing. 'director' is a substring of 'directorate',
    # which is why categorise() relies on word-boundary matching via _kw().
    ("Directorate Change", "Board Change"),
    ("Appointment of Director", "Board Change"),
    ("Resignation of Chairman", "Board Change"),
    ("Board Change", "Board Change"),
    ("Director/PDMR Shareholding", "Director Dealing"),
    ("PDMR Dealings", "Director Dealing"),
    ("Dealings by PDMR", "Director Dealing"),

    # Results, and the admin lookalikes that contain 'results' or 'annual report'.
    ("Full Year Results", "Results"),
    ("Final Results", "Results"),
    ("Interim Results", "Results"),
    ("Interim Management Statement", "Results"),
    ("Voting Results", "Admin"),
    ("Result of AGM", "Admin"),
    ("Annual Financial Report", "Admin"),
    ("Publication of Annual Report", "Admin"),

    # Real headlines that were missed in the live feed in September 2026.
    # The cause was scrape coverage, not classification, but these are pinned so the
    # classification side can never drift: all must stay OUT of NOISE_CATEGORIES.
    ("Half-year Report", "Results"),
    ("Half Year Report", "Results"),
    ("Half Yearly Report", "Results"),
    ("Interim Report and Accounts", "Results"),
    ("FY26 Interim Report and Accounts", "Results"),

    # Trading and company updates
    ("Trading Update", "Trading Update"),
    ("Q1 Trading Statement", "Trading Update"),
    ("Capital Markets Day", "Company Update"),
    ("Strategic Review", "Company Update"),
    ("Profit Warning", "Company Update"),

    # Corporate actions
    ("Acquisition of TargetCo", "M&A"),
    ("Recommended Cash Offer", "M&A"),
    ("Proposed Placing", "Fundraise"),
    ("Rights Issue", "Fundraise"),
    ("Transaction in Own Shares", "Buyback"),
    ("Share Buyback Programme", "Buyback"),

    # Contracts, holdings, dividends, admin
    ("Contract Award with NHS", "Contract"),
    ("Strategic Partnership", "Contract"),
    ("Holding(s) in Company", "Shareholding"),
    ("TR-1: Notification of Major Holdings", "Shareholding"),
    ("Interim Dividend", "Dividend"),
    ("Total Voting Rights", "Admin"),
    ("Block Listing Application", "Admin"),
    ("Price Monitoring Extension", "Admin"),
    ("Notice of AGM", "Admin"),
]

# Headlines that must NOT land in the given category. These are the substring
# collisions that word-boundary matching exists to prevent.
TRAPS = [
    ("Replacing the Auditor", "Fundraise"),        # 'placing' inside 'replacing'
    ("Directorate Change", "Director Dealing"),    # 'director' inside 'directorate'
    ("Dealings by PDMR", "Contract"),              # 'deal' inside 'dealings'
    ("Navigator Holdings update", "Admin"),        # 'nav' inside 'navigator'
]

# Announcements that must never be hidden by default, whatever else changes.
MUST_BE_VISIBLE = [
    "Half-year Report",
    "Interim Report and Accounts",
    "Full Year Results",
    "Trading Update",
    "Capital Markets Day",
]

# Watchlist entries that must stay wired end to end.
REQUIRED_WATCHLIST = {
    "KEYS": "read_across",
    "BOOK": "core",
    "GTLY": "core",
    "FTC": "read_across",
}


def check_categories():
    fails = []
    for hl, exp in CASES:
        got = app.categorise(hl)
        if got != exp:
            fails.append(f"  expected {exp!r:16} got {got!r:16} for {hl!r}")
    print(f"{len(CASES) - len(fails)}/{len(CASES)} category cases passed")
    return fails


def check_traps():
    fails = []
    for hl, wrong in TRAPS:
        got = app.categorise(hl)
        if got == wrong:
            fails.append(f"  {hl!r} was trapped into {wrong!r}")
    print(f"{len(TRAPS) - len(fails)}/{len(TRAPS)} substring-trap checks passed")
    return fails


def check_visibility():
    fails = []
    for hl in MUST_BE_VISIBLE:
        cat = app.categorise(hl)
        if cat in app.NOISE_CATEGORIES:
            fails.append(f"  {hl!r} classifies as {cat!r}, which is hidden by default")
        if not app.is_material_type(hl):
            fails.append(f"  {hl!r} is dropped by EXCLUDED_TYPES")
    print(f"{len(MUST_BE_VISIBLE) - len(fails)}/{len(MUST_BE_VISIBLE)} visibility checks passed")
    return fails


def check_watchlist():
    """Tier data is duplicated across three structures, so they must agree."""
    fails = []
    wl = app.DEFAULT_WATCHLIST
    for ticker, tier in REQUIRED_WATCHLIST.items():
        if ticker not in wl:
            fails.append(f"  {ticker} missing from DEFAULT_WATCHLIST")
        elif wl[ticker].get("tier") != tier:
            fails.append(f"  {ticker} tier is {wl[ticker].get('tier')!r}, expected {tier!r}")

    core = {t for t, v in wl.items() if v.get("tier") == "core"}
    ra = {t for t, v in wl.items() if v.get("tier") == "read_across"}

    mismatch = ra ^ app.READ_ACROSS_TICKERS
    if mismatch:
        fails.append(f"  read_across tier and READ_ACROSS_TICKERS disagree on: {sorted(mismatch)}")
    bad_src = [k for k in app.READ_ACROSS_MATRIX if k not in ra]
    if bad_src:
        fails.append(f"  matrix sources that are not read-across names: {bad_src}")
    bad_tgt = sorted({c for links in app.READ_ACROSS_MATRIX.values() for c in links} - core)
    if bad_tgt:
        fails.append(f"  matrix targets that are not core names: {bad_tgt}")

    print(f"watchlist wiring: {len(wl)} names ({len(core)} core, {len(ra)} read-across)"
          + ("" if fails else " — consistent"))
    return fails


def main():
    fails = check_categories() + check_traps() + check_visibility() + check_watchlist()
    for line in fails:
        print(line)
    if fails:
        raise SystemExit(1)
    print("\nALL PASS")


if __name__ == "__main__":
    main()
