"""Backend tests for the RNS Monitor.

Run from the repo root:
    python tests/test_backend.py

Everything runs against a mock Investegate, so no network access is needed and nothing
real is scraped. Watchlist and cache files are written to a temporary folder, never to
the repo.

The Postgres tests run only when TEST_DATABASE_URL points at a disposable database,
for example:
    TEST_DATABASE_URL=postgresql://postgres@localhost:5432/rnstest python tests/test_backend.py
They create and drop their own tables.

Each check is named after the behaviour it protects. Several exist because the
behaviour broke in production (see the notes beside them).
"""
import asyncio
import re
import gzip
import os
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

# Isolate data files before the app is imported (it loads the watchlist at import).
_TMP = Path(tempfile.mkdtemp(prefix="rns-test-"))
os.environ["WATCHLIST_FILE"] = str(_TMP / "watchlist.json")
os.environ["CACHE_FILE"] = str(_TMP / "cache_today.json")
os.environ.pop("DATABASE_URL", None)
os.environ.pop("APP_PASSWORD", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

B = "https://www.investegate.co.uk"
RESULTS = []


def check(name, condition):
    RESULTS.append((name, bool(condition)))


# ── Mock Investegate ─────────────────────────────────────────────────────────

def listing(rows, next_url=None):
    tr = "".join(
        f'<tr><td>{t}</td><td>{src}</td><td>{co}</td><td><a href="{href}">{hl}</a></td></tr>'
        for t, src, co, hl, href in rows)
    nxt = f'<a href="{next_url}">Next</a>' if next_url else ""
    return f"<html><body><table>{tr}</table>{nxt}</body></html>"


def company_page(rows):
    tr = "".join(f'<tr><td>{d}</td><td>{tm}</td><td>{src}</td><td><a href="{h}">{hl}</a></td></tr>'
                 for d, tm, src, hl, h in rows)
    return f"<html><body><table>{tr}</table></body></html>"


class MockInvestegate:
    """Serves pages from a dict. Anything not listed is an empty listing."""

    def __init__(self):
        self.pages = {}
        self.status = {}
        self.hits = {}
        self.in_flight = 0
        self.max_in_flight_company = 0

    def handler(self, request):
        url = str(request.url)
        self.hits[url] = self.hits.get(url, 0) + 1
        if url in self.status:
            return httpx.Response(self.status[url], text="")
        return httpx.Response(200, text=self.pages.get(url, listing([])))

    def count(self, prefix):
        return sum(v for k, v in self.hits.items() if k.startswith(prefix))


MOCK = MockInvestegate()
_RealClient = httpx.AsyncClient


class _FakeClient(_RealClient):
    def __init__(self, *a, **k):
        k["transport"] = httpx.MockTransport(MOCK.handler)
        super().__init__(*a, **k)


app.httpx.AsyncClient = _FakeClient


def reset_state(today=date(2026, 9, 28)):
    MOCK.pages, MOCK.status, MOCK.hits = {}, {}, {}
    app.today_london = lambda: today
    app.daily_cache.announcements = {}
    app.daily_cache.cache_date = today.isoformat()
    app.daily_cache.last_scrape = None
    app.daily_cache.last_errors = []
    app._full_titles.clear()


def run(coro):
    return asyncio.run(coro)


def served():
    anns = app.apply_live_watchlist(app.daily_cache.get_today())
    return {a.ticker: a for a in anns}


# ── Scraping ────────────────────────────────────────────────────────────────

def test_pagination_and_restart():
    """Capricorn, 28 Sep 2026: buried below the 50-row cap on a busy Monday."""
    reset_state()
    arch = f"{B}/today-announcements/2026-09-28"
    p1 = [(f"28 Sep 2026 07:{i % 60:02d} AM", "RNS", f"Noise Co {i} (N{i:02d})", f"Holding(s) in Company {i}",
           f"/announcement/rns/noise-co--n{i:02d}/x/{1000 + i}") for i in range(50)]
    p2 = [("28 Sep 2026 07:00 AM", "RNS", "Capricorn Energy (CNE)", "Statement regarding final offer for Capricorn",
           "/announcement/rns/capricorn-energy--cne/statement/9792659")]
    MOCK.pages[arch] = listing(p1, next_url=f"http://www.investegate.co.uk/today-announcements/2026-09-28?page=2")
    MOCK.pages[f"{arch}?page=2"] = listing(p2)
    run(app.scrape_investegate(force=True))
    by = served()
    check("pagination: announcement on page 2 of the archive is caught", "CNE" in by)
    check("pagination: all 50 page-1 rows kept", all(f"N{i:02d}" in by for i in range(50)))

    before = MOCK.hits.get(f"{arch}?page=2", 0)
    run(app.scrape_investegate(force=True))
    check("pagination: steady state stops at page 1", MOCK.hits.get(f"{arch}?page=2", 0) == before)

    app.daily_cache.announcements = {}          # a Render restart wipes the cache
    run(app.scrape_investegate(force=True))
    check("pagination: after a restart the sweep goes deep again", MOCK.hits.get(f"{arch}?page=2", 0) > before)
    check("pagination: after a restart the day is rebuilt", "CNE" in served())


def test_multiday_listing_stops_early():
    """The M&A category spans days. It used to walk all 20 pages of history whenever
    one new item appeared."""
    reset_state()
    cat = f"{B}/category/mergers-acquisitions-and-disposals"
    MOCK.pages[cat] = listing([("28 Sep 2026 07:00 AM", "RNS", "New Co (NEW)", "Possible Offer", "/a/new--new/x/1")],
                              next_url=f"{cat}?page=2")
    for n in range(2, 25):
        MOCK.pages[f"{cat}?page={n}"] = listing(
            [(f"{20 - n % 18} Sep 2026 07:00 AM", "RNS", f"Old {n} (O{n})", "Offer Update", f"/a/old--o{n}/x/{n}")],
            next_url=f"{cat}?page={n + 1}")
    run(app.scrape_investegate(force=True))
    pages = MOCK.count(cat)
    check(f"multi-day listing: stops once rows are older than the session (fetched {pages} pages)", pages <= 2)
    check("multi-day listing: today's M&A item still caught", "NEW" in served())


def test_weekend_and_bank_holidays():
    """Tribal, 27 Sep 2026: a Sunday takeover statement was dropped as not-today."""
    reset_state()
    MOCK.pages[f"{B}/today-announcements/2026-09-27"] = listing(
        [("27 Sep 2026 06:00 PM", "RNS", "Tribal Group (TRB)", "Increase in Price for the Recommended Acquisition",
          "/announcement/rns/tribal-group--trb/increase/9792556")])
    run(app.scrape_investegate(force=True))
    by = served()
    check("weekend: Monday's window is Sat, Sun, Mon",
          app.trading_window() == [date(2026, 9, 28), date(2026, 9, 27), date(2026, 9, 26)])
    check("weekend: Sunday release served on Monday", "TRB" in by)
    check("weekend: it keeps its real date", by.get("TRB") and by["TRB"].date_str == "27/09/2026")

    app.today_london = lambda: date(2026, 9, 29)
    check("weekend: an ordinary Tuesday is just today", app.trading_window() == [date(2026, 9, 29)])
    app.today_london = lambda: date(2026, 9, 1)            # Tuesday after August bank holiday
    check("bank holiday: Tuesday after a bank holiday Monday includes Mon, Sun, Sat",
          app.trading_window() == [date(2026, 9, 1), date(2026, 8, 31), date(2026, 8, 30), date(2026, 8, 29)])
    app.today_london = lambda: date(2026, 12, 29)          # after Christmas 2026 (28th is a substitute day)
    check("bank holiday: substitute Boxing Day handled",
          date(2026, 12, 28) in app.trading_window() and date(2026, 12, 25) in app.trading_window())


def test_aliases():
    """I-RES, 28 Sep 2026: filed on Investegate under its Dublin code 0QT8."""
    reset_state()
    url = "/announcement/eqs/irish-residential-properties-reit-p--0qt8/statement-regarding-possible-offer/9792949"
    MOCK.pages[f"{B}/company/0QT8"] = company_page(
        [("28 Sep 2026", "07:00 AM", "EQS", "Statement regarding Possible Offer", url)])
    MOCK.pages[f"{B}/category/mergers-acquisitions-and-disposals"] = listing(
        [("28 Sep 2026 07:00 AM", "EQS", "IRISH RESIDENTIAL PROPERTIES REIT P (0QT8)",
          "Statement regarding Possible Offer", url)])
    run(app.scrape_investegate(force=True))
    by = served()
    check("alias: 0QT8 announcement is served under IRES", "IRES" in by and "0QT8" not in by)
    check("alias: flagged as core watchlist", by.get("IRES") and by["IRES"].is_watchlist)
    check("alias: /company/0QT8 was fetched", MOCK.hits.get(f"{B}/company/0QT8", 0) >= 1)
    check("alias: not duplicated across sources",
          sum(1 for a in app.daily_cache.get_today() if app.canonical_ticker(a.ticker) == "IRES") == 1)


def test_truncated_headlines():
    """Listing pages cut long headlines short, so tagging worked from partial text."""
    reset_state()
    url = f"{B}/announcement/eqs/genel-energy--genl/increased/9790542"
    MOCK.pages[f"{B}/today-announcements/2026-09-28"] = listing(
        [("28 Sep 2026 07:00 AM", "EQS", "Genel Energy (GENL)", "Genel Energy PLC: INCREASED ...", url)])
    MOCK.pages[url] = "<html><h1>Genel Energy PLC: Increased Recommended Cash Offer for Capricorn</h1></html>"
    run(app.scrape_investegate(force=True))
    g = served().get("GENL")
    check("truncated headline: replaced with the full title", g and g.headline.endswith("Cash Offer for Capricorn"))
    check("truncated headline: recategorised from the full title", g and g.category == "M&A")
    fetches = MOCK.hits.get(url, 0)
    run(app.scrape_investegate(force=True))
    check("truncated headline: full title fetched once, then remembered", MOCK.hits.get(url, 0) == fetches)


def test_company_pages_are_polite():
    reset_state()
    # Count the peak number of company-page requests in flight by timing them.
    active, peak = [0], [0]

    async def slow_handler(request):
        url = str(request.url)
        if "/company/" in url:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
            await asyncio.sleep(0.02)
            active[0] -= 1
        return httpx.Response(200, text=listing([]))

    class _Slow(_RealClient):
        def __init__(self, *a, **k):
            k["transport"] = httpx.MockTransport(slow_handler)
            super().__init__(*a, **k)

    app.httpx.AsyncClient = _Slow
    try:
        run(app.scrape_investegate(force=True))
    finally:
        app.httpx.AsyncClient = _FakeClient
    check(f"company pages: at most {app.COMPANY_PAGE_CONCURRENCY} in flight (peak {peak[0]})",
          0 < peak[0] <= app.COMPANY_PAGE_CONCURRENCY)

    reset_state()
    flaky = f"{B}/company/GTLY"
    calls = {"n": 0}
    real = MOCK.handler

    def once_503(request):
        if str(request.url) == flaky:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(503, text="")
        return real(request)

    MOCK.handler = once_503
    try:
        run(app.scrape_investegate(force=True))
    finally:
        MOCK.handler = real
    stats = app.daily_cache.last_stats.get("sources", {}).get("company_pages", {})
    check("company pages: a one-off 503 is retried", calls["n"] == 2 and "GTLY" not in stats.get("failed", []))


def test_diagnostics():
    reset_state()
    MOCK.pages[f"{B}/today-announcements/2026-09-28"] = listing(
        [("28 Sep 2026 07:00 AM", "RNS", "Some Co (SOM)", "Trading Update", "/a/some--som/x/1")])
    MOCK.status[f"{B}/aim"] = 500
    run(app.scrape_investegate(force=True))
    st = app.daily_cache.last_stats
    src = st.get("sources", {})
    check("diagnostics: per-source row counts recorded",
          src.get(f"{B}/today-announcements/2026-09-28", {}).get("rows_in_session") == 1)
    check("diagnostics: a failing page is marked with its error", src.get(f"{B}/aim", {}).get("ok") is False)
    check("diagnostics: duration and session days recorded",
          "duration_seconds" in st and st.get("session_days", [None])[0] == "2026-09-28")



# ── Announcement detail ─────────────────────────────────────────────────────

# Shaped like an Investegate announcement: site chrome, AI summary, the RNS itself, and the
# site's footer, with attack attempts mixed into the body.
SAMPLE_RNS_PAGE = """<html><head><title>Final Results | Investegate</title><style>body{}</style></head><body>
<div id="outterContent">
 <nav><a href="/">Home</a><a href="/aim">AIM</a></nav>
 <h1>Final Results</h1>
 <div class="ai"><h3>Summary by AI</h3><p>Made Tech reported revenue growth of 12% and a return to profit for the year.</p>
   <a href="#">Close X</a><p><em>Disclaimer*</em></p></div>
 <div class="rnsbody">
  <p style="text-align:center"><b>Made Tech Group plc</b><br>("Made Tech" or "the Group")</p>
  <p style="text-align:center"><b>Final Results for the year ended 31 May 2026</b></p>
  <p>Made Tech, a leading provider of digital, data and technology services to the UK public sector, announces its audited results.</p>
  <p><b>Financial highlights</b></p>
  <table style="border-collapse:collapse; width:100%; background-image:url(https://evil.example/x.png)" onclick="alert(1)">
   <tr><td style="border-bottom:1px solid #000"></td><td style="text-align:right;border-bottom:1px solid #000"><b>FY26</b></td>
       <td style="text-align:right;border-bottom:1px solid #000"><b>FY25</b></td><td style="text-align:right;border-bottom:1px solid #000"><b>Change</b></td></tr>
   <tr><td>Revenue</td><td style="text-align:right">&pound;48.1m</td><td style="text-align:right">&pound;42.9m</td><td style="text-align:right">+12%</td></tr>
   <tr><td>Adjusted EBITDA</td><td style="text-align:right">&pound;3.2m</td><td style="text-align:right">&pound;1.1m</td><td style="text-align:right">+191%</td></tr>
   <tr><td>Net cash</td><td style="text-align:right">&pound;12.4m</td><td style="text-align:right">&pound;10.0m</td><td style="text-align:right">+24%</td></tr>
  </table>
  <p><b>Operational highlights</b></p>
  <ul><li>Contract wins with three new central government departments</li><li>Sales pipeline of &pound;210m</li></ul>
  <p>Rory MacDonald, Chief Executive, said: "This has been a year of disciplined growth."</p>
  <script>document.cookie='steal'</script><iframe src="https://evil.example"></iframe>
  <svg onload="alert(1)"><circle r="5"/></svg>
  <p><a href="javascript:alert(1)">Click me</a> <a href="/announcement/rns/made-tech--mtec/notice/1">Notice of AGM</a></p>
  <p><img src="/images/chart.png" alt="Revenue chart"> <img src="data:image/svg+xml;base64,PHN2Zy8+" alt="bad"></p>
  <p><font size="2" face="Arial">For further information:</font></p>
  <p>This information is provided by RNS, the news service of the London Stock Exchange.</p>
 </div>
 <div class="site-footer"><p>Please note, this site uses cookies. To continue using Investegate you accept our policy.</p>
  <p>Investegate takes no responsibility for the content of announcements.</p></div>
</div></body></html>"""


def _detail_for(page_html):
    url = f"{B}/announcement/rns/made-tech--mtec/final-results/{abs(hash(page_html)) % 10**9}"
    MOCK.pages[url] = page_html
    return url, run(app.fetch_announcement_detail(url))


SUMMARY_ONLY_PAGE = """<html><head><title>Total Voting Rights | Company Announcement | Investegate</title></head><body>
<div id="outterContent">
<ul><li><a href="/welcome">Welcome</a></li><li><a href="/content">Articles</a></li><li><a href="/about-us">About</a></li><li><a href="/contact-us">Contact Us</a></li><li><a href="/help">Help</a></li></ul>
<ul><li><a href="/">Home</a></li><li><a href="/search-company">Companies</a></li><li><a href="/advanced-search">Advanced search</a></li><li><a href="/announcement-archive">Archive</a></li><li><a href="/meetings">Meetings</a></li></ul>
<h1>Total Voting Rights</h1>
<h2>Summary by AI BETA<a href="#">Close X</a></h2>
<p>Breedon Group plc announces that as of October 30, 2026, its issued share capital comprises 347,106,800 ordinary shares, with no shares held in treasury, resulting in 347,106,800 voting rights.</p>
<a href="#">Disclaimer*</a>
<h5>AI Summary Disclaimer BETA</h5><span>&times;</span>
<p>The content you are viewing has been generated or assisted by artificial intelligence (AI). While efforts are made to ensure accuracy and clarity, AI-generated text may contain inaccuracies, outdated information, or subjective interpretations. This content is intended for general informational purposes only and should not be considered professional advice or factual confirmation.</p>
<p>We encourage you to independently verify any information provided, especially if it will be relied upon for making decisions or taking action. Any reliance you place on this material is strictly at your own risk. The AI system that produced this content cannot assess the specifics of individual circumstances, and results may vary based on context and individual needs.</p>
<h2>Companies</h2><a href="/company/BREE">Breedon Group (BREE)</a>
<h6><b>UK 100</b></h6><a href="https://www.tradingview.com/">FTSE quotes</a> by TradingView
<a href="/meetings"><img src="/assets/images/imc/imc-view-meetings@1x.gif" alt="Investor Meet Company View Meetings"></a>
<h2>Latest directors dealings</h2>
<ul><li>5 hours ago <a href="#">Kier Group</a></li><li>6 hours ago <a href="#">Banco Santander S.A.</a></li><li>6 hours ago <a href="#">Banco Santander S.A.</a></li><li>6 hours ago <a href="#">PZ Cussons</a></li><li>6 hours ago <a href="#">Oxford Nanopore Technologies</a></li></ul>
<a href="/category/directors-dealings">All directors dealings today</a>
<p>All intraday prices are subject to a delay of fifteen (15) minutes.</p>
<p>Investegate takes no responsibility for the accuracy of the information within this site.</p>
<p>&copy; 2026 Investor Meet Company Ltd</p>
<h5>This website is for Private Investors*</h5><p>I am a Private Investor</p>
<p>Please note, this site uses cookies.</p>
</div></body></html>"""


def test_summary_only_page_is_not_shown_as_the_release():
    reset_state()
    url, d = _detail_for(SUMMARY_ONLY_PAGE)
    check("summary-only page: release marked unavailable", d.body_available is False)
    check("summary-only page: no page furniture passed off as release text", d.body_text == "")
    check("summary-only page: no key figures invented from furniture", d.key_figures == [])
    check("summary-only page: AI summary still provided", "347,106,800" in (d.ai_summary or ""))
    url2, d2 = _detail_for(SAMPLE_RNS_PAGE)
    check("page with a release: still available, text kept for key figures", d2.body_available is True and "Adjusted EBITDA" in d2.body_text)
    check("reader removed: no formatted HTML is produced", d.body_html == "" and d2.body_html == "")


def test_version_stamp():
    """Open tabs never reload themselves, so the page and the feed both carry a version and
    the page can tell when a deploy has happened."""
    reset_state()
    c = TestClient(app.app)
    page = c.get("/").text
    m = re.search(r'<meta name="app-version" content="([0-9a-f]{12})">', page)
    check("version: page is stamped with its version", bool(m))
    r = c.get("/api/rns?include_all=true")
    check("version: feed response carries the same version", m and r.headers.get("x-app-version") == m.group(1))
    r304 = c.get("/api/rns?include_all=true", headers={"If-None-Match": r.headers.get("etag")})
    check("version: a 304 also carries it", r304.status_code == 304 and r304.headers.get("x-app-version") == m.group(1))


# ── Categorisation and exclusion ────────────────────────────────────────────

def test_energy_and_exclusion():
    for name in ("Harbour Energy", "Tullow Oil", "Capricorn Energy", "Genel Energy", "Shell", "BP", "Centrica"):
        check(f"energy: {name} is not excluded", not app.is_excluded_company(name))
    for name in ("Pan African Resources", "Fresnillo", "Greatland Gold", "Central Asia Metals"):
        check(f"mining: {name} is still excluded", app.is_excluded_company(name))
    check("energy: energy names rank as sector relevant", app.is_sector_relevant("Harbour Energy", "Trading Update"))


def test_exclusion_is_visible_and_never_hides_ma():
    reset_state()
    MOCK.pages[f"{B}/today-announcements/2026-09-28"] = listing([
        ("28 Sep 2026 07:00 AM", "RNS", "Fresnillo (FRES)", "Production Report", "/a/fres--fres/x/1"),
        ("28 Sep 2026 07:01 AM", "RNS", "Pan African Resources (PAF)", "Recommended Cash Offer", "/a/paf--paf/x/2"),
        ("28 Sep 2026 07:02 AM", "RNS", "Harbour Energy (HBR)", "Trading Update", "/a/hbr--hbr/x/3"),
    ])
    run(app.scrape_investegate(force=True))
    c = TestClient(app.app)
    j = c.get("/api/rns?include_all=true").json()
    by = {a["ticker"]: a for a in j["announcements"]}
    check("exclusion: mining company is returned, not dropped", "FRES" in by)
    check("exclusion: mining company is flagged excluded_sector", by.get("FRES", {}).get("excluded_sector") is True)
    check("exclusion: M&A involving a mining company is never flagged", by.get("PAF", {}).get("excluded_sector") is False)
    check("exclusion: energy company is not flagged", by.get("HBR", {}).get("excluded_sector") is False)


# ── Endpoints ───────────────────────────────────────────────────────────────

def _seed_cache(n=3):
    today = app.today_london().strftime("%d/%m/%Y")
    app.daily_cache.last_scrape = datetime.now()
    for i in range(n):
        a = app.Announcement(time=f"07:{i:02d}", company=f"Co {i}", ticker=f"C{i}", headline=f"Trading Update {i}",
                             category="Trading Update", url=f"{B}/a/c{i}--c{i}/x/{i}", date_str=today)
        app.daily_cache.announcements[app.make_dedup_key(a.ticker, a.company, a.headline, a.url)] = a.model_dump()


def test_etag_and_gzip():
    reset_state()
    _seed_cache(200)
    c = TestClient(app.app)
    r1 = c.get("/api/rns?include_all=true", headers={"Accept-Encoding": "gzip"})
    etag = r1.headers.get("etag")
    check("etag: response carries an ETag", bool(etag))
    check("gzip: feed is compressed", r1.headers.get("content-encoding") == "gzip")
    app.daily_cache.last_scrape = datetime.now()          # a scrape that found nothing new
    r2 = c.get("/api/rns?include_all=true", headers={"If-None-Match": etag})
    check("etag: unchanged feed returns 304 with no body", r2.status_code == 304 and not r2.content)
    _seed_cache(201)
    r3 = c.get("/api/rns?include_all=true", headers={"If-None-Match": etag})
    check("etag: a new announcement returns the full feed", r3.status_code == 200 and len(r3.json()["announcements"]) == 201)


def test_hard_is_not_destructive():
    """A hard refresh used to wipe every announcement that had scrolled off the listings."""
    reset_state()
    _seed_cache(12)
    c = TestClient(app.app)
    j = c.get("/api/rns?include_all=true&hard=true").json()
    check("hard=true keeps the day's cached announcements", len(j["announcements"]) == 12)
    check("DailyCache.clear no longer exists", not hasattr(app.daily_cache, "clear"))


def test_headers_and_session():
    c = TestClient(app.app)
    check("page is served with no-cache", "no-cache" in c.get("/").headers.get("cache-control", ""))
    check("session lasts 30 days by default", app.SESSION_DAYS == 30)
    h = c.get("/api/health").json()
    check("health reports watchlist storage", "watchlist_storage" in h)
    check("health reports bank holiday horizon", h.get("bank_holidays_listed_until") == "2028-12-26")


def test_watchlist_validation():
    reset_state()
    c = TestClient(app.app)
    r = c.post("/api/watchlist/add", params={"ticker": "not a ticker!", "name": "X"})
    check("watchlist: malformed ticker rejected", r.status_code == 400)
    MOCK.status[f"{B}/company/ZZZZ"] = 404
    r = c.post("/api/watchlist/add", params={"ticker": "ZZZZ", "name": "Nobody plc"})
    check("watchlist: ticker with no Investegate page rejected", r.status_code == 400 and "no company page" in r.json()["error"])
    check("watchlist: rejected ticker not added", "ZZZZ" not in app.WATCHLIST)
    MOCK.pages[f"{B}/company/IRSX"] = company_page(
        [("28 Sep 2026", "07:00 AM", "EQS", "Statement", "/announcement/eqs/some-company--0qt9/statement/1")])
    r = c.post("/api/watchlist/add", params={"ticker": "IRSX", "name": "Aliased plc"})
    check("watchlist: alias situation added with a warning naming the other code",
          r.status_code == 200 and "0QT9" in (r.json().get("warning") or ""))
    MOCK.pages[f"{B}/company/KGH"] = company_page(
        [("28 Sep 2026", "07:00 AM", "RNS", "Update", "/announcement/rns/knights-group--kgh/update/1")])
    r = c.post("/api/watchlist/add", params={"ticker": "kgh", "name": "Knights Group", "tier": "read_across"})
    check("watchlist: valid ticker added and upper-cased", r.status_code == 200 and "KGH" in app.WATCHLIST)
    check("watchlist: non-durable storage says so", "lost" in (r.json().get("warning") or ""))
    c.delete("/api/watchlist/IRSX")
    c.delete("/api/watchlist/KGH")


# ── Persistence ─────────────────────────────────────────────────────────────

def _store_roundtrip(label, make_store):
    store = make_store()
    wl = store.load()
    check(f"{label}: first load seeds the defaults", set(app.DEFAULT_WATCHLIST) <= set(wl))
    store.upsert("KGH", {"name": "Knights Group", "sector": "Legal", "tier": "read_across"})
    store.remove("CRST")
    fresh = make_store().load()                                  # simulates a restart
    check(f"{label}: an addition survives a restart", "KGH" in fresh and fresh["KGH"]["tier"] == "read_across")
    check(f"{label}: a removal survives a restart (not resurrected by defaults)", "CRST" not in fresh)
    app.DEFAULT_WATCHLIST["ZNEW"] = {"name": "New Default", "sector": "", "tier": "core"}
    try:
        check(f"{label}: a new default added in code appears after deploy", "ZNEW" in make_store().load())
    finally:
        del app.DEFAULT_WATCHLIST["ZNEW"]
    store.reset()
    after = make_store().load()
    check(f"{label}: reset restores defaults and forgets removals", "CRST" in after and "KGH" not in after)


def test_json_store():
    path = _TMP / "store_test.json"
    if path.exists():
        path.unlink()
    original = app.WATCHLIST_FILE
    app.WATCHLIST_FILE = path
    try:
        # load() writes the merged list, and WATCHLIST is what upsert/remove persist from
        def make():
            s = app.WatchlistStore()
            wl = s.load()
            app.WATCHLIST.clear()
            app.WATCHLIST.update(wl)
            return s
        store = make()
        app.WATCHLIST["KGH"] = {"name": "Knights Group", "sector": "Legal", "tier": "read_across"}
        store.upsert("KGH", app.WATCHLIST["KGH"])
        app.WATCHLIST.pop("CRST", None)
        store.remove("CRST")
        fresh = make().load()
        check("json store: an addition survives a restart", "KGH" in fresh)
        check("json store: a removal survives a restart", "CRST" not in fresh)
        check("json store: reports itself as not durable", store.status()["durable"] is False)
        old_format = _TMP / "old.json"
        old_format.write_text('{"HSW": {"name": "Hostelworld Group", "sector": "Travel"}}')
        app.WATCHLIST_FILE = old_format
        legacy = app.WatchlistStore().load()
        check("json store: reads the old plain-dict file format", legacy.get("HSW", {}).get("tier") == "core")
    finally:
        app.WATCHLIST_FILE = original
        app.WATCHLIST.clear()
        app.WATCHLIST.update(app.watchlist_store.load())


def test_postgres_store():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        print("  --   postgres store tests skipped (set TEST_DATABASE_URL to run them)")
        return
    import psycopg
    with psycopg.connect(url, autocommit=True) as c:
        c.execute("DROP TABLE IF EXISTS watchlist")
        c.execute("DROP TABLE IF EXISTS watchlist_removed")
    original = app.DATABASE_URL
    app.DATABASE_URL = url
    try:
        store = app.WatchlistStore()
        check("postgres store: connects and reports durable", store.backend == "postgres" and store.status()["durable"])
        _store_roundtrip("postgres store", app.WatchlistStore)
        app.DATABASE_URL = "postgresql://nobody@127.0.0.1:1/nothing"
        broken = app.WatchlistStore()
        check("postgres store: unreachable database falls back instead of crashing",
              broken.backend == "json" and broken.last_error)
    finally:
        app.DATABASE_URL = original
        with psycopg.connect(url, autocommit=True) as c:
            c.execute("DROP TABLE IF EXISTS watchlist")
            c.execute("DROP TABLE IF EXISTS watchlist_removed")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    failed = [n for n, ok in RESULTS if not ok]
    for name, ok in RESULTS:
        print(("  ok   " if ok else "  FAIL ") + name)
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} backend checks passed")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
