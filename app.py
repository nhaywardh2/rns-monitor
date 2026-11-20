"""
h2Radnor RNS Monitor, web deployment v2.2
Combined API + Frontend, with optional password gate.
"""

import re
import json
import asyncio
import os
import logging
import hmac
import hashlib
import time
from contextlib import asynccontextmanager
from datetime import datetime, date, timedelta
from typing import Optional
from pathlib import Path

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, Query, Request, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from pydantic import BaseModel

# ── Configuration via environment variables ──────────────────────────────────
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")  # Empty = no password required
# Secret used to derive the auth cookie token. Set this in Render for a stable, private secret.
# Falls back to the password so the app still works without extra config, but setting it is better.
SECRET_KEY = os.environ.get("SECRET_KEY", "")
PORT = int(os.environ.get("PORT", "8000"))
HOST = os.environ.get("HOST", "0.0.0.0")
# How often the background task re-scrapes Investegate, in seconds.
# 120s (2 min) is a good balance between freshness and load on Investegate.
SCRAPE_INTERVAL_SECONDS = int(os.environ.get("SCRAPE_INTERVAL_SECONDS", "120"))
# How long fetched announcement bodies stay cached, in seconds. Bodies don't change once published.
DETAIL_CACHE_TTL = int(os.environ.get("DETAIL_CACHE_TTL", "900"))  # 15 min
DETAIL_CACHE_MAX = int(os.environ.get("DETAIL_CACHE_MAX", "600"))  # cap entries to bound memory
# Maximum pages followed per paginated listing (50 rows each). 20 pages is 1,000 rows,
# enough to rebuild a full RNS day after a restart. Steady-state scrapes stop far sooner.
MAX_LISTING_PAGES = int(os.environ.get("MAX_LISTING_PAGES", "20"))
# Postgres connection string. When set, the watchlist is stored in the database and
# survives Render restarts, redeploys and spin-downs. When empty, watchlist.json is used,
# which Render's free tier wipes on every restart. Use the database's Internal URL.
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
# Login lifetime. The cookie is an HMAC of SECRET_KEY, so rotating SECRET_KEY still
# signs everyone out immediately regardless of this value.
SESSION_DAYS = int(os.environ.get("SESSION_DAYS", "30"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("rns-monitor")
# httpx and httpcore log every request at INFO, around 60 lines per scrape cycle, which
# buries genuine warnings in the Render logs. Only their warnings and errors are kept.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# ── London-time "today" ──────────────────────────────────────────────────────
# Render dynos run UTC. During BST, UTC's date lags London by an hour around
# midnight: the daily cache would reset at 1am London and announcements stamped
# with the new London date before UTC midnight would fail the is_today check.
# All date logic therefore uses London's calendar day, matching Investegate.
try:
    from zoneinfo import ZoneInfo
    _LONDON_TZ = ZoneInfo("Europe/London")
except Exception:  # tz database unavailable — fall back to server-local date
    _LONDON_TZ = None

def today_london() -> date:
    return datetime.now(_LONDON_TZ).date() if _LONDON_TZ else date.today()

# Background scraper task handle, populated in the lifespan handler
_background_task: Optional[asyncio.Task] = None

async def _periodic_scrape_loop():
    """Background loop that scrapes Investegate continuously and keeps the cache warm.
    Runs immediately on startup, then every SCRAPE_INTERVAL_SECONDS thereafter."""
    log.info(f"Background scraper starting (interval: {SCRAPE_INTERVAL_SECONDS}s)")
    while True:
        try:
            # Hard ceiling on scrape duration. A single trickling response can hold
            # a read alive past the per-request timeout; without this, one hung scrape
            # holds the scrape lock indefinitely, merges stop, the midnight cache wipe
            # never runs, and the app serves yesterday's snapshot all morning.
            await asyncio.wait_for(scrape_investegate(), timeout=240)
            log.info(f"Background scrape complete. Cache has {daily_cache.count} announcements.")
        except asyncio.TimeoutError:
            log.error("Background scrape exceeded 240s and was cancelled — will retry next interval.")
        except asyncio.CancelledError:
            log.info("Background scraper cancelled — shutting down.")
            raise
        except Exception as e:
            log.exception(f"Background scrape failed: {e}")
        try:
            await asyncio.sleep(SCRAPE_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise

@asynccontextmanager
async def lifespan(_: FastAPI):
    """FastAPI lifespan: spin up the background scraper on startup and tear it down on shutdown."""
    global _background_task
    _background_task = asyncio.create_task(_periodic_scrape_loop())
    try:
        yield
    finally:
        if _background_task and not _background_task.done():
            _background_task.cancel()
            try:
                await _background_task
            except asyncio.CancelledError:
                pass

app = FastAPI(title="h2Radnor RNS Monitor", version="2.2.0", lifespan=lifespan)
# Compress responses. A full day's feed is roughly 300 KB of JSON and shrinks by an order
# of magnitude, which matters for Render's bandwidth allowance with tabs polling all day.
app.add_middleware(GZipMiddleware, minimum_size=1024)

# No CORS middleware: the frontend is served same-origin from this app, so
# cross-origin headers are unnecessary. The previous allow_origins=["*"] with
# allow_credentials=True was an invalid combination browsers reject anyway.

# ── Password Gate ────────────────────────────────────────────────────────────

LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>h2Radnor · Login</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0a0f0d;color:#e8ede9;font-family:'DM Sans',sans-serif;min-height:100vh;display:flex;align-items:center;justify-content:center}
.login{background:#111a15;border:1px solid #1e2e24;border-radius:14px;padding:40px;width:100%;max-width:380px;margin:20px}
h1{font-size:22px;font-weight:700;margin-bottom:6px;letter-spacing:-0.02em}
h1 span{color:#3db86a}
.sub{font-size:13px;color:#5a6e5f;margin-bottom:28px}
input{width:100%;background:#0e1511;border:1px solid #1e2e24;border-radius:8px;padding:12px 16px;color:#e8ede9;font-family:'DM Sans',sans-serif;font-size:14px;outline:none;margin-bottom:16px}
input:focus{border-color:#2d4a36}
button{width:100%;background:#3db86a;color:#0a0f0d;border:none;border-radius:8px;padding:12px;font-family:'DM Sans',sans-serif;font-size:14px;font-weight:600;cursor:pointer}
button:hover{background:#2a7d49}
.err{color:#ef4444;font-size:12px;margin-bottom:12px}
</style>
</head>
<body>
<div class="login">
<h1><span>h2</span>Radnor</h1>
<p class="sub">RNS Monitor · Enter password to continue</p>
<form method="POST" action="/login">
<div class="err" id="err"></div>
<input type="password" name="password" placeholder="Password" autofocus>
<button type="submit">Enter</button>
</form>
</div>
<script>
if(new URLSearchParams(location.search).get('error')){document.getElementById('err').textContent='Incorrect password'}
</script>
</body>
</html>"""

AUTH_COOKIE = "rns_auth"

def _auth_token() -> str:
    """Derive the auth-cookie value from a server secret rather than storing the password itself.
    Stable across restarts as long as SECRET_KEY (or APP_PASSWORD) is stable."""
    secret = (SECRET_KEY or APP_PASSWORD or "rns-monitor").encode()
    return hmac.new(secret, b"rns-monitor-auth-v1", hashlib.sha256).hexdigest()

def _cookie_ok(token: Optional[str]) -> bool:
    """Constant-time comparison of the presented cookie against the expected token."""
    return bool(token) and hmac.compare_digest(token, _auth_token())

@app.get("/login", response_class=HTMLResponse)
async def login_page():
    if not APP_PASSWORD:
        return RedirectResponse("/")
    return HTMLResponse(content=LOGIN_HTML, headers=NO_CACHE)

@app.post("/login")
async def login(request: Request):
    form = await request.form()
    password = form.get("password", "")
    # Compare the submitted password against the real password in constant time.
    if APP_PASSWORD and hmac.compare_digest(password, APP_PASSWORD):
        response = RedirectResponse("/", status_code=302)
        # Store a derived token, never the password itself.
        response.set_cookie(AUTH_COOKIE, _auth_token(), httponly=True, secure=True, samesite="lax", max_age=SESSION_DAYS * 86400)
        return response
    return RedirectResponse("/login?error=1", status_code=302)

# ── Serve Frontend ───────────────────────────────────────────────────────────

STATIC_DIR = Path(__file__).parent / "static"
NO_CACHE = {"Cache-Control": "no-cache, must-revalidate"}

# The frontend's version is a short hash of static/index.html. It is stamped into the
# page as it is served and sent with every feed response (X-App-Version). A tab left open
# all day keeps running the code it loaded, because auto-refresh fetches data, not the
# page, so deploys never reached it. Comparing the two lets the page say an update exists.
_frontend_cache: dict = {"mtime": None, "html": "", "version": ""}

def frontend_page() -> tuple[str, str]:
    """(html with version stamp, version). Re-read only when the file changes."""
    html_path = STATIC_DIR / "index.html"
    try:
        mtime = html_path.stat().st_mtime
    except OSError:
        return "", ""
    if _frontend_cache["mtime"] != mtime:
        raw = html_path.read_text(encoding="utf-8")
        version = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
        stamp = f'<meta name="app-version" content="{version}">'
        html = raw.replace("<head>", "<head>\n" + stamp, 1) if "<head>" in raw else stamp + raw
        _frontend_cache.update(mtime=mtime, html=html, version=version)
    return _frontend_cache["html"], _frontend_cache["version"]

@app.get("/", response_class=HTMLResponse)
async def serve_frontend(request: Request):
    if APP_PASSWORD and not _cookie_ok(request.cookies.get(AUTH_COOKIE)):
        return RedirectResponse("/login")
    html, _ = frontend_page()
    # no-cache means the browser revalidates on every load, so a deploy shows up on the
    # next refresh instead of an old copy of the frontend lingering in the cache.
    return HTMLResponse(content=html, headers=NO_CACHE)

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Protect API routes with password if APP_PASSWORD is set."""
    path = request.url.path
    # Skip auth for login routes, health check, and static assets
    if not APP_PASSWORD or path in ("/login", "/api/health") or not path.startswith("/api/"):
        return await call_next(request)
    if not _cookie_ok(request.cookies.get(AUTH_COOKIE)):
        return Response(content='{"detail":"Not authenticated"}', status_code=401, media_type="application/json")
    return await call_next(request)

# ── Watchlist (persistent) ───────────────────────────────────────────────────

WATCHLIST_FILE = Path(os.environ.get("WATCHLIST_FILE") or Path(__file__).parent / "watchlist.json")

# Tickers tracked as read-across signals to the core watchlist.
# All other watchlist tickers are treated as core by default.
# Investegate sometimes files a company's announcements under a different company code
# from its LSE ticker. I-RES is admitted on Euronext Dublin, and Investegate lists its
# EQS announcements under company code 0QT8 at /company/0QT8, not /company/IRES. Without
# this map, those announcements were never fetched from a company page, their index-page
# rows extracted ticker 0QT8 (not on the watchlist) so they were never flagged, and the
# company-page slug check would have discarded them anyway. Keys are Investegate codes,
# values are the watchlist ticker they belong to. Add an entry whenever a watchlist
# company's announcement URLs show a different "--code/" slug.
TICKER_ALIASES = {
    "0QT8": "IRES",   # Irish Residential Properties REIT (Euronext Dublin line)
}

def canonical_ticker(ticker):
    """Map an Investegate company code onto its watchlist ticker, if aliased."""
    if not ticker:
        return ticker
    return TICKER_ALIASES.get(ticker.upper(), ticker)

def company_codes(ticker):
    """Every Investegate company code whose page should be fetched for this ticker."""
    return [ticker] + [code for code, t in TICKER_ALIASES.items() if t == ticker]

READ_ACROSS_TICKERS = {
    "CRST", "BTRW", "TW.", "GRI", "KNOS", "BBOX", "SAVS", "LSL",
    "ALFA", "WTB", "JET2", "SUS", "SGRO", "SAFE", "FTC",
    "IQE", "TTG", "DSCV", "ABDP", "KEYS",
}

DEFAULT_WATCHLIST = {
    # Core watchlist — companies h2Radnor directly advises or actively covers
    "HSW":  {"name": "Hostelworld Group",                "sector": "Online travel / hostels",              "tier": "core"},
    "VEIL": {"name": "Vietnam Enterprise Investments",   "sector": "Emerging markets investment trust",    "tier": "core"},
    "PSN":  {"name": "Persimmon",                        "sector": "Housebuilding",                        "tier": "core"},
    "FOXT": {"name": "Foxtons Group",                    "sector": "Estate agency / lettings",             "tier": "core"},
    "PHP":  {"name": "Primary Health Properties",        "sector": "Healthcare REIT",                      "tier": "core"},
    "CML":  {"name": "CML Microsystems",                 "sector": "Semiconductors / RF technology",       "tier": "core"},
    "GTLY": {"name": "Gateley Holdings",                 "sector": "Legal / professional services",        "tier": "core"},
    "FNTL": {"name": "Fintel",                           "sector": "Fintech / financial services distribution", "tier": "core"},
    "BMY":  {"name": "Bloomsbury Publishing",            "sector": "Publishing",                           "tier": "core"},
    "SEE":  {"name": "Seeing Machines",                  "sector": "Driver monitoring / AI technology",    "tier": "core"},
    "PPHE": {"name": "PPHE Hotel Group",                 "sector": "Hospitality / hotels",                 "tier": "core"},
    "ROSE": {"name": "Rosebank Industries",              "sector": "Industrials / acquisition vehicle",    "tier": "core"},
    "ALU":  {"name": "Alumasc Group",                    "sector": "Building products / construction",     "tier": "core"},
    "BYG":  {"name": "Big Yellow Group",                 "sector": "Self-storage REIT",                    "tier": "core"},
    "CPP":  {"name": "CPP Group",                        "sector": "Insurance / assistance products",      "tier": "core"},
    "HFG":  {"name": "Hilton Foods",                     "sector": "Food production / FMCG",               "tier": "core"},
    "IDOX": {"name": "Idox",                             "sector": "GovTech / software",                   "tier": "core"},
    "IPF":  {"name": "International Personal Finance",   "sector": "Consumer finance / lending",           "tier": "core"},
    "IRES": {"name": "Irish Residential Properties REIT","sector": "Residential property REIT",            "tier": "core"},
    "MTEC": {"name": "Made Tech Group",                  "sector": "Technology / digital services",        "tier": "core"},
    "NRR":  {"name": "NewRiver REIT",                    "sector": "Retail property REIT",                 "tier": "core"},
    "NFG":  {"name": "Next 15 Group",                    "sector": "Marketing / communications",           "tier": "core"},
    "REC":  {"name": "Record",                           "sector": "Currency management / financial services", "tier": "core"},
    "TRI":  {"name": "Trifast",                          "sector": "Industrial fasteners / manufacturing", "tier": "core"},
    # Read-across — tracked for read-through signals to core watchlist
    "CRST": {"name": "Crest Nicholson",                  "sector": "Housebuilding",                        "tier": "read_across"},
    "BTRW": {"name": "Barratt Redrow",                   "sector": "Housebuilding",                        "tier": "read_across"},
    "TW.":  {"name": "Taylor Wimpey",                    "sector": "Housebuilding",                        "tier": "read_across"},
    "GRI":  {"name": "Grainger",                         "sector": "Residential property / REIT",          "tier": "read_across"},
    "KNOS": {"name": "Kainos Group",                     "sector": "GovTech / digital transformation",     "tier": "read_across"},
    "BBOX": {"name": "Tritax Big Box REIT",              "sector": "Logistics REIT",                       "tier": "read_across"},
    "SAVS": {"name": "Savills",                          "sector": "Property services / estate agency",    "tier": "read_across"},
    "LSL":  {"name": "LSL Property Services",            "sector": "Estate agency / surveying",            "tier": "read_across"},
    "ALFA": {"name": "Alfa Financial Software",          "sector": "Specialist software",                  "tier": "read_across"},
    "WTB":  {"name": "Whitbread",                        "sector": "Hospitality / hotels",                 "tier": "read_across"},
    "JET2": {"name": "Jet2",                             "sector": "Leisure travel / airline",             "tier": "read_across"},
    "SUS":  {"name": "S&U",                              "sector": "Consumer finance / motor lending",     "tier": "read_across"},
    "SGRO": {"name": "SEGRO",                            "sector": "Industrial REIT",                      "tier": "read_across"},
    "SAFE": {"name": "Safestore Holdings",               "sector": "Self-storage REIT",                    "tier": "read_across"},
    "FTC":  {"name": "Filtronic",                        "sector": "RF / microwave components",            "tier": "read_across"},
    "IQE":  {"name": "IQE",                               "sector": "Compound semiconductors / RF materials", "tier": "read_across"},
    "TTG":  {"name": "TT Electronics",                    "sector": "Electronic components / manufacturing", "tier": "read_across"},
    "DSCV": {"name": "discoverIE Group",                  "sector": "Specialist electronic components",      "tier": "read_across"},
    "ABDP": {"name": "AB Dynamics",                       "sector": "Automotive test / simulation",          "tier": "read_across"},
    "KEYS": {"name": "Keystone Law Group",                "sector": "Legal / professional services",         "tier": "read_across"},
    "BOOK": {"name": "Literacy Capital",                  "sector": "Closed-end investment company",         "tier": "core"},
}

def _ensure_tiers(wl: dict) -> dict:
    """Ensure every watchlist entry has a 'tier' field (backward compat for old JSON files)."""
    for ticker, info in wl.items():
        if "tier" not in info:
            info["tier"] = "read_across" if ticker in READ_ACROSS_TICKERS else "core"
    return wl

class WatchlistStore:
    """Where the watchlist lives.

    Postgres when DATABASE_URL is set, so additions and removals made in the app survive
    Render restarts, redeploys and spin-downs. Otherwise watchlist.json, which Render's
    free tier wipes on every restart (fine for local use).

    Names removed in the app are remembered in a separate list. That lets new defaults
    added to DEFAULT_WATCHLIST in code appear automatically on the next deploy, without
    bringing back a company someone deliberately removed.

    Every method is safe to call if the database is unreachable: the in-memory watchlist
    keeps working, and the failure is reported through status() and /api/health.
    """

    def __init__(self):
        self.backend = "json"
        self.last_error: Optional[str] = None
        self._pg = None
        if DATABASE_URL:
            try:
                import psycopg
                self._pg = psycopg
                with self._conn() as c:
                    c.execute("""CREATE TABLE IF NOT EXISTS watchlist (
                        ticker TEXT PRIMARY KEY,
                        name   TEXT NOT NULL,
                        sector TEXT NOT NULL DEFAULT '',
                        tier   TEXT NOT NULL DEFAULT 'core')""")
                    c.execute("CREATE TABLE IF NOT EXISTS watchlist_removed (ticker TEXT PRIMARY KEY)")
                self.backend = "postgres"
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {str(e)[:200]}"
                log.error(f"DATABASE_URL is set but Postgres is unavailable; using watchlist.json. {self.last_error}")

    def _conn(self):
        return self._pg.connect(DATABASE_URL, autocommit=True, connect_timeout=10)

    # ── JSON helpers ────────────────────────────────────────────────────────
    def _read_json(self):
        """Returns (watchlist, removed). Accepts the old format (a plain dict of names)."""
        if not WATCHLIST_FILE.exists():
            return None, set()
        try:
            with open(WATCHLIST_FILE, "r") as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError):
            return None, set()
        if isinstance(data, dict) and "watchlist" in data:
            return data.get("watchlist") or {}, set(data.get("removed") or [])
        return data, set()

    def _write_json(self, wl, removed):
        try:
            with open(WATCHLIST_FILE, "w") as f:
                json.dump({"watchlist": wl, "removed": sorted(removed)}, f, indent=2)
        except IOError as e:
            self.last_error = f"could not write watchlist.json: {e}"
            log.warning(self.last_error)

    def _removed_json(self):
        return self._read_json()[1]

    # ── Public API ──────────────────────────────────────────────────────────
    def load(self) -> dict:
        """Current watchlist, with any new code defaults merged in (unless removed)."""
        if self.backend == "postgres":
            try:
                with self._conn() as c:
                    rows = c.execute("SELECT ticker, name, sector, tier FROM watchlist").fetchall()
                    removed = {r[0] for r in c.execute("SELECT ticker FROM watchlist_removed").fetchall()}
                    wl = {t: {"name": n, "sector": sec, "tier": tier} for t, n, sec, tier in rows}
                    for t, info in DEFAULT_WATCHLIST.items():
                        if t not in wl and t not in removed:
                            c.execute("INSERT INTO watchlist (ticker, name, sector, tier) VALUES (%s,%s,%s,%s) "
                                      "ON CONFLICT (ticker) DO NOTHING",
                                      (t, info["name"], info.get("sector", ""), info.get("tier", "core")))
                            wl[t] = dict(info)
                self.last_error = None
                return _ensure_tiers(wl)
            except Exception as e:
                self.last_error = f"load failed: {type(e).__name__}: {str(e)[:200]}"
                log.error(f"Watchlist load from Postgres failed; using defaults. {self.last_error}")
                return _ensure_tiers({t: dict(i) for t, i in DEFAULT_WATCHLIST.items()})
        wl, removed = self._read_json()
        wl = dict(wl) if wl else {}
        for t, info in DEFAULT_WATCHLIST.items():
            if t not in wl and t not in removed:
                wl[t] = dict(info)
        wl = _ensure_tiers(wl)
        self._write_json(wl, removed)
        return wl

    def upsert(self, ticker: str, info: dict) -> Optional[str]:
        """Add or update one company. Returns an error string, or None on success."""
        if self.backend == "postgres":
            try:
                with self._conn() as c:
                    c.execute("INSERT INTO watchlist (ticker, name, sector, tier) VALUES (%s,%s,%s,%s) "
                              "ON CONFLICT (ticker) DO UPDATE SET name=EXCLUDED.name, "
                              "sector=EXCLUDED.sector, tier=EXCLUDED.tier",
                              (ticker, info["name"], info.get("sector", ""), info.get("tier", "core")))
                    c.execute("DELETE FROM watchlist_removed WHERE ticker=%s", (ticker,))
                self.last_error = None
                return None
            except Exception as e:
                self.last_error = f"save failed: {type(e).__name__}: {str(e)[:200]}"
                log.error(self.last_error)
                return self.last_error
        removed = self._removed_json()
        removed.discard(ticker)
        self._write_json(WATCHLIST, removed)
        return None

    def remove(self, ticker: str) -> Optional[str]:
        if self.backend == "postgres":
            try:
                with self._conn() as c:
                    c.execute("DELETE FROM watchlist WHERE ticker=%s", (ticker,))
                    c.execute("INSERT INTO watchlist_removed (ticker) VALUES (%s) ON CONFLICT DO NOTHING", (ticker,))
                self.last_error = None
                return None
            except Exception as e:
                self.last_error = f"remove failed: {type(e).__name__}: {str(e)[:200]}"
                log.error(self.last_error)
                return self.last_error
        removed = self._removed_json()
        removed.add(ticker)
        self._write_json(WATCHLIST, removed)
        return None

    def reset(self) -> Optional[str]:
        """Back to DEFAULT_WATCHLIST, forgetting every removal."""
        if self.backend == "postgres":
            try:
                with self._conn() as c:
                    c.execute("DELETE FROM watchlist")
                    c.execute("DELETE FROM watchlist_removed")
                    for t, info in DEFAULT_WATCHLIST.items():
                        c.execute("INSERT INTO watchlist (ticker, name, sector, tier) VALUES (%s,%s,%s,%s)",
                                  (t, info["name"], info.get("sector", ""), info.get("tier", "core")))
                self.last_error = None
                return None
            except Exception as e:
                self.last_error = f"reset failed: {type(e).__name__}: {str(e)[:200]}"
                log.error(self.last_error)
                return self.last_error
        self._write_json({t: dict(i) for t, i in DEFAULT_WATCHLIST.items()}, set())
        return None

    def status(self) -> dict:
        durable = self.backend == "postgres"
        return {
            "backend": self.backend,
            "durable": durable,
            "note": ("Watchlist changes are saved to Postgres and survive restarts."
                     if durable else
                     "Watchlist changes are saved to watchlist.json, which Render's free tier "
                     "wipes on every restart. Set DATABASE_URL to keep them."),
            "database_url_set": bool(DATABASE_URL),
            "last_error": self.last_error,
        }

watchlist_store = WatchlistStore()
WATCHLIST = watchlist_store.load()

# ── Read-Across Matrix ───────────────────────────────────────────────────────
# When a read-across company announces something, these are the core watchlist
# companies affected, the type of relationship, and why it matters.

REL_TYPES = {
    "DIRECT":   {"label": "Direct peer",   "short": "D", "color": "#3db86a", "bg": "rgba(61,184,106,0.18)",  "border": "rgba(61,184,106,0.35)"},
    "SUPPLY":   {"label": "Supply chain",  "short": "S", "color": "#f0c040", "bg": "rgba(240,192,64,0.18)",  "border": "rgba(240,192,64,0.35)"},
    "MACRO":    {"label": "Macro / rates", "short": "M", "color": "#5ba8f7", "bg": "rgba(91,168,247,0.18)",  "border": "rgba(91,168,247,0.35)"},
    "THEMATIC": {"label": "Thematic",      "short": "T", "color": "#c084fc", "bg": "rgba(192,132,252,0.18)", "border": "rgba(192,132,252,0.35)"},
}

READ_ACROSS_MATRIX = {
    "CRST": {
        "PSN":  {"type": "DIRECT",   "strength": 3, "note": "Direct housebuilding peer — completions volume, ASP, and margin commentary all read directly across to PSN"},
        "ALU":  {"type": "SUPPLY",   "strength": 2, "note": "Alumasc supplies building products to volume housebuilders; CRST build rates affect ALU demand pipeline"},
        "FOXT": {"type": "MACRO",    "strength": 2, "note": "Shared housing market exposure — transaction volumes, Help to Buy withdrawal, mortgage rate sensitivity"},
        "GTLY": {"type": "SUPPLY",   "strength": 1, "note": "Gateley provides legal and conveyancing services to housebuilders and homebuyers; CRST volumes signal workload"},
        "IRES": {"type": "MACRO",    "strength": 1, "note": "New build supply affects PRS rental demand and residential property values"},
    },
    "BTRW": {
        "PSN":  {"type": "DIRECT",   "strength": 3, "note": "Largest UK housebuilder post-merger — BTRW is the primary benchmark for PSN on completions, ASP, margin, and land bank"},
        "ALU":  {"type": "SUPPLY",   "strength": 2, "note": "Alumasc building products; BTRW is one of the largest volume purchasers in the sector"},
        "FOXT": {"type": "MACRO",    "strength": 2, "note": "Housing market transaction volumes and pricing sentiment — Foxtons mortgage completions directly linked"},
        "GTLY": {"type": "SUPPLY",   "strength": 1, "note": "Conveyancing and development legal work reads through to Gateley pipeline"},
        "IRES": {"type": "MACRO",    "strength": 1, "note": "Residential supply/demand balance; new build completions vs PRS demand"},
    },
    "TW.": {
        "PSN":  {"type": "DIRECT",   "strength": 3, "note": "Large-cap housebuilding benchmark — TW. guidance and ASP commentary is the clearest read-across for PSN"},
        "ALU":  {"type": "SUPPLY",   "strength": 2, "note": "Building products demand linked to Taylor Wimpey build volumes"},
        "FOXT": {"type": "MACRO",    "strength": 2, "note": "Shared housing market conditions — both sensitive to Help to Buy, mortgage affordability, land availability"},
        "GTLY": {"type": "SUPPLY",   "strength": 1, "note": "Legal services for housebuilding transactions and land acquisition"},
        "IRES": {"type": "MACRO",    "strength": 1, "note": "New build completions affect rental market supply and demand"},
    },
    "GRI": {
        "IRES": {"type": "DIRECT",   "strength": 3, "note": "Direct peer — both are listed UK/European residential REITs focused on the private rented sector; rental growth, occupancy, and cost of debt commentary all read across"},
        "FOXT": {"type": "MACRO",    "strength": 2, "note": "Both exposed to London and regional rental market dynamics — GRI occupancy and rental reversion signals Foxtons lettings pipeline"},
        "BYG":  {"type": "MACRO",    "strength": 1, "note": "Shared REIT sector sensitivity to interest rates and cost of capital"},
        "PHP":  {"type": "MACRO",    "strength": 1, "note": "REIT sector — rate environment affects all listed property companies"},
        "NRR":  {"type": "MACRO",    "strength": 1, "note": "REIT sector rate and refinancing read-across"},
        "PSN":  {"type": "MACRO",    "strength": 1, "note": "Residential supply pipeline; new build starts affect PRS demand trajectory"},
    },
    "KNOS": {
        "IDOX": {"type": "DIRECT",   "strength": 3, "note": "Direct GovTech peer — both serve UK public sector digital transformation; Kainos contract wins and budget commentary read directly across to Idox pipeline"},
        "MTEC": {"type": "DIRECT",   "strength": 3, "note": "Direct peer — both are UK government digital delivery partners; Kainos results signal public sector IT spend environment for Made Tech"},
    },
    "BBOX": {
        "NRR":  {"type": "MACRO",    "strength": 2, "note": "REIT sector — cost of capital, yield movements, and refinancing conditions affect all listed property vehicles"},
        "BYG":  {"type": "MACRO",    "strength": 2, "note": "REIT sector rate sensitivity; BBOX commentary on financing conditions reads across"},
        "PHP":  {"type": "MACRO",    "strength": 1, "note": "Long-income REIT sector — interest rate and inflation-linked income read-across"},
        "IRES": {"type": "MACRO",    "strength": 1, "note": "REIT sector cost of capital and debt market conditions"},
    },
    "SAVS": {
        "FOXT": {"type": "DIRECT",   "strength": 3, "note": "Property services peer — SAVS is higher-end and global vs Foxtons London residential, but UK transaction volume commentary reads directly across"},
        "PSN":  {"type": "MACRO",    "strength": 2, "note": "UK housing market — Savills residential transaction volumes and buyer sentiment signal housebuilder demand"},
        "NRR":  {"type": "MACRO",    "strength": 1, "note": "Commercial property market sentiment affects retail REIT asset valuations"},
        "GTLY": {"type": "THEMATIC", "strength": 1, "note": "Both benefit from property market activity — legal services (GTLY) and advisory services (SAVS) move together"},
        "IRES": {"type": "MACRO",    "strength": 1, "note": "Residential property market health"},
    },
    "LSL": {
        "FOXT": {"type": "DIRECT",   "strength": 3, "note": "Direct estate agency peer — both UK residential sales and lettings; LSL includes Your Move and Reeds Rains, overlapping Foxtons geography in London"},
        "PSN":  {"type": "MACRO",    "strength": 2, "note": "UK housing market transaction volumes — LSL completions data is a live indicator for housebuilder demand"},
        "IRES": {"type": "MACRO",    "strength": 1, "note": "Lettings market — rental demand and supply dynamics"},
        "GTLY": {"type": "MACRO",    "strength": 1, "note": "Property transaction volumes drive both legal and agency revenue"},
    },
    "ALFA": {
        "FNTL": {"type": "THEMATIC", "strength": 2, "note": "B2B financial services technology — ALFA serves asset finance software, Fintel serves IFA distribution tech; both are niche fintech platforms with sticky recurring revenue"},
        "IPF":  {"type": "THEMATIC", "strength": 1, "note": "Consumer/specialist finance sector — technology investment trends and FCA regulatory environment read across"},
        "CPP":  {"type": "THEMATIC", "strength": 1, "note": "Financial services technology and distribution infrastructure"},
    },
    "WTB": {
        "PPHE": {"type": "DIRECT",   "strength": 3, "note": "Premier Inn is the UK hotel sector benchmark — WTB RevPAR, occupancy, and cost commentary reads directly across to PPHE despite PPHE's upscale positioning"},
        "HSW":  {"type": "THEMATIC", "strength": 2, "note": "Both exposed to leisure and business travel demand cycles — WTB UK occupancy trends signal broader accommodation booking environment for Hostelworld"},
        "NRR":  {"type": "MACRO",    "strength": 1, "note": "Consumer-facing leisure and hospitality spending trends affect retail/leisure property"},
    },
    "JET2": {
        "HSW":  {"type": "THEMATIC", "strength": 3, "note": "Leisure travel demand — JET2 forward booking data and customer pricing trends are the strongest read-across for Hostelworld accommodation demand; same traveller cohort"},
        "PPHE": {"type": "THEMATIC", "strength": 2, "note": "Both exposed to UK outbound and domestic leisure travel; JET2 passenger volumes signal PPHE city and resort occupancy"},
    },
    "SUS": {
        "IPF":  {"type": "DIRECT",   "strength": 3, "note": "Direct consumer finance peer — both are specialist non-standard lenders; SUS focuses on motor and bridging finance, IPF on home credit; FCA regulatory tone and consumer credit trends read across"},
        "CPP":  {"type": "THEMATIC", "strength": 2, "note": "Both serve financially non-prime consumers; shared exposure to consumer stress, arrears trends, and FCA regulatory environment"},
        "FNTL": {"type": "THEMATIC", "strength": 1, "note": "Financial services distribution and regulatory read-across"},
    },
    "SGRO": {
        "NRR":  {"type": "MACRO",    "strength": 2, "note": "REIT sector — cost of capital, debt refinancing, and yield compression all read across from SEGRO's financing disclosures"},
        "BYG":  {"type": "MACRO",    "strength": 2, "note": "REIT sector rate sensitivity — SGRO is a large-cap REIT bellwether"},
        "PHP":  {"type": "MACRO",    "strength": 1, "note": "Long-income REIT sector financing conditions"},
        "IRES": {"type": "MACRO",    "strength": 1, "note": "REIT sector debt markets and cost of capital"},
    },
    "SAFE": {
        "BYG":  {"type": "DIRECT",   "strength": 3, "note": "Direct peer — Safestore and Big Yellow are the two main listed UK self-storage REITs; RevPASF, occupancy, and rate commentary reads directly across"},
        "NRR":  {"type": "MACRO",    "strength": 1, "note": "REIT sector rates and refinancing environment"},
    },
    "FTC": {
        "CML":  {"type": "THEMATIC", "strength": 2, "note": "Shared RF, microwave and wireless end markets — Filtronic demand across telecoms infrastructure, aerospace/defence and space reads through to CML's RF and mixed-signal semiconductor exposure"},
    },
    "IQE": {
        "CML":  {"type": "THEMATIC", "strength": 2, "note": "Shared RF, wireless and photonics semiconductor end markets — IQE compound-semiconductor demand reads across to CML's RF and mixed-signal exposure; with Filtronic this forms an RF/semiconductor cluster"},
    },
    "TTG": {
        "TRI":  {"type": "THEMATIC", "strength": 2, "note": "Electronic components and manufacturing into shared industrial, aerospace/defence and medical OEM end markets — TT Electronics order-book and demand commentary reads across to Trifast's components-into-OEM supply chain"},
    },
    "DSCV": {
        "TRI":  {"type": "THEMATIC", "strength": 2, "note": "Specialist electronic components for industrial OEMs — discoverIE end-market demand across renewables, transportation, medical and industrial overlaps Trifast's OEM supply-chain exposure"},
    },
    "ABDP": {
        "SEE":  {"type": "THEMATIC", "strength": 2, "note": "Automotive test and driving-simulation adjacency — AB Dynamics demand tracks ADAS and vehicle-autonomy development spend, the same end market driving Seeing Machines' driver-monitoring systems"},
    },
    "KEYS": {
        "GTLY": {"type": "DIRECT",   "strength": 3, "note": "Direct listed legal-services peer to Gateley — fee income, fee-earner recruitment, realisation rates and chargeable-hours commentary read straight across"},
    },
}

# ── Daily Announcement Cache ─────────────────────────────────────────────────
# Stores all announcements seen today. Merges with fresh scrapes so earlier
# announcements that fall off index pages are preserved all day.

class DailyCache:
    def __init__(self, cache_file: Path):
        self.cache_file = cache_file
        self.announcements: dict[str, dict] = {}
        self.cache_date: Optional[str] = None
        self.last_scrape: Optional[datetime] = None
        self.last_errors: list[str] = []
        self.last_stats: dict = {}
        self._load()

    def _load(self):
        """Load cache from disk if it exists and is from today (London calendar day)."""
        if self.cache_file.exists():
            try:
                with open(self.cache_file, "r") as f:
                    data = json.load(f)
                if data.get("date") == today_london().isoformat():
                    self.announcements = data.get("announcements", {})
                    self.cache_date = data["date"]
                    return
            except (json.JSONDecodeError, IOError):
                pass
        self.announcements = {}
        self.cache_date = today_london().isoformat()

    def _save(self):
        """Save cache to disk."""
        try:
            with open(self.cache_file, "w") as f:
                json.dump({
                    "date": self.cache_date,
                    "announcements": self.announcements,
                }, f)
        except IOError:
            pass

    def _check_date(self):
        """Reset cache at midnight (London time, matching Investegate's date stamps)."""
        today = today_london().isoformat()
        if self.cache_date != today:
            self.announcements = {}
            self.cache_date = today
            self._save()

    def merge(self, new_announcements: list) -> list:
        """Merge new scrape results into the cache, return the full combined list.
        First reindexes existing cache entries under the normalised key so any
        legacy duplicates from earlier scrapes collapse on the next run."""
        self._check_date()
        # Reindex existing entries so old per-company-name keys collapse into URL/ticker-based keys
        reindexed = {}
        for v in self.announcements.values():
            ann = Announcement(**v) if not isinstance(v, Announcement) else v
            new_key = make_dedup_key(ann.ticker, ann.company, ann.headline, ann.url)
            # If two old entries collapse to the same new key, keep the newer one
            # (we have no timestamp on the dict value, so last write wins)
            reindexed[new_key] = v
        self.announcements = reindexed
        # Now merge in the fresh results — they take precedence over any older copy
        for ann in new_announcements:
            key = make_dedup_key(ann.ticker, ann.company, ann.headline, ann.url)
            self.announcements[key] = ann.model_dump()
        self.last_scrape = datetime.now()
        self._save()
        return [Announcement(**v) for v in self.announcements.values()]

    def get_today(self) -> list:
        """Return cached announcements for TODAY only. This is the serve-time gate:
        endpoints must read through this, never through .announcements directly.
        Two layers: _check_date wipes the cache if the calendar day has rolled over,
        and the date_str filter drops any individual entry stamped with another day.
        Between them, yesterday's announcements cannot reach the frontend even if
        the background scraper stalls overnight and the midnight wipe never runs."""
        self._check_date()
        # Accept every day in the current session (today plus a preceding weekend),
        # so a Sunday takeover statement is served on Monday rather than filtered out.
        window = {d.strftime("%d/%m/%Y") for d in trading_window()}
        out = []
        for v in self.announcements.values():
            ann = Announcement(**v)
            if ann.date_str and ann.date_str not in window:
                continue
            out.append(ann)
        return out

    @property
    def count(self):
        self._check_date()
        return len(self.announcements)

CACHE_FILE = Path(os.environ.get("CACHE_FILE") or Path(__file__).parent / "cache_today.json")
daily_cache = DailyCache(CACHE_FILE)

RELEVANT_SECTORS = [
    "travel", "hospitality", "leisure", "hotel", "hostel", "tourism",
    "housebuilding", "housebuilder", "property", "estate", "real estate",
    "reit", "self-storage", "storage", "lettings", "residential",
    "healthcare", "health", "pharma", "medical",
    "technology", "semiconductor", "ai", "driver", "adas", "software",
    "govtech", "digital services", "saas", "regtech",
    "professional services", "legal",
    "fintech", "financial services", "insurance", "wealth",
    "consumer finance", "lending", "currency", "fx",
    "publishing", "media", "content", "marketing", "communications",
    "advertising", "pr",
    "industrial", "engineering", "acquisition", "manufacturing",
    "fastener", "building products", "construction",
    "emerging market", "investment trust",
    "fmcg", "consumer", "food", "beverage", "consumer goods",
    # Energy, added September 2026: oil and gas, power and renewables names now appear
    # in the feed and rank as sector relevant rather than being excluded.
    "energy", "oil", "gas", "petroleum", "power", "renewable*", "utilities", "utility",
    "hydrogen", "solar", "wind", "nuclear",
]

# Mining and metals. These are no longer dropped by the server: they are flagged with
# excluded_sector=True and the frontend hides them behind a switch you can turn off, so
# nothing is ever invisible. M&A announcements are never flagged.
# Oil, gas and energy names were removed from this list in September 2026.
EXCLUDED_KEYWORDS = [
    "mining", "resources", "metals",
    "gold", "silver", "copper", "lithium", "coal", "diamond", "exploration",
]
EXCLUDED_COMPANIES = {
    "anglo american", "glencore", "antofagasta", "fresnillo",
    "rio tinto", "bhp", "endeavour mining",
    "centamin", "hochschild", "ferrexpo", "kenmare", "central asia metals",
    "greatland gold", "pan african", "thungela",
}
# A company whose name contains one of these is an energy name and is never excluded,
# even if it also contains a mining word (for example "Pantheon Resources", an oil
# explorer, is excluded because nothing in its name says energy, but "Europa Oil & Gas
# Resources" would not be).
ENERGY_TERMS = [
    "energy", "oil", "gas", "petroleum", "power", "renewable*", "utilities",
    "hydrogen", "solar", "wind", "nuclear",
]

EXCLUDED_TYPES = [
    # Pure noise — drop entirely, even for watchlist companies. These are administrative
    # filings or fund-trust boilerplate where there's no plausible IR signal.
    # Most other regulatory boilerplate (block listing, TVR, Form 8, scheme docs etc.)
    # is now categorised as "Admin" so watchlist override still surfaces them.
    "factsheet", "monthly factsheet", "portfolio update", "portfolio holdings",
    "net asset value", "monthly nav", "weekly nav", "daily nav",
    "monthly update", "investment update",
    # Re-issues / corrections — same content as the original announcement, just reposted
    "replacement:", "amendment:", "correction:", "additional information:",
    "re:", "re :",
    # Calendar admin where no actual announcement is being made
    "notice of results", "notice of agm", "notice of meeting", "financial calendar",
    # Confirmation of cancellation / redemption of shares (different from delisting itself)
    "cancellation of shares", "redemption",
]


# ── Models ───────────────────────────────────────────────────────────────────

class Announcement(BaseModel):
    time: str
    company: str
    ticker: Optional[str] = None
    headline: str
    category: Optional[str] = None
    url: Optional[str] = None
    source_feed: str = "RNS"
    is_watchlist: bool = False
    sector_relevant: bool = False
    source: str = "investegate"
    date_str: str = ""
    # Mining and metals companies. Hidden in the feed by default behind a switch; never
    # set on M&A or watchlist announcements.
    excluded_sector: bool = False

class ScrapeResponse(BaseModel):
    announcements: list[Announcement]
    total_raw: int
    total_filtered: int
    scraped_at: str
    date: str
    source: str
    errors: list[str] = []
    # Categories the UI should default-hide. Client may toggle these on per-category.
    default_hidden_categories: list[str] = []

class DetailResponse(BaseModel):
    url: str
    company: str
    headline: str
    body_text: str
    body_html: str
    key_figures: list[str]
    ai_summary: Optional[str] = None
    rns_number: Optional[str] = None
    fetched_at: str
    error: Optional[str] = None
    # False when the page Investegate served contained no release text, only its own
    # summary, disclaimer, sidebar and footer. Key figures are then left empty rather than
    # being extracted from that page furniture.
    body_available: bool = True

class ShareholdingDetail(BaseModel):
    url: str
    company: str
    holder: Optional[str] = None
    new_pct: Optional[float] = None
    old_pct: Optional[float] = None
    direction: Optional[str] = None
    change_pp: Optional[float] = None
    voting_rights: Optional[str] = None
    date_of_change: Optional[str] = None
    summary: Optional[str] = None
    fetched_at: str
    error: Optional[str] = None

# ── Helpers ──────────────────────────────────────────────────────────────────

def make_dedup_key(ticker, company, headline, url=None):
    """Build a normalised dedup key so the same announcement can't slip through under
    two slightly-different company-name strings or headline whitespace variants.

    Uses the announcement URL as the primary key when available — it's unique per
    announcement, so two same-day filings with identical headlines (e.g. two TR-1s
    both titled "Holding(s) in Company") are kept as separate rows rather than the
    later one silently overwriting the earlier. The same announcement scraped from
    multiple index pages shares one URL and still collapses to a single row.

    Falls back to ticker (the only stable cross-source signal), then to a
    normalised company name.
    """
    if url:
        return f"U:{url}"
    # Normalise headline: lowercase, collapse all whitespace
    h = re.sub(r'\s+', ' ', (headline or "").strip().lower())
    if ticker:
        return f"T:{ticker.upper()}|{h}"
    # No ticker — strip parenthetical tickers and extra whitespace from company name
    c = re.sub(r'\([A-Z0-9\.]{2,6}\)', '', (company or "")).strip().lower()
    c = re.sub(r'\s+', ' ', c)
    return f"C:{c}|{h}"

# Word-boundary keyword matching. Naive substring checks caused a family of
# mis-classifications: "deal" matched "Dealings", "director" matched "Directorate",
# "nav" matched "Navigator", "ai" matched "chairman", "gold" matched "Goldman Sachs".
# _kw() anchors keywords at word boundaries. A trailing '*' allows suffixes for
# stems ("fundrais*" matches fundraise/fundraising). Boundaries are only asserted
# where the keyword edge is alphanumeric, so entries like "re:" still work.
_KW_PATTERNS: dict[str, re.Pattern] = {}

def _kw(text: str, keyword: str) -> bool:
    pat = _KW_PATTERNS.get(keyword)
    if pat is None:
        stem = keyword.endswith("*")
        core = re.escape(keyword[:-1] if stem else keyword)
        prefix = r'\b' if keyword[0].isalnum() else ''
        if stem:
            suffix = r'\w*'
        else:
            last = keyword[-1]
            suffix = r'\b' if last.isalnum() else ''
        pat = re.compile(prefix + core + suffix)
        _KW_PATTERNS[keyword] = pat
    return bool(pat.search(text))

def _any_kw(text: str, keywords) -> bool:
    return any(_kw(text, k) for k in keywords)

# Market/venue tokens that appear in parentheses in company cells but are not tickers.
_NON_TICKER_TOKENS = {"AIM", "LSE", "FTSE", "PLC", "AQSE", "NEX", "GBP", "GBX", "USD", "EUR"}

def extract_ticker(company_text):
    matches = re.findall(r'\(([A-Z0-9\.]{2,6})\)', company_text)
    # Take the last parenthesised token that isn't a market/venue label,
    # so "Foo Group (AIM)" doesn't yield ticker "AIM".
    for m in reversed(matches):
        if m not in _NON_TICKER_TOKENS:
            return canonical_ticker(m)
    # Fallback: match a watchlist name only when the company cell starts with it
    # and the name isn't a prefix of a longer word — a bare substring check let
    # short names like "Record" attach to unrelated companies.
    cl = company_text.lower().strip()
    for ticker, info in WATCHLIST.items():
        name = info["name"].lower()
        if cl.startswith(name) and (len(cl) == len(name) or not cl[len(name)].isalnum()):
            return ticker
    return None

def clean_company_name(raw_text):
    return re.sub(r'\s+', ' ', raw_text.replace("Factsheet", "").strip())

def is_excluded_company(company):
    """True for mining and metals companies (see EXCLUDED_KEYWORDS). Energy names are
    never excluded."""
    cl = (company or "").lower()
    if _any_kw(cl, ENERGY_TERMS):
        return False
    return _any_kw(cl, EXCLUDED_COMPANIES) or _any_kw(cl, EXCLUDED_KEYWORDS)

def is_material_type(headline):
    """Include everything by default, only exclude known noise types."""
    hl = headline.lower()
    if _any_kw(hl, EXCLUDED_TYPES): return False
    return True

def is_sector_relevant(company, headline):
    combined = f"{company} {headline}".lower()
    return _any_kw(combined, RELEVANT_SECTORS)

def categorise(headline, source_feed="RNS"):
    h = headline.lower()
    # Early disambiguation: compound phrases that contain keywords from other buckets.
    # "Voting Results" contains 'results' but is admin. "Interim Dividend" contains 'interim'.
    # "Price Monitoring Extension" contains 'extension' (a Contract keyword).
    # "Publication of Annual Report" contains 'annual report' (a Results keyword).
    # Without this pre-check the broad keyword cascade below would mis-tag them.
    if _any_kw(h, [
        "voting results", "result of agm", "result of egm", "result of meeting",
        "result of general meeting", "results of general meeting",
        "agm voting", "agm statement", "results of agm", "results of egm",
        "price monitoring extension", "second price monitoring",
        "block listing", "block-listing",
        "publication of annual report", "posting of annual report",
    ]):
        return "Admin"
    # Tender offers are capital returns (usually investment-trust buybacks), not takeovers.
    # Checked early because "Results of Tender Offer" would otherwise tag as Results.
    if _any_kw(h, ["tender offer", "tender offers"]):
        return "Buyback"
    # Takeover Code vocabulary. Checked this early deliberately: it sits above Results,
    # Contract and Regulatory, each of which previously captured M&A headlines first
    # ("Merger Agreement" -> Contract via 'agreement', "Extension of PUSU Deadline" ->
    # Contract via 'extension', "Court Sanction of Scheme" -> Regulatory via 'sanction',
    # "Results of Court Meeting" -> Results via 'results'). A large share of the offer
    # lifecycle had no keyword at all (Rule 2.4 / 2.7 announcements, approaches, press
    # speculation, irrevocables, acceptances, formal sale processes, scheme milestones)
    # and fell through to Other. Bare 'offer' is intentionally absent so Open Offer,
    # Retail Offer and Offer for Subscription stay Fundraise.
    if _any_kw(h, [
        # Code rule references
        "rule 2.4", "rule 2.6", "rule 2.7", "rule 2.8", "rule 2.9", "rule 2.10",
        "rule 2.11", "rule 2.12", "rule 9 waiver", "dispensation from rule 9", "whitewash",
        # Approaches and speculation
        "possible offer", "firm offer", "firm intention to make",
        "statement re approach", "statement regarding approach", "response to approach",
        "approach regarding", "bid approach", "unsolicited approach", "receipt of approach",
        "press speculation", "media speculation", "market speculation",
        "response to speculation", "statement re speculation", "statement regarding speculation",
        # Offers
        "recommended offer", "recommended cash", "recommended final offer", "cash offer",
        "final offer", "revised offer", "increased offer", "improved offer", "competing offer",
        "mandatory offer", "offer update", "update on offer", "offer period",
        "offer unconditional", "offer declared unconditional", "offer lapsed",
        "offer withdrawn", "withdrawal of offer", "termination of offer",
        "no intention to make an offer", "no intention to bid",
        "unconditional in all respects", "wholly unconditional", "unconditional",
        "level of acceptances", "acceptance level", "acceptances",
        "irrevocable undertaking", "irrevocable undertakings", "irrevocables",
        "put up or shut up", "pusu", "formal sale process",
        # Schemes of arrangement
        "scheme of arrangement", "scheme document", "publication of scheme",
        "posting of scheme", "scheme sanctioned", "scheme effective",
        "scheme becomes effective", "sanction of scheme", "court sanction",
        "court meeting", "court hearing",
        # Deal structures
        "merger agreement", "proposed merger", "recommended merger", "all-share merger",
        "combination of", "all-share combination", "recommended combination",
        "proposed combination", "recommended acquisition",
        "sale of business", "sale of subsidiary", "sale of the group", "sale of the company",
        "sale of division", "proposed sale", "conditional sale", "sale and purchase agreement",
        "reverse takeover", "takeover offer", "takeover",
    ]):
        return "M&A"
    if _any_kw(h, [
        "interim dividend", "final dividend", "special dividend", "quarterly dividend",
        "dividend declaration", "dividend payment", "dividend timetable", "ex-dividend",
    ]):
        return "Dividend"
    # Results — expanded to catch quarterly reports, half-yearly, interim management statements
    if _any_kw(h, [
        "results", "interim", "half-year", "half year", "half-yearly", "half yearly",
        "preliminary", "prelims", "full year", "full-year", "fy results",
        "quarterly report", "q1 report", "q2 report", "q3 report", "q4 report",
        "first quarter", "second quarter", "third quarter", "fourth quarter",
        "annual report", "annual results",
        "interim management statement", "ims",
    ]): return "Results"
    # Trading Update — expanded to catch shorter-form Q-update headlines
    if _any_kw(h, [
        "trading update", "trading statement", "pre-close", "pre close",
        "q1 trading", "q2 trading", "q3 trading", "q4 trading",
        "q1 update", "q2 update", "q3 update", "q4 update",
        "first quarter trading", "second quarter trading", "third quarter trading", "fourth quarter trading",
        "h1 trading", "h2 trading", "year-end trading", "year end trading",
    ]): return "Trading Update"
    # Company Update — broader operational and strategic update phrasings.
    # Includes CMD / investor-event headlines: material for IR work and previously
    # falling through to Other, where hiding Other hid them.
    if _any_kw(h, [
        "company update", "operational update", "business update", "operational performance",
        "strategic update", "strategic review", "strategy update", "strategy",
        "outlook", "guidance", "guidance upgrade", "guidance update",
        "ahead of expectations", "ahead of market", "below expectations", "in line with expectations",
        "profit warning", "earnings update", "performance update",
        "year-end update", "year end update", "month-end update", "month end update",
        "interim update", "trading and operational",
        "response to press", "response to media", "response to speculation",
        "capital markets day", "capital markets event", "cmd",
        "investor presentation", "investor meet", "investor day", "investor event",
        "analyst presentation", "analyst day", "site visit",
    ]): return "Company Update"
    # Buyback — checked before Contract so "Renewal of Buyback Programme" doesn't
    # mis-tag as Contract via 'renewal', and before Fundraise so capital returns
    # aren't lumped with raises
    if _any_kw(h, ["buyback", "buy-back", "buy back", "share purchase", "share repurchase", "repurchase of shares", "transaction in own shares"]): return "Buyback"
    # Contract — broadened. Word boundaries stop 'deal' matching "Dealings by PDMR".
    if _any_kw(h, [
        "contract", "partnership", "agreement", "collaboration",
        "awarded", "mandate", "engagement", "deal", "renew", "renewal",
        "new client", "new customer", "framework agreement", "extension",
        "memorandum of understanding", "mou", "letter of intent", "loi",
    ]): return "Contract"
    # Fundraise — runs before M&A so 'open offer' / 'primary bid' phrasing doesn't mis-tag as M&A
    if _any_kw(h, ["placing", "rights issue", "fundrais*", "share issue", "capital raise", "open offer", "subscription agreement", "retail offer", "primary bid", "bookbuild", "equity raise", "intention to float", "ipo", "offering", "offer for subscription"]): return "Fundraise"
    # M&A
    if _any_kw(h, [
        "acquisition", "merger", "disposal", "takeover", "offer", "bid",
        "scheme of arrangement", "recommended offer", "recommended cash offer",
        "possible offer", "completion of acquisition", "completion of disposal",
    ]): return "M&A"
    # Board Change — "directorate change" listed explicitly; the word-boundary on
    # 'director' below no longer catches it, which is correct (it's a board matter,
    # not a dealing)
    if _any_kw(h, [
        "appointment", "resignation", "board change", "non-executive",
        "chief executive", "cfo", "ceo", "chairman", "chair appointment",
        "boardroom change", "succession", "stepping down", "steps down",
        "joins the board", "leaves the board", "new director",
        "directorate change", "directorate",
    ]): return "Board Change"
    # Shareholding
    if _any_kw(h, ["holding(s) in company", "holdings in company", "tr-1", "notification of major", "major shareholding", "significant shareholding"]): return "Shareholding"
    # Director Dealing
    if _any_kw(h, ["director", "directors", "pdmr", "person discharging", "directors' dealings", "director/pdmr"]): return "Director Dealing"
    # Regulatory
    if _any_kw(h, ["regulatory", "fca", "compliance", "sanction", "investigation", "enforcement"]): return "Regulatory"
    # Dividend
    if _any_kw(h, ["dividend", "distribution"]): return "Dividend"
    # Admin — formal regulatory and listing-related boilerplate that's typically non-material.
    # Runs AFTER all material category checks so anything containing both material and admin keywords (e.g. "Result of Q3" containing both Results and "result of") falls into the material bucket first.
    if _any_kw(h, [
        "block listing", "block-listing",
        "total voting rights", "tvr", "issued share capital",
        "form 8.3", "form 8.5", "form 8", "rule 8.3", "rule 8.5", "rule 8",
        "statement of capital",
        "notification of investments", "notification of holdings",
        "compulsory acquisition",
        "stabilisation notice", "pricing notice", "settlement notice",
        "publication of", "document available", "availability of", "documents available",
        "result of agm", "result of egm", "result of meeting", "result of general meeting", "voting results", "agm voting", "agm statement",
        "person closely associated", "pca dealing",
        "exercise of warrants", "exercise of options", "exercise of share options",
        "share incentive plan", "ltip", "lti award", "rsu award",
        "annual financial report",
        "share consolidation", "stock split",
        "notice of redemption",
        "delisting", "cancellation of listing",
        "scheme document", "publication of scheme",
        "admission to trading", "final terms",
        "prospectus", "circular",
        "agm", "egm", "annual general meeting", "extraordinary general meeting",
        "change of name", "change of registered office", "registered office",
        "change of adviser", "change of nominated adviser", "change of broker",
        # Rule 8 position/dealing disclosures: high volume during offer periods, procedural
        "opening position disclosure", "dealing disclosure", "form 8 (opd)", "form 8 (dd)",
    ]): return "Admin"
    # Non-regulatory sources get their own category if not matched above
    if source_feed in ("UKN", "GNW", "PRN", "MFN", "EQS"):
        return "Non-Reg"
    return "Other"

def parse_investegate_time(time_text):
    match = re.match(r'\d{1,2}\s+\w{3}\s+\d{4}\s+(\d{1,2}:\d{2})\s*(AM|PM)', time_text.strip(), re.IGNORECASE)
    if match:
        hour, minute = match.group(1).split(":")
        hour = int(hour)
        ampm = match.group(2).upper()
        if ampm == "PM" and hour != 12: hour += 12
        elif ampm == "AM" and hour == 12: hour = 0
        return f"{hour:02d}:{minute}"
    match2 = re.search(r'(\d{1,2}:\d{2})', time_text)
    return match2.group(1) if match2 else None

_MONTH_NUM = {"jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,"jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12}
_DATE_RE = re.compile(r'\b(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+(\d{4})\b', re.IGNORECASE)


# England and Wales bank holidays, from the GOV.UK list. 2026 and 2027 are published;
# 2028 follows the standard rules (substitute days included). Extend this set each year:
# /api/health reports bank_holidays_listed_until so you can see when it runs out.
UK_BANK_HOLIDAYS = {
    date(2026, 1, 1), date(2026, 4, 3), date(2026, 4, 6), date(2026, 5, 4),
    date(2026, 5, 25), date(2026, 8, 31), date(2026, 12, 25), date(2026, 12, 28),
    date(2027, 1, 1), date(2027, 3, 26), date(2027, 3, 29), date(2027, 5, 3),
    date(2027, 5, 31), date(2027, 8, 30), date(2027, 12, 27), date(2027, 12, 28),
    date(2028, 1, 3), date(2028, 4, 14), date(2028, 4, 17), date(2028, 5, 1),
    date(2028, 5, 29), date(2028, 8, 28), date(2028, 12, 25), date(2028, 12, 26),
}

def is_non_trading_day(d: date) -> bool:
    return d.weekday() >= 5 or d in UK_BANK_HOLIDAYS

def trading_window() -> list:
    """The calendar days that count as 'the current session': today, plus any weekend
    days and bank holidays immediately before it. On a Monday that is Sat, Sun and Mon;
    on the Tuesday after a bank holiday Monday it is Sat, Sun, Mon and Tue. Takeover
    statements are often released on non-trading days, and would otherwise be dropped
    for not being dated today. On an ordinary Tuesday to Friday it is just today."""
    today = today_london()
    days = [today]
    d = today
    while True:
        d = d - timedelta(days=1)
        if is_non_trading_day(d):
            days.append(d)
        else:
            break
    return days

def parse_announcement_date(text):
    """Pull the calendar date out of an Investegate time/date cell, or None."""
    m = _DATE_RE.search(text or "")
    if not m:
        return None
    try:
        return date(int(m.group(3)), _MONTH_NUM[m.group(2).lower()[:3]], int(m.group(1)))
    except (ValueError, KeyError):
        return None

def in_window(text):
    """The announcement's date if it falls inside trading_window(), otherwise None."""
    d = parse_announcement_date(text)
    return d if d is not None and d in trading_window() else None

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
    "Cookie": "igConsent=1; Inv_DisplayControl=4,2,3,4",
}

# ── AI Summary Extraction ────────────────────────────────────────────────────

def extract_ai_summary(soup) -> Optional[str]:
    """Extract the Investegate AI summary from a BeautifulSoup-parsed announcement page."""
    page_text = soup.get_text("\n", strip=True)

    patterns = [
        r'Close\s*X\s*\n(.+?)(?:\nDisclaimer|\n\[Disclaimer)',
        r'Summary by AI.*?\n(.+?)(?:\nDisclaimer|\n\[Disclaimer)',
    ]
    for pat in patterns:
        match = re.search(pat, page_text, re.DOTALL | re.IGNORECASE)
        if match:
            candidate = match.group(1).strip()
            lines = [l.strip() for l in candidate.split("\n") if l.strip()]
            good_lines = [l for l in lines if len(l) > 30 and not l.startswith("http") and "cookie" not in l.lower()]
            if good_lines:
                return " ".join(good_lines)

    # Fallback: find a substantial paragraph near the top with financial content
    for el in soup.find_all(["p", "div"]):
        text = el.get_text(strip=True)
        if len(text) > 80 and any(w in text.lower() for w in ["announced", "reported", "notified", "results", "revenue", "trading", "voting"]):
            if "cookie" not in text.lower() and "privacy" not in text.lower():
                return text[:600] + ("..." if len(text) > 600 else "")

    return None

# ── Key Figure Extraction ────────────────────────────────────────────────────

def extract_key_figures(text: str) -> list[str]:
    """Pull out lines/sentences containing financial figures — revenue, profit, EPS, margins, etc."""
    figures = []
    patterns = [
        r'(?i)(revenue|turnover|sales)[\s:]*[£$€]?[\d,]+\.?\d*\s*(m|bn|million|billion|k)?',
        r'(?i)(profit|loss|ebitda|ebit|pbt|pat)[\s:]*[£$€]?[\d,]+\.?\d*\s*(m|bn|million|billion|k)?',
        r'(?i)(eps|earnings per share)[\s:]*[\d,]+\.?\d*\s*p',
        r'(?i)(dividend)[\s:]*[\d,]+\.?\d*\s*p',
        r'(?i)(margin)[\s:]*[\d,]+\.?\d*\s*%',
        r'(?i)(nav|net asset value)[\s:]*[£$€]?[\d,]+\.?\d*',
        r'(?i)(fcf|free cash flow|cash flow|net cash|net debt)[\s:]*[£$€]?[\d,]+\.?\d*\s*(m|bn|million|billion)?',
        r'(?i)(guidance|outlook|expect|forecast).*?[\d,]+',
    ]

    # Split into sentences rather than lines for better context
    # First try splitting by newline, then by period-space for long lines
    raw_lines = text.split('\n')
    sentences = []
    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        # If the line is very long, split by sentence boundaries
        if len(line) > 300:
            parts = re.split(r'(?<=[.!])\s+', line)
            sentences.extend(parts)
        else:
            sentences.append(line)

    seen = set()
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence or len(sentence) < 15:
            continue
        for pattern in patterns:
            if re.search(pattern, sentence):
                # Clean up and cap at 400 chars to preserve full sentences
                display = sentence[:400] + ("..." if len(sentence) > 400 else "")
                if display not in seen:
                    seen.add(display)
                    figures.append(display)
                break
        if len(figures) >= 12:
            break

    return figures


# ── TR-1 / Shareholding Parsing ──────────────────────────────────────────────

def parse_shareholding_summary(summary_text: str) -> dict:
    """Parse Investegate's AI summary for TR-1 announcements to extract holder, percentages, direction."""
    result = {
        "holder": None,
        "new_pct": None,
        "old_pct": None,
        "direction": None,
        "change_pp": None,
        "voting_rights": None,
        "date_of_change": None,
    }

    if not summary_text:
        return result

    # Extract holder name — typically the first entity mentioned before "has notified" / "has acquired" / "has disclosed"
    holder_match = re.search(r'^(.+?)\s+has\s+(?:notified|acquired|disclosed|reported|announced|made)', summary_text, re.IGNORECASE)
    if holder_match:
        result["holder"] = holder_match.group(1).strip()
    else:
        # Fallback: first sentence subject before a verb
        holder_match2 = re.search(r'^([A-Z][\w\s&.,\']+?(?:Ltd|Limited|Plc|PLC|Inc|LLC|LLP|Co\.|Group|Management|Capital|Partners|Fund|Investments?)\.?)', summary_text)
        if holder_match2:
            result["holder"] = holder_match2.group(1).strip()

    # Extract new percentage — "now standing at X%" or "total voting rights...X%"
    new_pct_match = re.search(r'(?:now\s+standing\s+at|total.*?(?:voting\s+rights|holding).*?)\s+([\d.]+)%', summary_text, re.IGNORECASE)
    if new_pct_match:
        try:
            result["new_pct"] = float(new_pct_match.group(1))
        except ValueError:
            pass

    # If that didn't work, try finding any percentage patterns
    if result["new_pct"] is None:
        all_pcts = re.findall(r'([\d.]+)%', summary_text)
        if all_pcts:
            try:
                result["new_pct"] = float(all_pcts[0])
            except ValueError:
                pass

    # Extract old/previous percentage — "decrease from X%" or "previous...X%" or "from...X%"
    old_pct_match = re.search(r'(?:previous|prior|from|decrease from|increase from).*?([\d.]+)%', summary_text, re.IGNORECASE)
    if old_pct_match:
        try:
            result["old_pct"] = float(old_pct_match.group(1))
        except ValueError:
            pass

    # If we didn't find old_pct with the above, try the second percentage in the text
    if result["old_pct"] is None and result["new_pct"] is not None:
        all_pcts = re.findall(r'([\d.]+)%', summary_text)
        pct_floats = []
        for p in all_pcts:
            try:
                pct_floats.append(float(p))
            except ValueError:
                continue
        # The second distinct percentage is likely the old one
        for p in pct_floats:
            if p != result["new_pct"]:
                result["old_pct"] = p
                break

    # Determine direction
    text_lower = summary_text.lower()
    if any(w in text_lower for w in ["decrease", "reduced", "fallen", "disposed", "sold"]):
        result["direction"] = "decrease"
    elif any(w in text_lower for w in ["increase", "raised", "acquired", "bought", "purchased"]):
        result["direction"] = "increase"
    elif result["new_pct"] is not None and result["old_pct"] is not None:
        if result["new_pct"] > result["old_pct"]:
            result["direction"] = "increase"
        elif result["new_pct"] < result["old_pct"]:
            result["direction"] = "decrease"
        else:
            result["direction"] = "unchanged"

    # Calculate change
    if result["new_pct"] is not None and result["old_pct"] is not None:
        result["change_pp"] = round(result["new_pct"] - result["old_pct"], 4)

    # Extract voting rights count
    vr_match = re.search(r'([\d,]+)\s+voting\s+rights', summary_text, re.IGNORECASE)
    if vr_match:
        result["voting_rights"] = vr_match.group(1)

    # Extract date of change
    date_match = re.search(r'(?:occurred|changed|took place).*?(?:on\s+)?(\w+\s+\d{1,2},?\s+\d{4}|\d{1,2}\s+\w+\s+\d{4})', summary_text, re.IGNORECASE)
    if date_match:
        result["date_of_change"] = date_match.group(1)

    return result


# URL guard: the detail endpoints take a raw URL from the client and fetch it
# server-side. Without a host check that's an open proxy (SSRF) — anyone with
# access could make this box request arbitrary URLs. Only Investegate
# announcement pages are legitimate targets.
from urllib.parse import urlparse

def _is_investegate_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        return parsed.scheme in ("http", "https") and (
            host == "investegate.co.uk" or host.endswith(".investegate.co.uk")
        )
    except Exception:
        return False

_URL_GUARD_MSG = "URL must be an investegate.co.uk announcement link"

async def fetch_shareholding_detail(url: str) -> ShareholdingDetail:
    """Fetch a TR-1/Holdings announcement and parse the AI summary for structured data."""
    if not _is_investegate_url(url):
        return ShareholdingDetail(
            url=url, company="", fetched_at=datetime.now().isoformat(),
            error=_URL_GUARD_MSG,
        )
    async with httpx.AsyncClient(headers=HEADERS, timeout=30.0, follow_redirects=True) as client:
        try:
            resp = await client.get(url)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            return ShareholdingDetail(
                url=url, company="", fetched_at=datetime.now().isoformat(),
                error=f"Failed to fetch: {e}"
            )

        soup = BeautifulSoup(resp.text, "html.parser")

        # Extract company from the Companies section links
        company = ""
        companies_section = soup.find("h2", string=re.compile(r"Companies", re.IGNORECASE))
        if companies_section:
            next_links = companies_section.find_all_next("a", href=re.compile(r'/company/'))
            for link in next_links:
                text = link.get_text(strip=True)
                if text and "Factsheet" not in text and len(text) > 3:
                    company = text
                    break
        if not company:
            for link in soup.select("a[href*='/company/']"):
                text = link.get_text(strip=True)
                if text and "Factsheet" not in text and len(text) > 3:
                    company = text
                    break

        # Extract the AI summary using the shared helper
        summary_text = extract_ai_summary(soup) or ""

        parsed = parse_shareholding_summary(summary_text)

        return ShareholdingDetail(
            url=url,
            company=company,
            holder=parsed["holder"],
            new_pct=parsed["new_pct"],
            old_pct=parsed["old_pct"],
            direction=parsed["direction"],
            change_pp=parsed["change_pp"],
            voting_rights=parsed["voting_rights"],
            date_of_change=parsed["date_of_change"],
            summary=summary_text if summary_text else None,
            fetched_at=datetime.now().isoformat(),
            error="Could not find AI summary on page" if not summary_text else None,
        )



# ── Detail Scraping ──────────────────────────────────────────────────────────


# Investegate's own page furniture. Since late 2026 its announcement pages load the
# release text separately in the browser, so what a plain fetch receives is only the AI
# summary, its disclaimer, the sidebar and the footer. These lines identify that material.
_PAGE_CHROME_PHRASES = (
    "AI Summary Disclaimer", "The content you are viewing has been generated or assisted by artificial intelligence",
    "We encourage you to independently verify any information provided", "Latest directors dealings",
    "All directors dealings today", "This website is for Private Investors", "by TradingView",
    "Create your FREE account", "Personalise your experience", "Investor Meet Company",
    "Complete Profile", "I am a Private Investor",
    # Site footer. The plain-text cleaner stops at the first footer marker in its list rather
    # than the earliest on the page, so these lines can survive it.
    "All intraday prices are subject", "Investegate takes no responsibility",
    "The announcements are supplied by the denoted source", "Investegate reserves the right",
    "Please note, this site uses cookies", "Investor Meet Company Ltd",
)
_AGO_LINE = re.compile(r'^\d+\s+(second|minute|hour|day|week)s?\s+ago\b', re.I)

def strip_page_chrome(body_text: str) -> tuple[str, bool]:
    """(text without Investegate's page furniture, whether a release is plausibly present).

    A release is judged absent when, once the furniture is removed, too little text is left
    to be one. Measured on a summary-only page modelled on the live site, nothing substantive
    survives; a short results release keeps over 300 characters. 150 sits between the two.
    A genuinely tiny release could be judged absent; that only suppresses key figures, which
    is a safe failure."""
    kept = []
    skip_next_name = False
    for line in (body_text or "").split("\n"):
        t = line.strip()
        if not t:
            continue
        if any(p in t for p in _PAGE_CHROME_PHRASES):
            continue
        if _AGO_LINE.match(t):          # "5 hours ago" lines in the directors' dealings box
            skip_next_name = len(t) < 20  # the company name sometimes sits on its own line
            continue
        if skip_next_name and len(t) < 60:
            skip_next_name = False
            continue
        skip_next_name = False
        kept.append(t)
    cleaned = "\n".join(kept)
    substantive = sum(len(t) for t in kept if len(t) > 40)
    return cleaned, substantive >= 150


async def fetch_announcement_detail(url: str) -> DetailResponse:
    """Fetch a single Investegate announcement page and extract the RNS body."""
    if not _is_investegate_url(url):
        return DetailResponse(
            url=url, company="", headline="", body_text="", body_html="",
            key_figures=[], fetched_at=datetime.now().isoformat(),
            error=_URL_GUARD_MSG,
        )
    async with httpx.AsyncClient(headers=HEADERS, timeout=30.0, follow_redirects=True) as client:
        try:
            resp = await client.get(url)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            return DetailResponse(
                url=url, company="", headline="", body_text="", body_html="",
                key_figures=[], fetched_at=datetime.now().isoformat(),
                error=f"Failed to fetch: {e}"
            )

        soup = BeautifulSoup(resp.text, "html.parser")

        # Extract company and headline from the page
        company = ""
        headline = ""

        # Try to get the headline from the page title or h1/h2
        title_tag = soup.find("h1") or soup.find("title")
        if title_tag:
            headline = title_tag.get_text(strip=True)

        # The main RNS body is usually in a specific div
        # Investegate typically uses class="article-body", "announcement-detail",
        # or the main content area
        body_html = ""
        body_text = ""

        # Try multiple selectors for the announcement body
        content_selectors = [
            {"id": "outterContent"},
            {"id": "outterArticle"},
            {"id": "announcementContent"},
            {"class_": "article-body"},
            {"class_": "announcement-detail"},
            {"class_": "articleBody"},
            {"class_": "rns-body"},
            {"id": "dvArticle"},
        ]

        content_div = None
        for selector in content_selectors:
            content_div = soup.find("div", **selector)
            if content_div:
                break

        # Fallback: find the largest text block on the page
        if not content_div:
            all_divs = soup.find_all("div")
            max_text_len = 0
            for div in all_divs:
                text = div.get_text(strip=True)
                # Look for divs that contain typical RNS content
                if len(text) > max_text_len and len(text) > 200:
                    # Skip navigation and header divs
                    div_classes = div.get("class", [])
                    div_id = div.get("id", "")
                    skip_keywords = ["nav", "header", "footer", "menu", "sidebar", "cookie", "consent"]
                    if not any(kw in " ".join(div_classes + [div_id]).lower() for kw in skip_keywords):
                        max_text_len = len(text)
                        content_div = div

        if content_div:
            # Clean up the HTML — remove scripts, styles, ads, nav elements
            for tag in content_div.find_all(["script", "style", "iframe", "noscript", "nav", "header", "footer"]):
                tag.decompose()

            body_text = content_div.get_text("\n", strip=True)

            company_tag = soup.find("h2") or soup.find("h3")
            if company_tag:
                company = company_tag.get_text(strip=True)
        else:
            body_text = "Could not extract announcement body from the page."

        # Aggressively clean body text — remove Investegate navigation and boilerplate
        # Strip everything before the actual RNS content
        start_markers = [
            "Summary by AI",  # AI summary appears before the RNS body
            "Close X\n",      # End of AI summary section
        ]
        for marker in start_markers:
            idx = body_text.find(marker)
            if idx >= 0:
                # Find the end of the AI summary section
                after = body_text[idx:]
                # Look for where the actual RNS body starts (after "Close X" or "Disclaimer")
                for body_start in ["Disclaimer*\n", "Disclaimer\n", "Close X\n"]:
                    bs_idx = after.find(body_start)
                    if bs_idx >= 0:
                        body_text = after[bs_idx + len(body_start):].strip()
                        break

        # Remove common navigation text that leaks into the body
        nav_lines = [
            "Fundraise", "Welcome", "About", "Contact Us", "Help", "Home",
            "Companies", "Advanced search", "Archive", "UK Short Positions",
            "Meetings", "UK Newswire", "New", "Login", "Register",
            "Search the UK's largest", "Advanced Search", "Sponsored By",
            "FTSE 100", "FTSE 250", "AIM", "Aquis", "Asset Match",
            "JP Jenkins", "techMark", "My List", "Category", "Sector",
            "NAVs & Form8's", "Crypto News", "Show 50100200300",
            "Filter by source", "Read More", "Full Archive",
            "All directors dealings today", "I am a Private Investor",
            "Investor Meet Company", "UK 100", "FTSE quotes",
        ]
        cleaned_lines = []
        for line in body_text.split("\n"):
            line_stripped = line.strip()
            if not line_stripped:
                continue
            # Skip known navigation lines
            if line_stripped in nav_lines:
                continue
            # Skip very short lines that look like menu items
            if len(line_stripped) < 4 and line_stripped not in ("", ):
                continue
            # Skip lines that are just a date with day name
            if re.match(r'^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+\d', line_stripped):
                continue
            # Skip "COMPANIES COVERED:" lines
            if line_stripped.startswith("COMPANIES COVERED:"):
                continue
            # Skip lines that are just tickers (2-4 uppercase letters)
            if re.match(r'^[A-Z]{2,5}\.?$', line_stripped):
                continue
            cleaned_lines.append(line_stripped)
        body_text = "\n".join(cleaned_lines)

        # Trim trailing boilerplate
        boilerplate_markers = [
            "This information is provided by RNS",
            "Please note, this site uses cookies",
            "To continue using Investegate",
            "Privacy and Cookie Policy",
            "Terms and conditions relating to",
            "A Private Investor is a recipient",
            "All intraday prices are subject",
            "Investegate takes no responsibility",
            "© 2026 Stockomendation",
        ]
        for marker in boilerplate_markers:
            idx = body_text.find(marker)
            if idx > 100:  # Only trim if marker is not near the start
                body_text = body_text[:idx].rstrip()
                break

        body_text, body_available = strip_page_chrome(body_text)
        if not body_available:
            body_text = ""
        key_figures = extract_key_figures(body_text) if body_available else []
        ai_summary = extract_ai_summary(soup)

        # body_html is no longer produced: the in-app reader was removed and the release is
        # read on Investegate. The field stays (always empty) so the response shape is stable.
        body_html = ""

        return DetailResponse(
            url=url,
            company=company,
            headline=headline,
            body_text=body_text,
            body_html=body_html,
            key_figures=key_figures,
            ai_summary=ai_summary,
            fetched_at=datetime.now().isoformat(),
            body_available=body_available,
        )


# ── Homepage Scraping ────────────────────────────────────────────────────────

# Serialises scrapes: the background loop, a cold-start warm-up, and a force=true
# request could otherwise run full scrapes concurrently, doubling the request
# volume to Investegate for no benefit.
# Listing pages truncate long headlines ("INCREASED RECOMMENDED CASH ..."), so both
# categorisation and search were working from partial text. Full titles are fetched from
# the announcement page itself, a few per cycle, and remembered for the day so each is
# fetched once. After a restart they are refetched gradually.
TRUNCATION_MARKERS = ("...", "\u2026")
TITLE_FETCHES_PER_CYCLE = int(os.environ.get("TITLE_FETCHES_PER_CYCLE", "15"))
_full_titles: dict[str, str] = {}
_full_titles_date: Optional[str] = None

async def fetch_full_title(client, url) -> Optional[str]:
    try:
        resp = await client.get(url)
        if resp.status_code != 200:
            return None
        h1 = BeautifulSoup(resp.text, "html.parser").find("h1")
        title = h1.get_text(" ", strip=True) if h1 else ""
        return title or None
    except httpx.HTTPError:
        return None

async def resolve_truncated_headlines(anns, client) -> int:
    """Replace truncated headlines with the full title, then recategorise. Returns how
    many were newly fetched this cycle."""
    global _full_titles, _full_titles_date
    today = today_london().isoformat()
    if _full_titles_date != today:
        _full_titles, _full_titles_date = {}, today
    todo = []
    for a in anns:
        if not a.url or not a.headline.rstrip().endswith(TRUNCATION_MARKERS):
            continue
        if a.url in _full_titles:
            a.headline = _full_titles[a.url]
            a.category = categorise(a.headline, a.source_feed)
        elif a.url not in {t.url for t in todo}:
            todo.append(a)
    todo = todo[:TITLE_FETCHES_PER_CYCLE]
    if not todo:
        return 0
    sem = asyncio.Semaphore(4)
    async def one(a):
        async with sem:
            return a.url, await fetch_full_title(client, a.url)
    fetched = dict(await asyncio.gather(*[one(a) for a in todo]))
    for a in anns:
        full = fetched.get(a.url)
        if full:
            _full_titles[a.url] = full
            a.headline = full
            a.category = categorise(full, a.source_feed)
            a.sector_relevant = a.sector_relevant or is_sector_relevant(a.company, full)
    return len({u for u, t in fetched.items() if t})

_scrape_lock = asyncio.Lock()
_SCRAPE_FRESHNESS_SECONDS = 60
# Bounded, pooled connections for scraping. COMPANY_PAGE_CONCURRENCY caps how many
# watchlist company pages are requested from Investegate at the same moment.
HTTP_LIMITS = httpx.Limits(max_connections=10, max_keepalive_connections=10)
COMPANY_PAGE_CONCURRENCY = int(os.environ.get("COMPANY_PAGE_CONCURRENCY", "6"))

async def scrape_investegate(force: bool = False):
  async with _scrape_lock:
    # Freshness check INSIDE the lock: several requests arriving on a cold start
    # (the frontend fires /api/rns and /api/readacross together) queue on the lock,
    # and without this check each would run its own full scrape back to back.
    # Whoever queued behind a scrape that just finished gets the fresh cache instead.
    # force=True (hard refresh) always scrapes.
    if not force and daily_cache.last_scrape is not None:
        age = (datetime.now() - daily_cache.last_scrape).total_seconds()
        if age < _SCRAPE_FRESHNESS_SECONDS:
            cached = daily_cache.get_today()
            return cached, len(cached), list(daily_cache.last_errors)
    errors = []
    raw = []
    seen_keys = set()  # For deduplication
    started = time.monotonic()
    # Per-source diagnostics, exposed at /api/health so a missed announcement can be
    # traced to the page that should have caught it.
    source_stats: dict[str, dict] = {}

    # Investegate caps each page at 50 rows (JS-driven "Show 100/200/300" doesn't work via HTTP).
    # Fix: scrape multiple index pages in parallel and deduplicate.
    # Paginated sources. Each is followed page by page (see fetch_paginated) so the
    # 50-row cap no longer loses announcements. One archive per day in the session,
    # so on a Monday the weekend's releases are swept too.
    paginated_sources = [
        d.strftime("https://www.investegate.co.uk/today-announcements/%Y-%m-%d")
        for d in trading_window()
    ]
    # Investegate's own Mergers, Acquisitions and Disposals category. Low volume and
    # high signal, and it spans several days, so it catches takeover statements for
    # companies that are on no index page we read.
    paginated_sources.append("https://www.investegate.co.uk/category/mergers-acquisitions-and-disposals")

    pages_to_scrape = [
        "https://www.investegate.co.uk/",
        "https://www.investegate.co.uk/ftse100",
        "https://www.investegate.co.uk/ftse250",
        "https://www.investegate.co.uk/aim",
        "https://www.investegate.co.uk/aquis",
        "https://www.investegate.co.uk/techmark",
    ]

    def parse_rows(html_text):
        """Parse announcement rows from an Investegate HTML page.
        Returns (results, table_row_count) — the count of 4-cell table rows seen,
        parsed or not, so the caller can tell an empty feed from broken markup."""
        results = []
        table_rows = 0
        soup = BeautifulSoup(html_text, "html.parser")
        for row in soup.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) < 4:
                continue
            table_rows += 1
            try:
                time_raw = cells[0].get_text(strip=True)
                ann_date = in_window(time_raw)
                if ann_date is None:
                    continue
                time_display = parse_investegate_time(time_raw)
                if not time_display:
                    continue
                source_feed = cells[1].get_text(strip=True)
                company_name = clean_company_name(cells[2].get_text(strip=True))
                if not company_name:
                    continue
                ticker = extract_ticker(company_name)
                headline_cell = cells[3]
                headline = headline_cell.get_text(strip=True)
                if not headline:
                    continue
                link_tag = headline_cell.find("a", href=True)
                url = None
                if link_tag:
                    href = link_tag["href"]
                    url = href if href.startswith("http") else f"https://www.investegate.co.uk{href}"

                # Dedup key: URL-based (ticker / normalised-name fallback) so the same
                # announcement scraped from multiple sources collapses to one row
                dedup_key = make_dedup_key(ticker, company_name, headline, url)
                if dedup_key in seen_keys:
                    continue
                seen_keys.add(dedup_key)

                results.append(Announcement(
                    time=time_display, company=company_name, ticker=ticker,
                    headline=headline, category=categorise(headline, source_feed), url=url,
                    source_feed=source_feed, is_watchlist=ticker in WATCHLIST if ticker else False,
                    sector_relevant=is_sector_relevant(company_name, headline),
                    source="investegate", date_str=ann_date.strftime("%d/%m/%Y"),
                ))
            except (IndexError, AttributeError):
                continue
        return results, table_rows

    # Fetch all pages in parallel
    index_rows_parsed = 0
    index_table_rows = 0
    pages_ok = 0
    async with httpx.AsyncClient(headers=HEADERS, timeout=30.0, follow_redirects=True,
                                 limits=HTTP_LIMITS) as client:
        async def fetch_page(url):
            # One retry after a short pause — a single transient blip on the
            # today-archive page (the richest source) otherwise loses most of a
            # refresh cycle. Deliberately no more than one: retry storms are a
            # bigger IP-block risk than a thin cycle the daily cache papers over.
            last_err = None
            for attempt in (1, 2):
                try:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    return url, resp.text, None
                except httpx.HTTPError as e:
                    last_err = str(e)
                    if attempt == 1:
                        await asyncio.sleep(1.0)
            return url, None, last_err

        # URLs already in today's cache. A paginated sweep stops at the first page whose
        # rows are all known, because pages run newest first and everything deeper is
        # older and already held. In steady state that is usually page 1 only. After a
        # restart the cache is empty, so the sweep runs deep and rebuilds the whole day,
        # which is what stops announcements disappearing after a Render spin-down.
        known_urls = {v.get("url") for v in daily_cache.announcements.values() if v.get("url")}

        def row_urls(html_text):
            """(in-session URLs, total announcement rows) for a page, with no side effects.
            Builds URLs exactly as parse_rows does, so they compare against the cache."""
            urls = set()
            total = 0
            soup = BeautifulSoup(html_text, "html.parser")
            for row in soup.find_all("tr"):
                cells = row.find_all("td")
                if len(cells) < 4:
                    continue
                total += 1
                if in_window(cells[0].get_text(strip=True)) is None:
                    continue
                link = cells[3].find("a", href=True)
                if link:
                    href = link["href"]
                    urls.add(href if href.startswith("http") else f"https://www.investegate.co.uk{href}")
            return urls, total

        def next_page_url(html_text, current):
            """Follow only pagination links Investegate actually emits (?page=N)."""
            soup = BeautifulSoup(html_text, "html.parser")
            for a in soup.find_all("a", href=True):
                m = re.search(r'[?&]page=(\d+)', a["href"])
                if m and int(m.group(1)) == current + 1:
                    href = a["href"]
                    if href.startswith("/"):
                        href = "https://www.investegate.co.uk" + href
                    return href.replace("http://", "https://", 1)
            return None

        async def fetch_paginated(base_url):
            """Fetch a listing page by page. Returns a list of (url, html, err)."""
            out = []
            url, page = base_url, 1
            while url and page <= MAX_LISTING_PAGES:
                got = await fetch_page(url)
                out.append(got)
                _, html_text, err = got
                if err or not html_text:
                    break
                # Must NOT call parse_rows here: it records rows in the shared seen_keys
                # dedup set, so the results loop below would then treat every row on this
                # page as a duplicate and drop it. row_urls is read-only.
                page_urls, page_total = row_urls(html_text)
                # Listings run newest first, so either condition means everything deeper
                # is older than what we already have:
                #  - every in-session row on this page is already cached, or
                #  - the page has rows but none from the current session. Without this,
                #    a multi-day listing such as the M&A category walked its entire
                #    history (all MAX_LISTING_PAGES pages) whenever one new item appeared.
                if page_urls and page_urls <= known_urls:
                    break
                if page_total and not page_urls:
                    break
                url = next_page_url(html_text, page)
                page += 1
                if url:
                    await asyncio.sleep(0.3)   # be polite between pages
            return out

        single = asyncio.gather(*[fetch_page(u) for u in pages_to_scrape])
        paged = asyncio.gather(*[fetch_paginated(u) for u in paginated_sources])
        single_results, paged_results = await asyncio.gather(single, paged)
        # (source, url, html, err). A paginated source contributes one entry per page.
        results = [(r[0], *r) for r in single_results]
        for base, group in zip(paginated_sources, paged_results):
            results += [(base, *r) for r in group]

        for source, url, html, err in results:
            st = source_stats.setdefault(source, {"pages": 0, "rows_in_session": 0, "ok": True, "error": None})
            st["pages"] += 1
            if err:
                errors.append(f"Failed: {url} — {err}")
                st["ok"] = False
                st["error"] = err[:160]
                continue
            pages_ok += 1
            page_rows, table_rows = parse_rows(html)
            st["rows_in_session"] += len(page_rows)
            index_rows_parsed += len(page_rows)
            index_table_rows += table_rows
            raw.extend(page_rows)

    # Loud failure when the scrape silently breaks: a markup or date-format change
    # at Investegate produces zero rows from healthy HTTP 200s, indistinguishable
    # in the UI from a quiet news day. Two signatures:
    #  - pages fetched fine but NO 4-cell table rows anywhere → markup changed
    #  - table rows present but none matched today's date after the RNS day has
    #    started (07:30 London) → date format likely changed
    # Before 07:30 the second case is normal (tables still show yesterday), so
    # the time condition avoids a false alarm every early morning.
    if pages_ok > 0 and index_rows_parsed == 0:
        if index_table_rows == 0:
            errors.append(
                f"Parser warning: {pages_ok} index pages fetched but no announcement "
                f"table rows found — Investegate markup may have changed"
            )
        else:
            now_london = datetime.now(_LONDON_TZ) if _LONDON_TZ else datetime.now()
            if (now_london.hour, now_london.minute) >= (7, 30):
                errors.append(
                    f"Parser warning: {index_table_rows} table rows seen but none matched "
                    f"today's date — Investegate date format may have changed"
                )

    # Supplement with direct company-page fetches for watchlist companies.
    # This catches announcements from smaller companies that don't appear on
    # any of the seven index pages (FTSE 100 / 250 / AIM / Aquis / techMark / homepage / today).
    # Pure HTML parsing — no XML/RSS/lxml dependency, no silent failures.
    #
    # Investegate company pages use a 4-column table:
    # Cell 0: Date (e.g. "17 Apr 2026")
    # Cell 1: Time (e.g. "07:00 AM")
    # Cell 2: Source (e.g. "RNS")
    # Cell 3: Announcement (with link)
    # Company pages are fetched at most COMPANY_PAGE_CONCURRENCY at a time. Firing all 46
    # at Investegate in the same instant every two minutes invites rate limiting.
    company_sem = asyncio.Semaphore(COMPANY_PAGE_CONCURRENCY)
    company_failed: list[str] = []
    async with httpx.AsyncClient(headers=HEADERS, timeout=30.0, follow_redirects=True,
                                 limits=HTTP_LIMITS) as client:
        async def get_company_page(page_url):
            """One retry, as for index pages: a single blip otherwise drops a watchlist
            company from the whole cycle."""
            for attempt in (1, 2):
                try:
                    async with company_sem:
                        resp = await client.get(page_url)
                    if resp.status_code >= 500 and attempt == 1:
                        await asyncio.sleep(1.0)
                        continue
                    return resp
                except httpx.HTTPError:
                    if attempt == 2:
                        raise
                    await asyncio.sleep(1.0)

        async def fetch_company_page(ticker, info, code=None):
            # `code` is the Investegate company code whose page is fetched. It differs
            # from `ticker` only for aliased companies (see TICKER_ALIASES).
            code = code or ticker
            found = []
            try:
                # Investegate company pages: e.g. https://www.investegate.co.uk/company/NRR
                # Tickers with trailing dot (TW., JD., JET2 etc.) are kept as-is — the dot
                # is part of the LSE symbol and Investegate URL slugs preserve it.
                page_url = f"https://www.investegate.co.uk/company/{code}"
                resp = await get_company_page(page_url)
                if resp.status_code != 200:
                    errors.append(f"Company page {ticker}: HTTP {resp.status_code}")
                    company_failed.append(code)
                    return found
                soup = BeautifulSoup(resp.text, "html.parser")

                for row in soup.find_all("tr"):
                    cells = row.find_all("td")
                    if len(cells) < 4:
                        continue
                    try:
                        date_raw = cells[0].get_text(strip=True)
                        time_raw = cells[1].get_text(strip=True)
                        source_feed = cells[2].get_text(strip=True) or "RNS"
                        headline_cell = cells[3]

                        # Filter to today's announcements only
                        row_date = in_window(date_raw)
                        if row_date is None:
                            continue

                        # Parse time — combine date and time so existing helper recognises the format
                        combined = f"{date_raw} {time_raw}"
                        time_display = parse_investegate_time(combined)
                        if not time_display:
                            continue

                        headline = headline_cell.get_text(strip=True)
                        if not headline:
                            continue

                        link_tag = headline_cell.find("a", href=True)
                        url = None
                        if link_tag:
                            href = link_tag["href"]
                            url = href if href.startswith("http") else f"https://www.investegate.co.uk{href}"

                        # Defensive: only keep rows whose URL slug contains this ticker.
                        # Stops cross-referenced "related announcements" sneaking in.
                        if url:
                            url_lower = url.lower()
                            ticker_slug = code.lower()
                            if f"--{ticker_slug}/" not in url_lower:
                                continue

                        dedup_key = make_dedup_key(ticker, info['name'], headline, url)
                        if dedup_key in seen_keys:
                            continue
                        seen_keys.add(dedup_key)

                        # Detect non-RNS source feeds from URL pattern (overrides cell text if URL is more specific)
                        sf = source_feed
                        if url:
                            url_lower = url.lower()
                            if "/gnw/" in url_lower: sf = "GNW"
                            elif "/ukn/" in url_lower: sf = "UKN"
                            elif "/prn/" in url_lower: sf = "PRN"
                            elif "/mfn/" in url_lower: sf = "MFN"
                            elif "/eqs/" in url_lower: sf = "EQS"

                        found.append(Announcement(
                            time=time_display, company=info["name"], ticker=ticker,
                            headline=headline, category=categorise(headline, sf), url=url,
                            source_feed=sf, is_watchlist=True, sector_relevant=True,
                            source="investegate-company", date_str=row_date.strftime("%d/%m/%Y"),
                        ))
                    except (IndexError, AttributeError):
                        continue
            except Exception as e:
                errors.append(f"Company page {ticker}: {type(e).__name__}: {str(e)[:100]}")
                company_failed.append(code)
            return found

        company_results = await asyncio.gather(
            *[fetch_company_page(t, i, code) for t, i in WATCHLIST.items() for code in company_codes(t)]
        )
        for company_anns in company_results:
            raw.extend(company_anns)

        titles_resolved = await resolve_truncated_headlines(raw, client)

    source_stats["company_pages"] = {
        "requested": sum(len(company_codes(t)) for t in WATCHLIST),
        "failed": sorted(set(company_failed)),
        "rows_in_session": sum(len(x) for x in company_results),
    }

    total_raw = len(raw)

    # Merge with daily cache — preserves earlier announcements that fell off index pages
    all_today = daily_cache.merge(raw)
    total_cached = len(all_today)

    # Persist errors on the cache so /api/rns can surface them between background scrapes
    daily_cache.last_errors = errors
    daily_cache.last_stats = {
        "finished_at": datetime.now().isoformat(),
        "duration_seconds": round(time.monotonic() - started, 1),
        "session_days": [d.isoformat() for d in trading_window()],
        "rows_scraped": total_raw,
        "cached_total": total_cached,
        "truncated_headlines_resolved": titles_resolved,
        "sources": source_stats,
    }

    return all_today, total_cached, errors

# Categories excluded from the default feed (watchlist company announcements in these
# categories are always kept). Keeps the morning feed focused on material news.
# Note: "Other" is intentionally NOT in this set — better to surface uncategorised
# announcements and let the user filter them out via the category pills than to
# silently drop genuine company updates that just didn't match any keyword.
NOISE_CATEGORIES = {
    "Fundraise",         # placings, rights issues, capital raises (mostly noise unless covered)
    "Buyback",           # transactions in own shares (procedural, not material to non-holders)
    "Director Dealing",  # PDMR transactions (hundreds per day, rarely material)
    "Shareholding",      # TR-1 holdings notifications (lots of fund rebalancing noise)
    "Admin",             # block listings, voting rights, scheme docs, AGM results, etc.
    "Dividend",          # dividend declarations (usually announced with results anyway)
}

def _date_key(date_str):
    """dd/mm/yyyy -> yyyymmdd so dates sort chronologically."""
    try:
        d, m, y = date_str.split("/")
        return f"{y}{m}{d}"
    except (AttributeError, ValueError):
        return ""

def rank_announcements(anns):
    def sk(a):
        p = 3
        if a.is_watchlist: p = 0
        elif a.sector_relevant: p = 1
        elif a.category in ("M&A", "Results", "Trading Update"): p = 2
        return (p, _date_key(a.date_str), a.time)
    return sorted(anns, key=sk)

def apply_live_watchlist(anns):
    """Recompute is_watchlist against the CURRENT watchlist at serve time.
    The flag is set when an announcement is first scraped, so a company added to the
    watchlist later would otherwise stay is_watchlist=False in the cache. Recomputing
    here means manually-added companies are highlighted, filtered, and ranked correctly
    on the next refresh without waiting for the announcement to be re-scraped."""
    for a in anns:
        # Canonicalise first so entries cached under an Investegate alias code
        # (e.g. 0QT8 for I-RES) are recognised as the watchlist company they are.
        a.ticker = canonical_ticker(a.ticker)
        a.is_watchlist = bool(a.ticker) and a.ticker in WATCHLIST
    return anns

# ── Endpoints ────────────────────────────────────────────────────────────────

@app.get("/api/health")
async def health():
    bg_state = "not_started"
    if _background_task is not None:
        if _background_task.done():
            bg_state = "stopped"
        else:
            bg_state = "running"
    return {
        "status": "ok",
        "timestamp": datetime.now().isoformat(),
        "cache_size": daily_cache.count,
        "last_scrape": daily_cache.last_scrape.isoformat() if daily_cache.last_scrape else None,
        "background_scraper": bg_state,
        "scrape_interval_seconds": SCRAPE_INTERVAL_SECONDS,
        "last_errors_count": len(daily_cache.last_errors),
        "watchlist_size": len(WATCHLIST),
        "app_version": frontend_page()[1],
        "watchlist_storage": watchlist_store.status(),
        "session_days": [d.isoformat() for d in trading_window()],
        "bank_holidays_listed_until": max(UK_BANK_HOLIDAYS).isoformat(),
        "bank_holiday_list_needs_update": today_london().year > max(UK_BANK_HOLIDAYS).year,
        "last_scrape_stats": daily_cache.last_stats,
    }

@app.get("/api/watchlist")
async def get_watchlist():
    return {"watchlist": WATCHLIST}

async def _ensure_cache_warm(force: bool = False):
    """Ensure the daily cache has been populated at least once. The background scraper
    keeps it warm in steady state. Only triggers a synchronous scrape if the server has
    never scraped at all (cold start) or if force=True is passed. A cache that has been
    scraped and legitimately found 0 announcements (quiet day, source down) is left alone."""
    if force or daily_cache.last_scrape is None:
        await scrape_investegate(force=force)

@app.get("/api/rns", response_model=ScrapeResponse)
async def get_rns(
    request: Request,
    watchlist_only: bool = Query(False),
    include_all: bool = Query(False),
    limit: int = Query(2000),
    include_fundraise: bool = Query(False, description="Legacy alias; noise filtering is client-side"),
    force: bool = Query(False, description="Bypass cache freshness and trigger a fresh scrape"),
    hard: bool = Query(False, description="Deprecated alias for force. It used to wipe the day's cache; it no longer does."),
):
    """Read announcements from the daily cache (kept warm by the background scraper).

    Category filtering is client-side. Mining and metals companies are returned with
    excluded_sector=True so the UI can hide them behind a switch; nothing is dropped
    here, and M&A announcements are never flagged.

    Conditional requests: the response carries an ETag derived from its content (not
    from the scrape timestamp). A client that sends it back in If-None-Match gets a
    bodiless 304 when nothing has changed, which is most polls.

    `include_all=false` additionally drops headlines matching EXCLUDED_TYPES
    (factsheets, NAVs, reissue notices)."""
    # `hard` used to call daily_cache.clear(), destroying every announcement that had
    # scrolled off Investegate's listing pages. It now simply forces a rescrape.
    await _ensure_cache_warm(force=force or hard)
    raw = apply_live_watchlist(daily_cache.get_today())
    total_raw = len(raw)
    for a in raw:
        a.excluded_sector = (not a.is_watchlist and a.category != "M&A"
                             and is_excluded_company(a.company))
    if include_all:
        filtered = raw
    else:
        filtered = [a for a in raw if not a.excluded_sector and is_material_type(a.headline)]
    if watchlist_only:
        filtered = [a for a in filtered if a.is_watchlist]
    ranked = rank_announcements(filtered)
    body = ScrapeResponse(
        announcements=ranked[:limit], total_raw=total_raw, total_filtered=len(filtered),
        scraped_at=(daily_cache.last_scrape.isoformat() if daily_cache.last_scrape
                    else datetime.now().isoformat()),
        date=today_london().strftime("%A %d %B %Y"),
        source="investegate", errors=list(daily_cache.last_errors),
        default_hidden_categories=sorted(NOISE_CATEGORIES),
    ).model_dump()
    # The scrape timestamp changes every cycle even when nothing new arrived, so it is
    # left out of the ETag; otherwise every poll would miss.
    fingerprint = json.dumps({k: v for k, v in body.items() if k != "scraped_at"},
                             sort_keys=True, default=str)
    etag = '"' + hashlib.sha1(fingerprint.encode()).hexdigest() + '"'
    headers = {"ETag": etag, "Cache-Control": "no-cache", "X-App-Version": frontend_page()[1]}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return JSONResponse(content=body, headers=headers)

@app.get("/api/rns/all", response_model=ScrapeResponse)
async def get_rns_unfiltered(limit: int = Query(2000), force: bool = Query(False)):
    """Like /api/rns but with no headline-level or category-level filtering at all.
    Reads from cache."""
    await _ensure_cache_warm(force=force)
    raw = apply_live_watchlist(daily_cache.get_today())
    total_raw = len(raw)
    scraped_at = daily_cache.last_scrape.isoformat() if daily_cache.last_scrape else datetime.now().isoformat()
    return ScrapeResponse(
        announcements=raw[:limit], total_raw=total_raw, total_filtered=total_raw,
        scraped_at=scraped_at, date=today_london().strftime("%A %d %B %Y"),
        source="investegate", errors=list(daily_cache.last_errors),
        default_hidden_categories=sorted(NOISE_CATEGORIES),
    )

# ── Detail response cache (TTL) ──────────────────────────────────────────────
# Announcement bodies don't change once published, so caching repeat opens avoids
# re-fetching and re-parsing the same Investegate page. Only successful (error-free)
# responses are cached. Cache is in-memory and resets on restart, which is fine.
_detail_cache: dict[str, tuple[float, object]] = {}

def _cache_get(key: str):
    hit = _detail_cache.get(key)
    if hit and (time.monotonic() - hit[0]) < DETAIL_CACHE_TTL:
        return hit[1]
    if hit:
        _detail_cache.pop(key, None)  # expired
    return None

def _cache_put(key: str, value):
    # Evict oldest entries if over the cap.
    if len(_detail_cache) >= DETAIL_CACHE_MAX:
        for old_key in sorted(_detail_cache, key=lambda k: _detail_cache[k][0])[:max(1, DETAIL_CACHE_MAX // 5)]:
            _detail_cache.pop(old_key, None)
    _detail_cache[key] = (time.monotonic(), value)

@app.get("/api/rns/detail", response_model=DetailResponse)
async def get_rns_detail(url: str = Query(..., description="Full Investegate announcement URL")):
    """Fetch and parse a single announcement's full body text. Cached per URL for DETAIL_CACHE_TTL."""
    cache_key = f"detail:{url}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    result = await fetch_announcement_detail(url)
    if result.error is None:  # never cache failures, so a retry can succeed
        _cache_put(cache_key, result)
    return result

@app.get("/api/rns/shareholding", response_model=ShareholdingDetail)
async def get_shareholding_detail(url: str = Query(..., description="Full Investegate TR-1/Holdings announcement URL")):
    """Fetch a TR-1/Holdings announcement and return structured shareholding data. Cached per URL."""
    cache_key = f"sh:{url}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    result = await fetch_shareholding_detail(url)
    if result.error is None:
        _cache_put(cache_key, result)
    return result

@app.get("/api/readacross")
async def get_readacross():
    """Return the read-across matrix plus live-triggered read-across companies based on today's cache.
    A read-across company is 'triggered' if it has any announcement in today's cache."""
    # Cold-start safety: if cache is empty, kick off a synchronous scrape so first call returns data
    await _ensure_cache_warm()
    # Pull today's announcements from the daily cache
    today_anns = apply_live_watchlist(daily_cache.get_today())

    # Group triggered read-across announcements by ticker
    triggered: dict[str, list[dict]] = {}
    for ann in today_anns:
        if ann.ticker and ann.ticker in READ_ACROSS_MATRIX:
            triggered.setdefault(ann.ticker, []).append(ann.model_dump())

    # Split watchlist by tier
    core_list, read_across_list = [], []
    for ticker, info in WATCHLIST.items():
        tier = info.get("tier", "core")
        entry = {"ticker": ticker, **info}
        if tier == "read_across":
            read_across_list.append(entry)
        else:
            core_list.append(entry)

    # Determine which core companies are affected today
    affected_core: dict[str, list[str]] = {}
    for ra_ticker in triggered.keys():
        for core_ticker in READ_ACROSS_MATRIX.get(ra_ticker, {}).keys():
            affected_core.setdefault(core_ticker, []).append(ra_ticker)

    return {
        "matrix": READ_ACROSS_MATRIX,
        "rel_types": REL_TYPES,
        "core": core_list,
        "read_across": read_across_list,
        "triggered": triggered,
        "affected_core": affected_core,
        "cache_date": daily_cache.cache_date,
        "generated_at": datetime.now().isoformat(),
    }

@app.get("/api/rns/{ticker}")
async def get_rns_by_ticker(ticker: str):
    ticker = ticker.upper()
    await _ensure_cache_warm()
    raw = apply_live_watchlist(daily_cache.get_today())
    return {"ticker": ticker, "company": WATCHLIST.get(ticker, {}).get("name", "Unknown"),
            "announcements": [a for a in raw if a.ticker == ticker], "errors": list(daily_cache.last_errors)}

_TICKER_RE = re.compile(r'^[A-Z0-9]{1,6}\.?$')

async def check_investegate_company(ticker: str) -> tuple[Optional[str], Optional[str]]:
    """(error, warning) for a proposed watchlist ticker.

    An error blocks the addition: Investegate has no page for it, so it would be fetched
    every two minutes forever and show a permanent scrape error. A warning allows it but
    flags that the company's announcements appear to be filed under a different code,
    the I-RES / 0QT8 situation, which needs an entry in TICKER_ALIASES.
    If Investegate itself is unreachable, the addition is allowed with a warning rather
    than blocked by an outage."""
    url = f"https://www.investegate.co.uk/company/{ticker}"
    try:
        async with httpx.AsyncClient(headers=HEADERS, timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(url)
    except httpx.HTTPError as e:
        return None, f"Could not reach Investegate to check {ticker} ({type(e).__name__}). Added anyway."
    if resp.status_code == 404:
        return (f"Investegate has no company page for {ticker}. Check the ticker: it should be "
                f"the code shown in brackets after the company name on Investegate."), None
    if resp.status_code != 200:
        return None, f"Investegate returned HTTP {resp.status_code} for {ticker}. Added anyway."
    soup = BeautifulSoup(resp.text, "html.parser")
    codes = set()
    for a in soup.find_all("a", href=True):
        m = re.search(r'/announcement/[^/]+/[^/]*--([a-z0-9.]+)/', a["href"].lower())
        if m:
            codes.add(m.group(1).upper())
    if codes and ticker not in codes:
        others = ", ".join(sorted(codes)[:3])
        return None, (f"{ticker}'s announcements on Investegate appear under {others}. "
                      f"Announcements may be missed until {others} is added to TICKER_ALIASES in app.py.")
    return None, None

@app.post("/api/watchlist/add")
async def add_to_watchlist(ticker: str, name: str, sector: str = "", tier: str = "core"):
    ticker = (ticker or "").strip().upper()
    name = (name or "").strip()
    if not _TICKER_RE.match(ticker):
        return JSONResponse(status_code=400, content={
            "status": "error", "error": f"'{ticker}' is not a valid ticker. Use 1 to 6 letters or digits, e.g. GTLY or TW."})
    if not name:
        return JSONResponse(status_code=400, content={"status": "error", "error": "Enter the company name."})
    if tier not in ("core", "read_across"):
        tier = "core"
    error, warning = await check_investegate_company(ticker)
    if error:
        return JSONResponse(status_code=400, content={"status": "error", "error": error})
    info = {"name": name, "sector": (sector or "").strip(), "tier": tier}
    WATCHLIST[ticker] = info
    save_error = await asyncio.to_thread(watchlist_store.upsert, ticker, info)
    status = watchlist_store.status()
    if save_error:
        warning = (warning + " " if warning else "") + "The change could not be saved and will be lost on restart."
    elif not status["durable"]:
        warning = (warning + " " if warning else "") + "Saved for now, but it will be lost when the server restarts."
    return {"status": "added", "ticker": ticker, "warning": warning, "storage": status, "watchlist": WATCHLIST}

@app.delete("/api/watchlist/{ticker}")
async def remove_from_watchlist(ticker: str):
    ticker = ticker.upper()
    removed = WATCHLIST.pop(ticker, None)
    if not removed:
        return {"status": "not_found", "ticker": ticker}
    save_error = await asyncio.to_thread(watchlist_store.remove, ticker)
    return {"status": "removed", "ticker": ticker, "warning": save_error, "storage": watchlist_store.status()}

@app.post("/api/watchlist/reset")
async def reset_watchlist():
    """Reset watchlist back to defaults."""
    WATCHLIST.clear()
    WATCHLIST.update({t: dict(i) for t, i in DEFAULT_WATCHLIST.items()})
    save_error = await asyncio.to_thread(watchlist_store.reset)
    return {"status": "reset", "warning": save_error, "watchlist": WATCHLIST}

@app.get("/api/debug/raw-html")
async def debug_raw_html():
    today_url = today_london().strftime("https://www.investegate.co.uk/today-announcements/%Y-%m-%d")
    async with httpx.AsyncClient(headers=HEADERS, timeout=30.0, follow_redirects=True) as client:
        try:
            resp = await client.get(today_url)
            soup = BeautifulSoup(resp.text, "html.parser")
            tables = soup.find_all("table")
            all_rows = soup.find_all("tr")
            rows_with_4_cells = [r for r in all_rows if len(r.find_all("td")) >= 4]
            return {
                "url_fetched": today_url,
                "tables_found": len(tables),
                "total_tr_elements": len(all_rows),
                "rows_with_4_plus_cells": len(rows_with_4_cells),
                "sample_rows": [[c.get_text(strip=True)[:100] for c in r.find_all("td")] for r in rows_with_4_cells[:5]],
            }
        except Exception as e: return {"error": str(e)}

if __name__ == "__main__":
    import uvicorn
    print()
    print("  h2Radnor RNS Monitor v2.0")
    print(f"  Running on http://{HOST}:{PORT}")
    if APP_PASSWORD:
        print("  Password protection: ON")
    else:
        print("  Password protection: OFF (set APP_PASSWORD env var to enable)")
    print()
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
