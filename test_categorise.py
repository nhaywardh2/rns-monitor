"""
test_categorise.py — regression suite for the RNS Monitor's headline categoriser
and filtering helpers.

Run with:  python test_categorise.py

Covers three things:
  1. Standard RNS headline vocabulary maps to the right category.
  2. Substring traps stay fixed: word-boundary matching means 'deal' must not
     match "Dealings", 'director' must not match "Directorate", 'ai' must not
     match "chairman", 'gold' must not match "Goldman Sachs", 'nav' must not
     match "Navigator".
  3. Rule ordering: Buyback before Contract, Fundraise before M&A, Admin
     pre-checks before the Results/Contract cascades, material categories
     always beating Admin.
"""

from app import (
    categorise,
    is_excluded_company,
    is_material_type,
    is_sector_relevant,
    extract_ticker,
    make_dedup_key,
)

# ── Categoriser cases: (headline, expected category) ─────────────────────────

CASES = [
    # Results
    ("Final Results", "Results"),
    ("Interim Results", "Results"),
    ("Half-year Report", "Results"),
    ("Preliminary Results for the year ended 31 March 2026", "Results"),
    ("Full Year Results 2025", "Results"),
    ("Annual Results", "Results"),
    ("Q3 Report", "Results"),
    ("Interim Management Statement", "Results"),
    ("First Quarter Results", "Results"),

    # Trading Update
    ("Trading Update", "Trading Update"),
    ("Trading Statement", "Trading Update"),
    ("Pre-close Trading Update", "Trading Update"),
    ("Q1 Trading Update", "Trading Update"),
    ("Year-end Trading Update", "Trading Update"),

    # Company Update — includes the CMD / investor-event additions
    ("Company Update", "Company Update"),
    ("Operational Update", "Company Update"),
    ("Business Update", "Company Update"),
    ("Strategic Review", "Company Update"),
    ("Profit Warning", "Company Update"),
    ("Capital Markets Day", "Company Update"),
    ("Notice of Capital Markets Event", "Company Update"),
    ("Investor Presentation", "Company Update"),
    ("Investor Meet Company Presentation", "Company Update"),
    ("Analyst and Investor Day", "Company Update"),
    ("Site Visit for Analysts", "Company Update"),
    ("Response to Press Speculation", "Company Update"),

    # Buyback — must win over Contract ('renewal') and Fundraise
    ("Transaction in Own Shares", "Buyback"),
    ("Share Buyback Programme", "Buyback"),
    ("Renewal of Buyback Programme", "Buyback"),
    ("Commencement of Share Repurchase", "Buyback"),

    # Contract
    ("Contract Award", "Contract"),
    ("New Partnership Agreement", "Contract"),
    ("Framework Agreement with NHS Trust", "Contract"),
    ("Contract Extension", "Contract"),
    ("Major New Client Win", "Contract"),

    # Fundraise — must win over M&A for offer-adjacent raise language
    ("Placing to raise £50m", "Fundraise"),
    ("Rights Issue", "Fundraise"),
    ("Result of Open Offer", "Fundraise"),
    ("Retail Offer via PrimaryBid", "Fundraise"),
    ("Successful Fundraising", "Fundraise"),
    ("Proposed Equity Raise and Bookbuild", "Fundraise"),
    ("Intention to Float", "Fundraise"),
    ("Secondary Offering", "Fundraise"),

    # M&A
    ("Recommended Cash Offer for Foo plc", "M&A"),
    ("Completion of Acquisition", "M&A"),
    ("Proposed Merger", "M&A"),
    ("Disposal of Non-core Assets", "M&A"),
    ("Statement re Possible Offer", "M&A"),
    ("Scheme of Arrangement becomes Effective", "M&A"),

    # Board Change — 'Directorate Change' is the standard RNS header and is a
    # board matter, not a dealing
    ("Directorate Change", "Board Change"),
    ("Appointment of Chief Financial Officer", "Board Change"),
    ("Resignation of Non-Executive Director", "Board Change"),
    ("Board Change", "Board Change"),
    ("CEO Succession", "Board Change"),
    ("Chairman Steps Down", "Board Change"),

    # Shareholding
    ("Holding(s) in Company", "Shareholding"),
    ("TR-1: Notification of Major Holdings", "Shareholding"),
    ("Notification of Major Interest in Shares", "Shareholding"),

    # Director Dealing — 'Dealings' must not be caught by Contract's 'deal'
    ("Director/PDMR Shareholding", "Director Dealing"),
    ("Dealings by PDMR", "Director Dealing"),
    ("Directors' Dealings", "Director Dealing"),
    ("PDMR Notification", "Director Dealing"),

    # Regulatory
    ("FCA Investigation Update", "Regulatory"),
    ("Regulatory Statement", "Regulatory"),

    # Dividend
    ("Interim Dividend Declaration", "Dividend"),
    ("Dividend Timetable", "Dividend"),
    ("Special Dividend", "Dividend"),

    # Admin — the noise bucket, watchlist-overridden client-side
    ("Total Voting Rights", "Admin"),
    ("Block Listing Interim Review", "Admin"),
    ("Result of AGM", "Admin"),
    ("Result of General Meeting", "Admin"),
    ("AGM Statement", "Admin"),
    ("Form 8.3 - Foo plc", "Admin"),
    ("Publication of Prospectus", "Admin"),
    ("Publication of Circular", "Admin"),
    ("Publication of Annual Report and Accounts", "Admin"),
    ("Posting of Annual Report", "Admin"),
    ("Price Monitoring Extension", "Admin"),
    ("Second Price Monitoring Extn", "Admin"),
    ("Exercise of Options", "Admin"),
    ("Change of Name", "Admin"),
    ("Change of Registered Office", "Admin"),
    ("Change of Nominated Adviser", "Admin"),
    ("Admission to Trading on AIM", "Admin"),
    ("Notice of Redemption", "Admin"),
    ("Statement of Capital", "Admin"),

    # Ordering: material categories beat Admin when both match
    ("Final Results and Notice of AGM", "Results"),
    ("Publication of Final Results", "Results"),
    ("Acquisition and Publication of Circular", "M&A"),

    # Substring traps — word boundaries must hold
    ("Navigator Holdings Rebrand", "Other"),        # 'nav' must not fire; no other keyword matches
    ("Claims Settlement Reached", "Other"),          # 'ims' must not match inside 'claims'

    # Fallbacks
    ("Miscellaneous Announcement", "Other"),
]

