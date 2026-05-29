"""
h2Radnor RNS Monitor — Web Deployment v2.0
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
from datetime import datetime, date
from typing import Optional
from pathlib import Path

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, Query, Request, Response, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ── Configuration via environment variables ──────────────────────────────────
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")  # Empty = no password required
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")  # For outreach draft generation
# Model used for outreach drafting. Override from the Render dashboard without a code change.
# claude-sonnet-4-6 = best speed/quality balance; claude-opus-4-8 = highest quality; claude-haiku-4-5 = cheapest/fastest.
OUTREACH_MODEL = os.environ.get("OUTREACH_MODEL", "claude-sonnet-4-6")
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("rns-monitor")

# Background scraper task handle, populated in the lifespan handler
_background_task: Optional[asyncio.Task] = None

async def _periodic_scrape_loop():
    """Background loop that scrapes Investegate continuously and keeps the cache warm.
    Runs immediately on startup, then every SCRAPE_INTERVAL_SECONDS thereafter."""
    log.info(f"Background scraper starting (interval: {SCRAPE_INTERVAL_SECONDS}s)")
    while True:
        try:
            await scrape_investegate()
            log.info(f"Background scrape complete. Cache has {daily_cache.count} announcements.")
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

app = FastAPI(title="h2Radnor RNS Monitor", version="2.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

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

def check_auth(request: Request):
    """Check if user is authenticated. Skip if no password is set."""
    if not APP_PASSWORD:
        return True
    if not _cookie_ok(request.cookies.get(AUTH_COOKIE)):
        raise HTTPException(status_code=401, detail="Not authenticated")
    return True

@app.get("/login", response_class=HTMLResponse)
async def login_page():
    if not APP_PASSWORD:
        return RedirectResponse("/")
    return HTMLResponse(content=LOGIN_HTML)

@app.post("/login")
async def login(request: Request):
    form = await request.form()
    password = form.get("password", "")
    # Compare the submitted password against the real password in constant time.
    if APP_PASSWORD and hmac.compare_digest(password, APP_PASSWORD):
        response = RedirectResponse("/", status_code=302)
        # Store a derived token, never the password itself.
        response.set_cookie(AUTH_COOKIE, _auth_token(), httponly=True, samesite="lax", max_age=86400)
        return response
    return RedirectResponse("/login?error=1", status_code=302)

# ── Serve Frontend ───────────────────────────────────────────────────────────

STATIC_DIR = Path(__file__).parent / "static"

@app.get("/", response_class=HTMLResponse)
async def serve_frontend(request: Request):
    if APP_PASSWORD and not _cookie_ok(request.cookies.get(AUTH_COOKIE)):
        return RedirectResponse("/login")
    html_path = STATIC_DIR / "index.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))

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

WATCHLIST_FILE = Path(__file__).parent / "watchlist.json"

# Tickers tracked as read-across signals to the core watchlist.
# All other watchlist tickers are treated as core by default.
READ_ACROSS_TICKERS = {
    "CRST", "BTRW", "TW.", "GRI", "KNOS", "BBOX", "SAVS", "LSL",
    "ALFA", "WTB", "JET2", "SUS", "SGRO", "SAFE",
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
}

def _ensure_tiers(wl: dict) -> dict:
    """Ensure every watchlist entry has a 'tier' field (backward compat for old JSON files)."""
    for ticker, info in wl.items():
        if "tier" not in info:
            info["tier"] = "read_across" if ticker in READ_ACROSS_TICKERS else "core"
    return wl

def load_watchlist() -> dict:
    """Load watchlist from JSON file, falling back to defaults if file doesn't exist."""
    if WATCHLIST_FILE.exists():
        try:
            with open(WATCHLIST_FILE, "r") as f:
                wl = json.load(f)
                return _ensure_tiers(wl)
        except (json.JSONDecodeError, IOError):
            pass
    # First run or corrupted file — save defaults and return them
    save_watchlist(DEFAULT_WATCHLIST)
    return dict(DEFAULT_WATCHLIST)

def save_watchlist(wl: dict):
    """Save watchlist to JSON file."""
    try:
        with open(WATCHLIST_FILE, "w") as f:
            json.dump(wl, f, indent=2)
    except IOError as e:
        print(f"Warning: could not save watchlist: {e}")

