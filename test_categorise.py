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

    # ── Real Investegate headlines, 19-20 November 2026 ─────────────────────────
    # Routine takeover dealing disclosures: hidden, never M&A or Other.
    ("Form 38.5a (EPT/RI) DCC Energy plc BNPP FM", "Dealing Disclosure"),
    ("20261119_Irish Residential_38.5 (b)_IE_BOFASE", "Dealing Disclosure"),
    ("20261119_Irish Residential_38.5 (a)_IE_MLI", "Dealing Disclosure"),
    ("Form 38.5A – Irish Residential Prop REIT PLC", "Dealing Disclosure"),
    ("Form 38.5a Kenmare Resources plc", "Dealing Disclosure"),
    ("Form 8.3 DCC Energy PLC 201126", "Dealing Disclosure"),
    ("Form 8.3: DCC LN", "Dealing Disclosure"),
    ("Irish Takeover Panel - Form 8.3 - DCC Energy PLC", "Dealing Disclosure"),   # was M&A
    ("20261119_SEGRO PLC_8.5 EPT RI_UK_MLI", "Dealing Disclosure"),
    # Routine paperwork: Admin.
    ("Result of GM", "Admin"),
    ("Result of AGM", "Admin"),
    ("Results of Annual General Meeting and Salient...", "Admin"),
    ("Company Secretary Change", "Admin"),
    ("Notice to Noteholders", "Admin"),
    ("Issue of Options", "Admin"),
    ("Purchase of Shares by Employee Benefit Trust", "Admin"),
    ("Director Declaration", "Admin"),
    ("Publication of AGM Date", "Admin"),
    ("Publication Of Final Terms", "Admin"),
    ("Admission to Trading", "Admin"),
    ("Total Voting Rights", "Admin"),
    ("Net Asset Value(s)", "Admin"),
    # Fund reporting.
    ("Monthly Report", "Fund Update"),
    ("Portfolio Update", "Fund Update"),
    ("Monthly Shareholder Report - October 2026", "Fund Update"),
    ("Kepler Trust Intelligence: New Research", "Fund Update"),
    ("Monthly Factsheet", "Fund Update"),
    # Investor events.
    ("AVIATION TO PRESENT AT WOLFE RESEARCH CONFERENCE", "Investor Event"),
    ("Investor Presentation via Investor Meet Company", "Investor Event"),
    ("Publication of Investor Presentation", "Investor Event"),
    # News that used to fall to Other.
    ("Business Plan Implementation Update", "Company Update"),
    ("£130,000 Share Subscription at 5.25p per Share", "Fundraise"),
    ("Listing on the Canadian Securities Exchange", "Listing & Advisers"),
    ("Result of GM & Update on Proposed Admission to AIM", "Listing & Advisers"),
    ("Auction Suspension", "Listing & Advisers"),

    # ── Looks routine, but is news: must be visible ──────────────────────────────
    ("Suspension of Listing", "Listing & Advisers"),
    ("Temporary Suspension of Trading", "Listing & Advisers"),
    ("Restoration of Trading", "Listing & Advisers"),
    ("Restoration - MobilityOne Limited", "Listing & Advisers"),
    ("Proposed Cancellation of Admission to AIM", "Listing & Advisers"),
    ("Notice of Delisting", "Listing & Advisers"),
    ("Change of Nominated Adviser", "Listing & Advisers"),
    ("Resignation of Nominated Adviser", "Listing & Advisers"),
    ("Change of Broker", "Listing & Advisers"),
    ("Appointment of Joint Broker", "Listing & Advisers"),
    ("Change of Auditor", "Listing & Advisers"),
    ("Appointment of Administrators", "Listing & Advisers"),
    ("Going Concern Update", "Listing & Advisers"),
    ("Change of Name", "Listing & Advisers"),
    ("AGM Statement", "Trading Update"),
    ("Publication of Prospectus", "Fundraise"),
    ("Posting of Circular", "Company Update"),
    ("Compulsory Acquisition", "M&A"),
    ("Share Consolidation", "Company Update"),

    # ── Numbers in ordinary headlines must not look like dealing forms ───────────
    ("Revenue up 8.5% in Q3", "Other"),   # the point: never a dealing form
    ("Results for the 38.5 week period", "Results"),
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
    ("Capital Markets Day", "Investor Event"),
    ("Strategic Review", "Company Update"),
    ("Profit Warning", "Company Update"),

    # Corporate actions
    ("Acquisition of TargetCo", "M&A"),
    ("Recommended Cash Offer", "M&A"),
    ("Proposed Placing", "Fundraise"),
    ("Rights Issue", "Fundraise"),
    ("Transaction in Own Shares", "Buyback"),
    ("Share Buyback Programme", "Buyback"),

    # Takeover Code lifecycle. Before this was fixed, 30 of 68 of these tagged Other and
    # several more were captured by Contract, Regulatory or Results first.
    ("Increase in Price for the Recommended Acquisition", "M&A"),   # Tribal, 27 Sep 2026
    ("Statement regarding Possible Offer", "M&A"),                  # I-RES, 28 Sep 2026
    ("Statement regarding final offer for Capricorn", "M&A"),       # Capricorn, 28 Sep 2026
    ("Rule 2.4 Announcement", "M&A"),
    ("Rule 2.7 Announcement", "M&A"),
    ("Statement re Press Speculation", "M&A"),
    ("Response to Approach", "M&A"),
    ("Irrevocable Undertakings", "M&A"),
    ("Level of Acceptances", "M&A"),
    ("Acceptance Level Update", "M&A"),
    ("Unconditional in All Respects", "M&A"),
    ("Formal Sale Process", "M&A"),
    ("Strategic Review and Formal Sale Process", "M&A"),
    ("Extension of PUSU Deadline", "M&A"),          # was Contract via 'extension'
    ("Merger Agreement", "M&A"),                    # was Contract via 'agreement'
    ("Court Sanction of Scheme", "M&A"),            # was Regulatory via 'sanction'
    ("Results of Court Meeting and General Meeting", "M&A"),   # was Results
    ("Scheme Document", "M&A"),                     # was Admin, i.e. hidden by default
    ("Scheme Effective", "M&A"),
    ("Recommended Increased Best and Final Offer", "M&A"),
    ("Genel Energy PLC: INCREASED RECOMMENDED CASH ...", "M&A"),   # truncated listing headline
    ("Sale of Business", "M&A"),
    ("Recommended All-Share Combination", "M&A"),

    # Near-misses that must stay OUT of M&A
    ("Open Offer", "Fundraise"),
    ("Retail Offer", "Fundraise"),
    ("Offer for Subscription", "Fundraise"),
    ("Results of Tender Offer", "Buyback"),
    ("Opening Position Disclosure", "Dealing Disclosure"),
    ("Form 8.3 - Tribal Group plc", "Dealing Disclosure"),

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
    # Announcements that look administrative but can be the most important news a
    # company issues. Admin is hidden by default; none of these may land in a hidden
    # category.
    "Suspension of Listing", "Notice of Delisting", "Change of Nominated Adviser",
    "Resignation of Nominated Adviser", "Change of Broker", "Appointment of Administrators",
    "AGM Statement", "Posting of Circular", "Compulsory Acquisition",
    "Monthly Report", "Portfolio Update", "AVIATION TO PRESENT AT WOLFE RESEARCH CONFERENCE",
    "Business Plan Implementation Update",
    "Scheme Document",
    "Statement regarding Possible Offer",
    "Increase in Price for the Recommended Acquisition",
    "Half-year Report",
    "Interim Report and Accounts",
    "Full Year Results",
    "Trading Update",
    "Capital Markets Day",
]