# Non-Reg fallback: unmatched headline from a non-regulatory feed
NONREG_CASES = [
    ("Miscellaneous Announcement", "GNW", "Non-Reg"),
    ("Miscellaneous Announcement", "PRN", "Non-Reg"),
    ("Final Results", "GNW", "Results"),  # matched headlines keep their category regardless of feed
]

# ── Exclusion cases: (company name, should be excluded) ─────────────────────

EXCLUSION_CASES = [
    ("Anglo American", True),
    ("Rio Tinto", True),
    ("Greatland Gold", True),
    ("Tullow Oil", True),
    ("Empire Metals", True),
    ("Atlantic Lithium", True),
    ("Diversified Gas & Oil", True),
    # Word boundaries: these must NOT be excluded
    ("Goldman Sachs Group", False),          # 'gold' must not match 'Goldman'
    ("Hostelworld Group", False),
    ("Bloomsbury Publishing", False),
    ("Gattaca", False),
    ("Petrofac", False),                     # 'petroleum'/'oil' must not fire on 'Petro'/'ofac'
]

# ── Noise-type cases: (headline, is material i.e. NOT excluded) ──────────────

MATERIAL_CASES = [
    ("Final Results", True),
    ("Monthly Factsheet", False),
    ("Net Asset Value(s)", False),
    ("Replacement: Final Results", False),
    ("Notice of AGM", False),
    ("Notice of Results", False),
    # 'nav' word-boundary: headlines about ships and renames must survive
    ("Navigator Holdings Contract Win", True),
    ("Change of Name to Navigo Group", True),
]