WATCHLIST = load_watchlist()

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
        self._load()

    def _load(self):
        """Load cache from disk if it exists and is from today."""
        if self.cache_file.exists():
            try:
                with open(self.cache_file, "r") as f:
                    data = json.load(f)
                if data.get("date") == date.today().isoformat():
                    self.announcements = data.get("announcements", {})
                    self.cache_date = data["date"]
                    return
            except (json.JSONDecodeError, IOError):
                pass
        self.announcements = {}
        self.cache_date = date.today().isoformat()

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
        """Reset cache at midnight."""
        today = date.today().isoformat()
        if self.cache_date != today:
            self.announcements = {}
            self.cache_date = today
            self._save()

    def merge(self, new_announcements: list) -> list:
        """Merge new scrape results into the cache, return the full combined list.
        First reindexes existing cache entries under the normalised key so any
        legacy duplicates from earlier scrapes collapse on the next run."""
        self._check_date()
        # Reindex existing entries so old per-company-name keys collapse into ticker-based keys
        reindexed = {}
        for v in self.announcements.values():
            ann = Announcement(**v) if not isinstance(v, Announcement) else v
            new_key = make_dedup_key(ann.ticker, ann.company, ann.headline)
            # If two old entries collapse to the same new key, keep the newer one
            # (we have no timestamp on the dict value, so last write wins)
            reindexed[new_key] = v
        self.announcements = reindexed
        # Now merge in the fresh results — they take precedence over any older copy
        for ann in new_announcements:
            key = make_dedup_key(ann.ticker, ann.company, ann.headline)
            self.announcements[key] = ann.model_dump()
        self.last_scrape = datetime.now()
        self._save()
        return [Announcement(**v) for v in self.announcements.values()]

    @property
    def count(self):
        self._check_date()
        return len(self.announcements)

CACHE_FILE = Path(__file__).parent / "cache_today.json"
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
    "advertising", "pr ",
    "industrial", "engineering", "acquisition", "manufacturing",
    "fastener", "building products", "construction",
    "emerging market", "investment trust",
    "fmcg", "consumer", "food", "beverage", "consumer goods",
]

EXCLUDED_KEYWORDS = [
    "mining", "resources", "metals", "petroleum", "oil", "gas",
    "gold", "silver", "copper", "lithium", "coal", "diamond", "exploration",
]
EXCLUDED_COMPANIES = {
    "anglo american", "glencore", "antofagasta", "fresnillo",
    "rio tinto", "bhp", "shell", "bp", "endeavour mining",
    "centamin", "hochschild", "ferrexpo", "kenmare", "central asia metals",
    "greatland gold", "pan african", "thungela", "tullow oil",
    "harbour energy", "energean", "parkmead",
}

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