# Watchlist entries that must stay wired end to end.
# Investegate company codes that must map onto a watchlist ticker.
REQUIRED_ALIASES = {"0QT8": "IRES"}

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


def check_aliases():
    """An aliased code must resolve to its watchlist ticker in every path that
    identifies a company: ticker extraction, serve-time recompute, company pages."""
    fails = []
    for code, ticker in REQUIRED_ALIASES.items():
        if app.canonical_ticker(code) != ticker:
            fails.append(f"  canonical_ticker({code!r}) is {app.canonical_ticker(code)!r}, expected {ticker!r}")
        if app.extract_ticker(f"SOME COMPANY NAME ({code})") != ticker:
            fails.append(f"  extract_ticker did not map ({code}) to {ticker}")
        if code not in app.company_codes(ticker):
            fails.append(f"  company page for {code} would not be fetched for {ticker}")
        if ticker not in app.DEFAULT_WATCHLIST:
            fails.append(f"  alias target {ticker} is not on the watchlist")
    print(f"{len(REQUIRED_ALIASES) - len(fails)}/{len(REQUIRED_ALIASES)} alias checks passed"
          if not fails else "alias checks FAILED")
    return fails


def main():
    fails = check_categories() + check_traps() + check_visibility() + check_watchlist() + check_aliases()
    for line in fails:
        print(line)
    if fails:
        raise SystemExit(1)
    print("\nALL PASS")


if __name__ == "__main__":
    main()