# ── Sector relevance: 'ai' must be word-bounded ───────────────────────────────

SECTOR_CASES = [
    ("DeepTech plc", "AI partnership announced", True),
    ("Foo plc", "Chairman appointment", False),          # 'chairman' contains 'ai'
    ("Foo plc", "Documents available for inspection", False),  # 'available' contains 'ai'
    ("Persimmon", "Housebuilding update", True),
]

# ── Ticker extraction ─────────────────────────────────────────────────────────

TICKER_CASES = [
    ("Hostelworld Group (HSW)", "HSW"),
    ("Foo Group (AIM) (FOO)", "FOO"),
    ("Foo Group (AIM)", None),               # venue token must not become a ticker
    ("Taylor Wimpey (TW.)", "TW."),
]

# ── Dedup keys ────────────────────────────────────────────────────────────────

def run():
    failures = []

    for headline, expected in CASES:
        got = categorise(headline)
        if got != expected:
            failures.append(f"categorise({headline!r}) = {got!r}, expected {expected!r}")

    for headline, feed, expected in NONREG_CASES:
        got = categorise(headline, feed)
        if got != expected:
            failures.append(f"categorise({headline!r}, {feed!r}) = {got!r}, expected {expected!r}")

    for company, expected in EXCLUSION_CASES:
        got = is_excluded_company(company)
        if got != expected:
            failures.append(f"is_excluded_company({company!r}) = {got}, expected {expected}")

    for headline, expected in MATERIAL_CASES:
        got = is_material_type(headline)
        if got != expected:
            failures.append(f"is_material_type({headline!r}) = {got}, expected {expected}")

    for company, headline, expected in SECTOR_CASES:
        got = is_sector_relevant(company, headline)
        if got != expected:
            failures.append(f"is_sector_relevant({company!r}, {headline!r}) = {got}, expected {expected}")

    for text, expected in TICKER_CASES:
        got = extract_ticker(text)
        if got != expected:
            failures.append(f"extract_ticker({text!r}) = {got!r}, expected {expected!r}")

    # Dedup: distinct URLs never collapse, same URL always collapses
    k1 = make_dedup_key("FOO", "Foo plc", "Holding(s) in Company", "https://www.investegate.co.uk/announcement/rns/foo--foo/holdings/1")
    k2 = make_dedup_key("FOO", "Foo plc", "Holding(s) in Company", "https://www.investegate.co.uk/announcement/rns/foo--foo/holdings/2")
    k3 = make_dedup_key("FOO", "Foo  plc", "Holding(s)  in Company", "https://www.investegate.co.uk/announcement/rns/foo--foo/holdings/1")
    if k1 == k2:
        failures.append("make_dedup_key: two different URLs collapsed to the same key")
    if k1 != k3:
        failures.append("make_dedup_key: identical URLs did not collapse to one key")
    # No-URL fallback still normalises
    k4 = make_dedup_key("FOO", "Foo plc", "Final  Results")
    k5 = make_dedup_key("FOO", "Foo plc (FOO)", "Final Results")
    if k4 != k5:
        failures.append("make_dedup_key: ticker fallback failed to normalise whitespace/name variants")

    total = (len(CASES) + len(NONREG_CASES) + len(EXCLUSION_CASES)
             + len(MATERIAL_CASES) + len(SECTOR_CASES) + len(TICKER_CASES) + 3)
    if failures:
        print(f"FAILED {len(failures)}/{total} checks:\n")
        for f in failures:
            print(f"  ✗ {f}")
        raise SystemExit(1)
    print(f"All {total} checks passed.")

if __name__ == "__main__":
    run()