MATERIAL_TYPES = [
    "results", "interim", "half-year", "preliminary", "trading update",
    "trading statement", "acquisition", "merger", "disposal",
    "placing", "rights issue", "fundrais", "share issue",
    "director", "pdmr",
    "company update", "strategic", "outlook", "guidance",
    "revenue", "profit", "earnings",
    "contract", "partnership", "agreement", "collaboration",
    "offer", "bid", "takeover",
    "buyback", "share purchase", "repurchase",
    "appointment", "resignation", "board change",
    "dividend", "special dividend",
    "ipo", "admission", "regulatory", "fca", "compliance",
    "restructur", "cost saving", "redundan",
    "capital markets", "investor", "notice of results",
    "holding(s) in company", "tr-1", "notification of major",
    "major shareholding", "significant shareholding",
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

class OutreachRequest(BaseModel):
    company: str
    ticker: Optional[str] = None
    headline: str
    category: Optional[str] = None
    ai_summary: Optional[str] = None
    key_figures: Optional[list[str]] = None
    investor_type: str = "generalist"  # generalist, compounder, contrarian, smid

class OutreachVariant(BaseModel):
    label: str
    subject: str
    body: str

class OutreachResponse(BaseModel):
    variants: list[OutreachVariant]
    generated_at: str
    error: Optional[str] = None

# ── Outreach Prompt Template ─────────────────────────────────────────────────

OUTREACH_SYSTEM_PROMPT = """You are an investor relations professional at h2Radnor Limited, a research and IR advisory firm based in London. You draft investor outreach emails following a management meeting, results release, or material company announcement.

Your writing style:
- Professional, concise, and direct
- Mandate-aware: tailor content to the investor's strategy
- Lead with the most material data points — never bury the numbers
- No promotional language or sell-side bias
- Assume the recipient is a sophisticated institutional investor
- Keep emails to 150-250 words maximum
- Use specific figures from the announcement, not vague language
- Close with a clear call to action (meeting, call, further materials)
- Sign off as the h2Radnor Capital Markets team

Format: plain text email, no HTML or markdown. Include a subject line on the first line prefixed with "Subject: "

Do NOT include any preamble, explanation, or commentary. Output ONLY the email."""

def build_outreach_prompt(req: OutreachRequest) -> str:
    """Build the user prompt for outreach generation."""
    parts = [
        f"Company: {req.company} ({req.ticker or 'N/A'})",
        f"Announcement type: {req.category or 'General'}",
        f"Headline: {req.headline}",
    ]
    if req.ai_summary:
        parts.append(f"Summary: {req.ai_summary}")
    if req.key_figures:
        parts.append(f"Key figures: {'; '.join(req.key_figures[:8])}")

    investor_descriptions = {
        "generalist": "a UK equity generalist fund manager",
        "compounder": "a quality/compounder-focused fund manager who values margin expansion, recurring revenue, and management execution",
        "contrarian": "a contrarian/value investor looking for mispriced situations and catalysts for re-rating",
        "smid": "a UK small/mid-cap specialist focused on AIM and FTSE SmallCap companies with growth potential",
    }
    inv_desc = investor_descriptions.get(req.investor_type, investor_descriptions["generalist"])
    parts.append(f"Target investor profile: {inv_desc}")
    parts.append("Draft an outreach email to this investor about this announcement. Highlight why it matters for their mandate specifically.")

    return "\n".join(parts)

async def generate_outreach(req: OutreachRequest) -> OutreachResponse:
    """Call Claude API to generate outreach email variants."""
    if not ANTHROPIC_API_KEY:
        return OutreachResponse(
            variants=[], generated_at=datetime.now().isoformat(),
            error="ANTHROPIC_API_KEY not configured. Set it as an environment variable."
        )

    investor_types = ["generalist", "compounder", "contrarian"]
    variants = []

    async with httpx.AsyncClient(timeout=60.0) as client:
        for inv_type in investor_types:
            req_copy = req.model_copy()
            req_copy.investor_type = inv_type
            prompt = build_outreach_prompt(req_copy)

            try:
                resp = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": ANTHROPIC_API_KEY,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": OUTREACH_MODEL,
                        "max_tokens": 1000,
                        "system": OUTREACH_SYSTEM_PROMPT,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                )
                if resp.status_code != 200:
                    variants.append(OutreachVariant(
                        label=inv_type.title(),
                        subject="Error",
                        body=f"API error: {resp.status_code} — {resp.text[:200]}"
                    ))
                    continue

                data = resp.json()
                text = ""
                for block in data.get("content", []):
                    if block.get("type") == "text":
                        text += block.get("text", "")

                # Parse subject line from response
                subject = ""
                body = text.strip()
                if body.lower().startswith("subject:"):
                    lines = body.split("\n", 1)
                    subject = lines[0].replace("Subject:", "").replace("subject:", "").strip()
                    body = lines[1].strip() if len(lines) > 1 else ""

                label_map = {
                    "generalist": "Generalist",
                    "compounder": "Compounder / quality",
                    "contrarian": "Contrarian / value",
                }
                variants.append(OutreachVariant(
                    label=label_map.get(inv_type, inv_type.title()),
                    subject=subject,
                    body=body,
                ))

            except Exception as e:
                variants.append(OutreachVariant(
                    label=inv_type.title(),
                    subject="Error",
                    body=f"Failed to generate: {str(e)}"
                ))

    return OutreachResponse(variants=variants, generated_at=datetime.now().isoformat())

# ── Helpers ──────────────────────────────────────────────────────────────────

def make_dedup_key(ticker, company, headline):
    """Build a normalised dedup key so the same announcement can't slip through under
    two slightly-different company-name strings or headline whitespace variants.

    Uses ticker as the primary identifier when available — it's the only stable
    cross-source signal. Falls back to a normalised company name otherwise.
    """
    # Normalise headline: lowercase, collapse all whitespace
    h = re.sub(r'\s+', ' ', (headline or "").strip().lower())
    if ticker:
        return f"T:{ticker.upper()}|{h}"
    # No ticker — strip parenthetical tickers and extra whitespace from company name
    c = re.sub(r'\([A-Z0-9\.]{2,6}\)', '', (company or "")).strip().lower()
    c = re.sub(r'\s+', ' ', c)
    return f"C:{c}|{h}"

