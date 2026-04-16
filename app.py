"""
h2Radnor RNS Monitor — Web Deployment v2.0
Combined API + Frontend, with optional password gate.
"""

import re
import json
import asyncio
import os
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
PORT = int(os.environ.get("PORT", "8000"))
HOST = os.environ.get("HOST", "0.0.0.0")

app = FastAPI(title="h2Radnor RNS Monitor", version="2.0.0")

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

def check_auth(request: Request):
    """Check if user is authenticated. Skip if no password is set."""
    if not APP_PASSWORD:
        return True
    token = request.cookies.get(AUTH_COOKIE)
    if token != APP_PASSWORD:
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
    if password == APP_PASSWORD:
        response = RedirectResponse("/", status_code=302)
        response.set_cookie(AUTH_COOKIE, APP_PASSWORD, httponly=True, max_age=86400)
        return response
    return RedirectResponse("/login?error=1", status_code=302)

# ── Serve Frontend ───────────────────────────────────────────────────────────

STATIC_DIR = Path(__file__).parent / "static"

@app.get("/", response_class=HTMLResponse)
async def serve_frontend(request: Request):
    if APP_PASSWORD:
        token = request.cookies.get(AUTH_COOKIE)
        if token != APP_PASSWORD:
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
    token = request.cookies.get(AUTH_COOKIE)
    if token != APP_PASSWORD:
        return Response(content='{"detail":"Not authenticated"}', status_code=401, media_type="application/json")
    return await call_next(request)

# ── Watchlist (persistent) ───────────────────────────────────────────────────

WATCHLIST_FILE = Path(__file__).parent / "watchlist.json"

DEFAULT_WATCHLIST = {
    "HSW":  {"name": "Hostelworld Group",               "sector": "Online travel / hostels"},
    "VEIL": {"name": "Vietnam Enterprise Investments",   "sector": "Emerging markets investment trust"},
    "PSN":  {"name": "Persimmon",                        "sector": "Housebuilding"},
    "FOXT": {"name": "Foxtons Group",                    "sector": "Estate agency / lettings"},
    "PHP":  {"name": "Primary Health Properties",        "sector": "Healthcare REIT"},
    "CML":  {"name": "CML Microsystems",                 "sector": "Semiconductors / RF technology"},
    "GTLY": {"name": "Gateley Holdings",                 "sector": "Legal / professional services"},
    "FNTL": {"name": "Fintel",                           "sector": "Fintech / financial services distribution"},
    "BMY":  {"name": "Bloomsbury Publishing",            "sector": "Publishing"},
    "SEE":  {"name": "Seeing Machines",                  "sector": "Driver monitoring / AI technology"},
    "PPHE": {"name": "PPHE Hotel Group",                 "sector": "Hospitality / hotels"},
    "ROSE": {"name": "Rosebank Industries",              "sector": "Industrials / acquisition vehicle"},
    "ALU":  {"name": "Alumasc Group",                    "sector": "Building products / construction"},
    "BYG":  {"name": "Big Yellow Group",                 "sector": "Self-storage REIT"},
    "CPP":  {"name": "CPP Group",                        "sector": "Insurance / assistance products"},
    "HFG":  {"name": "Hilton Foods",                     "sector": "Food production / FMCG"},
    "IDOX": {"name": "Idox",                             "sector": "GovTech / software"},
    "IPF":  {"name": "International Personal Finance",   "sector": "Consumer finance / lending"},
    "IRES": {"name": "Irish Residential Properties REIT","sector": "Residential property REIT"},
    "MTEC": {"name": "Made Tech Group",                  "sector": "Technology / digital services"},
    "NRR":  {"name": "NewRiver REIT",                    "sector": "Retail property REIT"},
    "NFG":  {"name": "Next 15 Group",                    "sector": "Marketing / communications"},
    "REC":  {"name": "Record",                           "sector": "Currency management / financial services"},
    "TRI":  {"name": "Trifast",                          "sector": "Industrial fasteners / manufacturing"},
}

def load_watchlist() -> dict:
    """Load watchlist from JSON file, falling back to defaults if file doesn't exist."""
    if WATCHLIST_FILE.exists():
        try:
            with open(WATCHLIST_FILE, "r") as f:
                return json.load(f)
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

# ── Daily Announcement Cache ─────────────────────────────────────────────────
# Stores all announcements seen today. Merges with fresh scrapes so earlier
# announcements that fall off index pages are preserved all day.