def extract_ticker(company_text):
    matches = re.findall(r'\(([A-Z0-9\.]{2,6})\)', company_text)
    if matches:
        return matches[-1]
    for ticker, info in WATCHLIST.items():
        if info["name"].lower() in company_text.lower().strip():
            return ticker
    return None

def clean_company_name(raw_text):
    return re.sub(r'\s+', ' ', raw_text.replace("Factsheet", "").strip())

def is_excluded_company(company):
    cl = company.lower()
    return any(e in cl for e in EXCLUDED_COMPANIES) or any(k in cl for k in EXCLUDED_KEYWORDS)

def is_material_type(headline):
    """Include everything by default, only exclude known noise types."""
    hl = headline.lower()
    if any(e in hl for e in EXCLUDED_TYPES): return False
    return True

def is_sector_relevant(company, headline):
    combined = f"{company} {headline}".lower()
    return any(s in combined for s in RELEVANT_SECTORS)

def categorise(headline, source_feed="RNS"):
    h = headline.lower()
    # Early disambiguation: compound phrases that contain keywords from other buckets.
    # "Voting Results" contains 'results' but is admin. "Interim Dividend" contains 'interim'.
    # Without this pre-check the broad keyword cascade below would mis-tag them.
    if any(w in h for w in [
        "voting results", "result of agm", "result of egm", "result of meeting",
        "agm voting", "agm statement", "results of agm", "results of egm",
    ]):
        return "Admin"
    if any(w in h for w in [
        "interim dividend", "final dividend", "special dividend", "quarterly dividend",
        "dividend declaration", "dividend payment", "dividend timetable", "ex-dividend",
    ]):
        return "Dividend"
    # Results — expanded to catch quarterly reports, half-yearly, interim management statements
    if any(w in h for w in [
        "results", "interim", "half-year", "half year", "half-yearly", "half yearly",
        "preliminary", "prelims", "full year", "full-year", "fy results",
        "quarterly report", "q1 report", "q2 report", "q3 report", "q4 report",
        "first quarter", "second quarter", "third quarter", "fourth quarter",
        "annual report", "annual results",
        "interim management statement", "ims",
    ]): return "Results"
    # Trading Update — expanded to catch shorter-form Q-update headlines
    if any(w in h for w in [
        "trading update", "trading statement", "pre-close", "pre close",
        "q1 trading", "q2 trading", "q3 trading", "q4 trading",
        "q1 update", "q2 update", "q3 update", "q4 update",
        "first quarter trading", "second quarter trading", "third quarter trading", "fourth quarter trading",
        "h1 trading", "h2 trading", "year-end trading", "year end trading",
    ]): return "Trading Update"
    # Company Update — broader operational and strategic update phrasings
    if any(w in h for w in [
        "company update", "operational update", "business update", "operational performance",
        "strategic update", "strategic review", "strategy update", "strategy",
        "outlook", "guidance", "guidance upgrade", "guidance update",
        "ahead of expectations", "ahead of market", "below expectations", "in line with expectations",
        "profit warning", "earnings update", "performance update",
        "year-end update", "year end update", "month-end update", "month end update",
        "interim update", "trading and operational",
        "response to press", "response to media", "response to speculation",
    ]): return "Company Update"
    # Contract — broadened
    if any(w in h for w in [
        "contract", "partnership", "agreement", "collaboration",
        "awarded", "mandate", "engagement", "deal", "renew", "renewal",
        "new client", "new customer", "framework agreement", "extension",
        "memorandum of understanding", "mou ", "letter of intent", "loi ",
    ]): return "Contract"
    # Buyback — separated from Fundraise so genuine capital returns aren't lumped with raises
    if any(w in h for w in ["buyback", "buy-back", "buy back", "share purchase", "share repurchase", "repurchase of shares", "transaction in own shares"]): return "Buyback"
    # Fundraise — runs before M&A so 'open offer' / 'primary bid' phrasing doesn't mis-tag as M&A
    if any(w in h for w in ["placing", "rights issue", "fundrais", "share issue", "capital raise", "open offer", "subscription agreement", "retail offer", "primary bid", "bookbuild", "equity raise"]): return "Fundraise"
    # M&A
    if any(w in h for w in [
        "acquisition", "merger", "disposal", "takeover", "offer", "bid",
        "scheme of arrangement", "recommended offer", "recommended cash offer",
        "possible offer", "completion of acquisition", "completion of disposal",
    ]): return "M&A"
    # Board Change
    if any(w in h for w in [
        "appointment", "resignation", "board change", "non-executive",
        "chief executive", "cfo", "ceo", "chairman", "chair appointment",
        "boardroom change", "succession", "stepping down", "steps down",
        "joins the board", "leaves the board", "new director",
    ]): return "Board Change"
    # Shareholding
    if any(w in h for w in ["holding(s) in company", "tr-1", "notification of major", "major shareholding", "significant shareholding"]): return "Shareholding"
    # Director Dealing
    if any(w in h for w in ["director", "pdmr", "person discharging", "directors' dealings"]): return "Director Dealing"
    # Regulatory
    if any(w in h for w in ["regulatory", "fca", "compliance", "sanction", "investigation", "enforcement"]): return "Regulatory"
    # Dividend
    if any(w in h for w in ["dividend", "distribution", "interim dividend", "final dividend", "special dividend"]): return "Dividend"
    # Admin — formal regulatory and listing-related boilerplate that's typically non-material.
    # Runs AFTER all material category checks so anything containing both material and admin keywords (e.g. "Result of Q3" containing both Results and "result of") falls into the material bucket first.
    if any(w in h for w in [
        "block listing", "block-listing",
        "total voting rights", "tvr ", "issued share capital",
        "form 8.3", "form 8.5", "form 8 ", "rule 8.3", "rule 8.5", "rule 8 ",
        "statement of capital",
        "notification of investments", "notification of holdings",
        "compulsory acquisition",
        "stabilisation notice", "pricing notice", "settlement notice",
        "publication of", "document available", "availability of", "documents available",
        "result of agm", "result of egm", "result of meeting", "voting results", "agm voting", "agm statement",
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

def is_today(time_text):
    today = date.today()
    abbrevs = {1:"Jan",2:"Feb",3:"Mar",4:"Apr",5:"May",6:"Jun",7:"Jul",8:"Aug",9:"Sep",10:"Oct",11:"Nov",12:"Dec"}
    return f"{today.day} {abbrevs[today.month]} {today.year}" in time_text

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


async def fetch_shareholding_detail(url: str) -> ShareholdingDetail:
    """Fetch a TR-1/Holdings announcement and parse the AI summary for structured data."""
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

async def fetch_announcement_detail(url: str) -> DetailResponse:
    """Fetch a single Investegate announcement page and extract the RNS body."""
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

            body_html = str(content_div)
            body_text = content_div.get_text("\n", strip=True)

            company_tag = soup.find("h2") or soup.find("h3")
            if company_tag:
                company = company_tag.get_text(strip=True)
        else:
            body_text = "Could not extract announcement body from the page."
            body_html = "<p>Could not extract announcement body from the page.</p>"

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

        key_figures = extract_key_figures(body_text)
        ai_summary = extract_ai_summary(soup)

        return DetailResponse(
            url=url,
            company=company,
            headline=headline,
            body_text=body_text,
            body_html=body_html,
            key_figures=key_figures,
            ai_summary=ai_summary,
            fetched_at=datetime.now().isoformat(),
        )


# ── Homepage Scraping ────────────────────────────────────────────────────────

async def scrape_investegate():
    errors = []
    raw = []
    seen_keys = set()  # For deduplication
    today_str = date.today().strftime("%d/%m/%Y")
    today_archive = date.today().strftime("https://www.investegate.co.uk/today-announcements/%Y-%m-%d")

    # Investegate caps each page at 50 rows (JS-driven "Show 100/200/300" doesn't work via HTTP).
    # Fix: scrape multiple index pages in parallel and deduplicate.
    pages_to_scrape = [
        today_archive,
        "https://www.investegate.co.uk/",
        "https://www.investegate.co.uk/ftse100",
        "https://www.investegate.co.uk/ftse250",
        "https://www.investegate.co.uk/aim",
        "https://www.investegate.co.uk/aquis",
        "https://www.investegate.co.uk/techmark",
    ]

    def parse_rows(html_text):
        """Parse announcement rows from an Investegate HTML page."""
        results = []
        soup = BeautifulSoup(html_text, "html.parser")
        for row in soup.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) < 4:
                continue
            try:
                time_raw = cells[0].get_text(strip=True)
                if not is_today(time_raw):
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

                # Dedup key: ticker-based (or normalised name fallback) so the same
                # announcement scraped from multiple sources collapses to one row
                dedup_key = make_dedup_key(ticker, company_name, headline)
                if dedup_key in seen_keys:
                    continue
                seen_keys.add(dedup_key)

                results.append(Announcement(
                    time=time_display, company=company_name, ticker=ticker,
                    headline=headline, category=categorise(headline, source_feed), url=url,
                    source_feed=source_feed, is_watchlist=ticker in WATCHLIST if ticker else False,
                    sector_relevant=is_sector_relevant(company_name, headline),
                    source="investegate", date_str=today_str,
                ))
            except (IndexError, AttributeError):
                continue
        return results

    # Fetch all pages in parallel
    async with httpx.AsyncClient(headers=HEADERS, timeout=30.0, follow_redirects=True) as client:
        import asyncio
        async def fetch_page(url):
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                return url, resp.text, None
            except httpx.HTTPError as e:
                return url, None, str(e)

        results = await asyncio.gather(*[fetch_page(u) for u in pages_to_scrape])

        for url, html, err in results:
            if err:
                errors.append(f"Failed: {url} — {err}")
                continue
            page_rows = parse_rows(html)
            raw.extend(page_rows)

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
    async with httpx.AsyncClient(headers=HEADERS, timeout=30.0, follow_redirects=True) as client:
        async def fetch_company_page(ticker, info):
            found = []
            today_str = date.today().strftime("%d/%m/%Y")
            try:
                # Investegate company pages: e.g. https://www.investegate.co.uk/company/NRR
                # Tickers with trailing dot (TW., JD., JET2 etc.) are kept as-is — the dot
                # is part of the LSE symbol and Investegate URL slugs preserve it.
                page_url = f"https://www.investegate.co.uk/company/{ticker}"
                resp = await client.get(page_url)
                if resp.status_code != 200:
                    errors.append(f"Company page {ticker}: HTTP {resp.status_code}")
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
                        if not is_today(date_raw):
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
                            ticker_slug = ticker.lower()
                            if f"--{ticker_slug}/" not in url_lower:
                                continue

                        dedup_key = make_dedup_key(ticker, info['name'], headline)
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
                            source="investegate-company", date_str=today_str,
                        ))
                    except (IndexError, AttributeError):
                        continue
            except Exception as e:
                errors.append(f"Company page {ticker}: {type(e).__name__}: {str(e)[:100]}")
            return found

        company_results = await asyncio.gather(
            *[fetch_company_page(t, i) for t, i in WATCHLIST.items()]
        )
        for company_anns in company_results:
            raw.extend(company_anns)

    total_raw = len(raw)

    # Merge with daily cache — preserves earlier announcements that fell off index pages
    all_today = daily_cache.merge(raw)
    total_cached = len(all_today)

    # Persist errors on the cache so /api/rns can surface them between background scrapes
    daily_cache.last_errors = errors

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

def is_noise_category(ann):
    """True if this announcement is in a noisy category and the company is not on the watchlist."""
    return ann.category in NOISE_CATEGORIES and not ann.is_watchlist

def filter_announcements(anns):
    return [a for a in anns
            if not is_excluded_company(a.company)
            and is_material_type(a.headline)
            and not is_noise_category(a)]

def rank_announcements(anns):
    def sk(a):
        p = 3
        if a.is_watchlist: p = 0
        elif a.sector_relevant: p = 1
        elif a.category in ("Results","Trading Update"): p = 2
        return (p, a.time)
    return sorted(anns, key=sk)

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
        await scrape_investegate()

@app.get("/api/rns", response_model=ScrapeResponse)
async def get_rns(
    watchlist_only: bool = Query(False),
    include_all: bool = Query(False),
    limit: int = Query(2000),
    include_fundraise: bool = Query(False, description="Legacy alias — noise filtering is now client-side"),
    force: bool = Query(False, description="Bypass cache and trigger a fresh scrape"),
):
    """Read announcements from the daily cache (kept warm by the background scraper).
    Returns instantly when the cache is populated. Cold-cache and force=true paths
    fall back to a synchronous scrape.

    Category-based noise filtering (Director Dealing, Shareholding, Admin, etc.) is now
    handled client-side. The server returns everything except mining/oil/gas companies
    and lets the UI manage category visibility, which means the user can unhide any
    category with one click via the existing category pills.

    `include_all=true` additionally bypasses the headline-level material-type filter
    (EXCLUDED_TYPES); without it, factsheets, NAVs, and reissue notices are dropped."""
    await _ensure_cache_warm(force=force)
    raw = [Announcement(**v) for v in daily_cache.announcements.values()]
    total_raw = len(raw)
    if include_all:
        # Just exclude mining/oil/gas — let the client decide what to hide
        filtered = [a for a in raw if not is_excluded_company(a.company)]
    else:
        # Apply EXCLUDED_TYPES headline-level filter; still no category-level filter
        filtered = [a for a in raw
                    if not is_excluded_company(a.company)
                    and is_material_type(a.headline)]
    if watchlist_only: filtered = [a for a in filtered if a.is_watchlist]
    ranked = rank_announcements(filtered)
    scraped_at = daily_cache.last_scrape.isoformat() if daily_cache.last_scrape else datetime.now().isoformat()
    return ScrapeResponse(
        announcements=ranked[:limit], total_raw=total_raw, total_filtered=len(filtered),
        scraped_at=scraped_at, date=date.today().strftime("%A %d %B %Y"),
        source="investegate", errors=list(daily_cache.last_errors),
        default_hidden_categories=sorted(NOISE_CATEGORIES),
    )