class DailyCache:
    def __init__(self, cache_file: Path):
        self.cache_file = cache_file
        self.announcements: dict[str, dict] = {}
        self.cache_date: Optional[str] = None
        self.last_scrape: Optional[datetime] = None
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
        """Merge new scrape results into the cache, return the full combined list."""
        self._check_date()
        for ann in new_announcements:
            key = f"{ann.company}|{ann.headline}"
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
    "nav", "form 8.3", "form 8 ", "form8", "block listing",
    "total voting rights", "issue of equity",
    "prospectus", "circular",
    "agm", "egm", "annual general meeting", "extraordinary general meeting",
    "share incentive plan", "ltip",
    "admission to trading", "final terms", "exercise of warrants",
    "annual and sustainability", "annual financial report",
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
    direction: Optional[str] = None  # "increase", "decrease", or "unchanged"
    change_pp: Optional[float] = None  # percentage point change
    voting_rights: Optional[str] = None
    date_of_change: Optional[str] = None
    summary: Optional[str] = None  # The raw AI summary text
    fetched_at: str
    error: Optional[str] = None

# ── Helpers ──────────────────────────────────────────────────────────────────

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
    if any(w in h for w in ["results", "interim", "half-year", "preliminary", "full year", "full-year"]): return "Results"
    if any(w in h for w in ["trading update", "trading statement", "q1 trading", "q2 trading", "q3 trading", "q4 trading", "pre-close"]): return "Trading Update"
    if any(w in h for w in ["company update", "operational update", "business update", "strategic update", "strategic review", "strategy", "outlook", "guidance", "notice of results"]): return "Company Update"
    if any(w in h for w in ["acquisition", "merger", "disposal", "takeover", "offer", "bid", "scheme of arrangement"]): return "M&A"
    if any(w in h for w in ["contract", "partnership", "agreement", "collaboration", "awarded", "mandate", "engagement", "deal", "renew"]): return "Contract"
    if any(w in h for w in ["placing", "rights issue", "fundrais", "share issue", "buyback", "share purchase", "repurchase", "capital raise", "transaction in own shares"]): return "Fundraise"
    if any(w in h for w in ["appointment", "resignation", "board change", "non-executive", "chief executive", "cfo", "ceo"]): return "Board Change"
    if any(w in h for w in ["holding(s) in company", "tr-1", "notification of major", "major shareholding", "significant shareholding"]): return "Shareholding"
    if any(w in h for w in ["director", "pdmr"]): return "Director Dealing"
    if any(w in h for w in ["regulatory", "fca", "compliance", "sanction"]): return "Regulatory"
    if any(w in h for w in ["dividend", "distribution"]): return "Dividend"
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
            # Clean up the HTML — remove scripts, styles, ads
            for tag in content_div.find_all(["script", "style", "iframe", "noscript"]):
                tag.decompose()

            body_html = str(content_div)
            body_text = content_div.get_text("\n", strip=True)

            # Try to extract company name from the page
            # Often in a heading or specific element above the body
            company_tag = soup.find("h2") or soup.find("h3")
            if company_tag:
                company = company_tag.get_text(strip=True)
        else:
            body_text = "Could not extract announcement body from the page."
            body_html = "<p>Could not extract announcement body from the page.</p>"

        # Clean up body text — remove Investegate boilerplate
        boilerplate_markers = [
            "This information is provided by RNS",
            "Please note, this site uses cookies",
            "To continue using Investegate",
            "Privacy and Cookie Policy",
            "Terms and conditions relating to",
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

                # Dedup key: company + headline (same announcement won't appear twice)
                dedup_key = f"{company_name}|{headline}"
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

    # Supplement with RSS feeds for watchlist companies ONLY for announcements
    # genuinely FROM that company (validated by checking ticker appears in URL)
    async with httpx.AsyncClient(headers=HEADERS, timeout=30.0, follow_redirects=True) as client:
        async def fetch_rss(ticker, info):
            found = []
            try:
                rss_url = f"https://www.investegate.co.uk/Index.aspx?action=RSS&company={ticker}"
                resp = await client.get(rss_url)
                if resp.status_code != 200:
                    return found
                rss_soup = BeautifulSoup(resp.text, "xml")
                items = rss_soup.find_all("item")
                for item in items:
                    title = item.find("title")
                    link = item.find("link")
                    pub_date = item.find("pubDate")
                    if not title:
                        continue
                    headline = title.get_text(strip=True)
                    if not headline:
                        continue

                    url = link.get_text(strip=True) if link else None

                    # CRITICAL: only include if the URL contains this company's ticker
                    # Investegate URLs: /announcement/rns/company-name--TICKER/headline/id
                    # This filters out "related" announcements from other companies
                    if not url:
                        continue
                    url_lower = url.lower()
                    ticker_lower = ticker.lower()
                    if f"--{ticker_lower}/" not in url_lower:
                        continue

                    # Check if from today
                    date_text = pub_date.get_text(strip=True) if pub_date else ""
                    today = date.today()
                    month_abbrevs = {1:"Jan",2:"Feb",3:"Mar",4:"Apr",5:"May",6:"Jun",7:"Jul",8:"Aug",9:"Sep",10:"Oct",11:"Nov",12:"Dec"}
                    today_check = f"{today.day} {month_abbrevs[today.month]} {today.year}"
                    if today_check not in date_text:
                        continue

                    time_match = re.search(r'(\d{2}):(\d{2})', date_text)
                    time_display = f"{time_match.group(1)}:{time_match.group(2)}" if time_match else "00:00"

                    dedup_key = f"{info['name']}|{headline}"
                    if dedup_key in seen_keys:
                        continue
                    seen_keys.add(dedup_key)

                    sf = "RNS"
                    if "/gnw/" in url_lower: sf = "GNW"
                    elif "/ukn/" in url_lower: sf = "UKN"
                    elif "/prn/" in url_lower: sf = "PRN"
                    elif "/mfn/" in url_lower: sf = "MFN"
                    elif "/eqs/" in url_lower: sf = "EQS"

                    found.append(Announcement(
                        time=time_display, company=info["name"], ticker=ticker,
                        headline=headline, category=categorise(headline, sf), url=url,
                        source_feed=sf, is_watchlist=True, sector_relevant=True,
                        source="investegate-rss", date_str=date.today().strftime("%d/%m/%Y"),
                    ))
            except Exception:
                pass
            return found

        rss_results = await asyncio.gather(
            *[fetch_rss(t, i) for t, i in WATCHLIST.items()]
        )
        for rss_anns in rss_results:
            raw.extend(rss_anns)

    total_raw = len(raw)

    # Merge with daily cache — preserves earlier announcements that fell off index pages
    all_today = daily_cache.merge(raw)
    total_cached = len(all_today)

    return all_today, total_cached, errors

def filter_announcements(anns):
    return [a for a in anns if not is_excluded_company(a.company) and is_material_type(a.headline)]

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
    return {
        "status": "ok",
        "timestamp": datetime.now().isoformat(),
        "cache_size": daily_cache.count,
        "last_scrape": daily_cache.last_scrape.isoformat() if daily_cache.last_scrape else None,
    }

@app.get("/api/watchlist")
async def get_watchlist():
    return {"watchlist": WATCHLIST}

@app.get("/api/rns", response_model=ScrapeResponse)
async def get_rns(watchlist_only: bool = Query(False), include_all: bool = Query(False), limit: int = Query(500)):
    raw, total_raw, errors = await scrape_investegate()
    filtered = [a for a in raw if not is_excluded_company(a.company)] if include_all else filter_announcements(raw)
    if watchlist_only: filtered = [a for a in filtered if a.is_watchlist]
    ranked = rank_announcements(filtered)
    return ScrapeResponse(
        announcements=ranked[:limit], total_raw=total_raw, total_filtered=len(filtered),
        scraped_at=datetime.now().isoformat(), date=date.today().strftime("%A %d %B %Y"),
        source="investegate", errors=errors,
    )

@app.get("/api/rns/all", response_model=ScrapeResponse)
async def get_rns_unfiltered(limit: int = Query(500)):
    raw, total_raw, errors = await scrape_investegate()
    return ScrapeResponse(
        announcements=raw[:limit], total_raw=total_raw, total_filtered=total_raw,
        scraped_at=datetime.now().isoformat(), date=date.today().strftime("%A %d %B %Y"),
        source="investegate", errors=errors,
    )

@app.get("/api/rns/detail", response_model=DetailResponse)
async def get_rns_detail(url: str = Query(..., description="Full Investegate announcement URL")):
    """Fetch and parse a single announcement's full body text."""
    return await fetch_announcement_detail(url)

@app.get("/api/rns/shareholding", response_model=ShareholdingDetail)
async def get_shareholding_detail(url: str = Query(..., description="Full Investegate TR-1/Holdings announcement URL")):
    """Fetch a TR-1/Holdings announcement and return structured shareholding data."""
    return await fetch_shareholding_detail(url)

@app.get("/api/rns/{ticker}")
async def get_rns_by_ticker(ticker: str):
    ticker = ticker.upper()
    raw, _, errors = await scrape_investegate()
    return {"ticker": ticker, "company": WATCHLIST.get(ticker, {}).get("name", "Unknown"),
            "announcements": [a for a in raw if a.ticker == ticker], "errors": errors}

@app.post("/api/watchlist/add")
async def add_to_watchlist(ticker: str, name: str, sector: str = ""):
    WATCHLIST[ticker.upper()] = {"name": name, "sector": sector}
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