@app.get("/api/rns/all", response_model=ScrapeResponse)
async def get_rns_unfiltered(limit: int = Query(2000), force: bool = Query(False)):
    """Like /api/rns but with no headline-level or category-level filtering at all.
    Reads from cache."""
    await _ensure_cache_warm(force=force)
    raw = [Announcement(**v) for v in daily_cache.announcements.values()]
    total_raw = len(raw)
    scraped_at = daily_cache.last_scrape.isoformat() if daily_cache.last_scrape else datetime.now().isoformat()
    return ScrapeResponse(
        announcements=raw[:limit], total_raw=total_raw, total_filtered=total_raw,
        scraped_at=scraped_at, date=date.today().strftime("%A %d %B %Y"),
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

@app.post("/api/outreach", response_model=OutreachResponse)
async def generate_outreach_emails(req: OutreachRequest):
    """Generate investor outreach email variants for a given announcement."""
    return await generate_outreach(req)

@app.get("/api/readacross")
async def get_readacross():
    """Return the read-across matrix plus live-triggered read-across companies based on today's cache.
    A read-across company is 'triggered' if it has any announcement in today's cache."""
    # Cold-start safety: if cache is empty, kick off a synchronous scrape so first call returns data
    await _ensure_cache_warm()
    # Pull today's announcements from the daily cache
    today_anns = [Announcement(**v) for v in daily_cache.announcements.values()]

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
    raw = [Announcement(**v) for v in daily_cache.announcements.values()]
    return {"ticker": ticker, "company": WATCHLIST.get(ticker, {}).get("name", "Unknown"),
            "announcements": [a for a in raw if a.ticker == ticker], "errors": list(daily_cache.last_errors)}

@app.post("/api/watchlist/add")
async def add_to_watchlist(ticker: str, name: str, sector: str = "", tier: str = "core"):
    if tier not in ("core", "read_across"):
        tier = "core"
    WATCHLIST[ticker.upper()] = {"name": name, "sector": sector, "tier": tier}
    save_watchlist(WATCHLIST)
    return {"status": "added", "ticker": ticker.upper(), "watchlist": WATCHLIST}

@app.delete("/api/watchlist/{ticker}")
async def remove_from_watchlist(ticker: str):
    removed = WATCHLIST.pop(ticker.upper(), None)
    if removed:
        save_watchlist(WATCHLIST)
    return {"status": "removed" if removed else "not_found", "ticker": ticker.upper()}

@app.post("/api/watchlist/reset")
async def reset_watchlist():
    """Reset watchlist back to defaults."""
    WATCHLIST.clear()
    WATCHLIST.update(DEFAULT_WATCHLIST)
    save_watchlist(WATCHLIST)
    return {"status": "reset", "watchlist": WATCHLIST}

@app.get("/api/debug/raw-html")
async def debug_raw_html():
    today_url = date.today().strftime("https://www.investegate.co.uk/today-announcements/%Y-%m-%d")
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
        print(f"  Password protection: ON")
    else:
        print(f"  Password protection: OFF (set APP_PASSWORD env var to enable)")
    print()
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
