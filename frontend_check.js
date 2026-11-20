/*
 * Frontend checks for the RNS Monitor.
 *
 * Run from the repo root:
 *     npm install        (once)
 *     npm run build      (compile index.src.html into static/index.html)
 *     npm run test:frontend
 *
 * Tests the compiled file that is actually deployed (static/index.html), in a simulated
 * browser with a mock backend, so no server is needed. Each check is named after the
 * behaviour it protects; several exist because the behaviour once broke in use.
 */
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const ROOT = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(ROOT, "static", "index.html"), "utf8");
const m = html.match(/<script>\n\/\* Precompiled[\s\S]*?\*\/([\s\S]*?)<\/script>/);
if (!m) { console.error("Could not find the compiled script in static/index.html. Run npm run build."); process.exit(1); }
const APP = m[1];
// React's package "exports" hide the UMD files from require.resolve, so locate the package
// folder and read them directly. These are the same builds the page loads from its CDN.
const pkgDir = (name) => path.dirname(require.resolve(`${name}/package.json`));
const REACT = fs.readFileSync(path.join(pkgDir("react"), "umd", "react.production.min.js"), "utf8");
const REACT_DOM = fs.readFileSync(path.join(pkgDir("react-dom"), "umd", "react-dom.production.min.js"), "utf8");

const results = [];
const check = (name, ok) => results.push([name, !!ok]);
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const londonToday = () => new Date().toLocaleDateString("en-GB", { timeZone: "Europe/London" });

function ann(over) {
  return Object.assign({
    time: "07:00", company: "Example plc", ticker: "EX", headline: "Trading Update",
    category: "Trading Update", is_watchlist: false, source_feed: "RNS",
    url: "https://www.investegate.co.uk/a/ex--ex/x/1", date_str: londonToday(),
    sector_relevant: false, excluded_sector: false,
  }, over);
}

const WATCHLIST = {
  PSN: { name: "Persimmon", sector: "Housebuilding", tier: "core" },
  FTC: { name: "Filtronic", sector: "RF", tier: "read_across" },
};
const BASE_FEED = [
  ann({ ticker: "PSN", company: "Persimmon", headline: "Full Year Results", category: "Results", is_watchlist: true, url: "https://x/psn" }),
  ann({ ticker: "FTC", company: "Filtronic", headline: "Trading Update", is_watchlist: true, url: "https://x/ftc" }),
  ann({ ticker: "TRB", company: "Tribal Group", headline: "Increase in Price for the Recommended Acquisition", category: "M&A", url: "https://x/trb", date_str: "27/09/2026", time: "18:00" }),
];

function res(status, body, headers = {}) {
  const h = Object.fromEntries(Object.entries(headers).map(([k, v]) => [k.toLowerCase(), v]));
  return Promise.resolve({
    ok: status >= 200 && status < 300, status,
    headers: { get: (k) => h[k.toLowerCase()] ?? null },
    json: () => Promise.resolve(body),
  });
}

function defaultServer({ feed = BASE_FEED, etag = '"v1"' } = {}) {
  return (url, opts) => {
    if (url.includes("/api/rns/detail")) return res(200, { ai_summary: "A summary.", key_figures: [], body_text: "Body.", fetched_at: new Date().toISOString() });
    if (url.includes("/api/rns?")) return res(200, { date: "d", announcements: feed, errors: [], default_hidden_categories: ["Admin"] }, { ETag: etag });
    if (url.includes("/api/watchlist")) return res(200, { watchlist: WATCHLIST });
    if (url.includes("/api/readacross")) return res(200, { matrix: { FTC: { CML: {} } }, rel_types: {}, core: [], read_across: [], triggered: {}, affected_core: {} });
    if (url.includes("/api/health")) return res(200, {
      watchlist_storage: { durable: true, note: "Watchlist changes are saved to Postgres and survive restarts." },
      last_scrape_stats: { finished_at: new Date().toISOString(), duration_seconds: 4.2, sources: {
        "https://www.investegate.co.uk/today-announcements/2026-09-28": { pages: 3, rows_in_session: 140, ok: true },
        "https://www.investegate.co.uk/aim": { pages: 1, rows_in_session: 0, ok: false, error: "HTTP 500" },
        company_pages: { requested: 46, failed: [], rows_in_session: 12 },
      } },
    });
    return res(200, {});
  };
}

function boot({ width = 1400, prefs = null, server = defaultServer(), version = null } = {}) {
  // `version` stands in for the stamp app.py puts in the page as it serves it.
  const head = version ? `<head><meta name="app-version" content="${version}"></head>` : "";
  const dom = new JSDOM(`<!DOCTYPE html><html>${head}<body><div id="root"></div></body></html>`,
    { url: "https://localhost/", runScripts: "outside-only", pretendToBeVisual: true });
  const { window } = dom;
  window.self = window;
  Object.defineProperty(window, "innerWidth", { value: width, writable: true, configurable: true });
  window.matchMedia = window.matchMedia || (() => ({ matches: false, addListener() {}, removeListener() {} }));
  if (prefs) window.localStorage.setItem("rns_prefs_v1", JSON.stringify(prefs));
  const calls = [];
  window.fetch = (url, opts = {}) => { calls.push({ url: String(url), opts }); return server(String(url), opts); };
  window.eval(REACT); window.eval(REACT_DOM); window.eval(APP);
  return { window, doc: window.document, calls };
}

const text = (doc) => doc.body.textContent || "";
const btn = (doc, label) => [...doc.querySelectorAll("button")].find((b) => b.textContent.trim().startsWith(label));
const key = (w, k) => w.dispatchEvent(new w.KeyboardEvent("keydown", { key: k, bubbles: true }));

async function main() {
  // ── Core rendering ────────────────────────────────────────────────────────
  {
    const { window, doc, calls } = boot();
    await wait(400);
    check("renders the feed", doc.querySelectorAll(".row").length === 3);
    check("core and read-across tags shown", doc.querySelector(".row-tag.tier-core") && doc.querySelector(".row-tag.tier-ra"));
    check("read-across row shows its core target", (doc.querySelector(".row-ra-map") || {}).textContent?.includes("CML"));
    check("auto-refresh is on by default", /Auto ON/.test(text(doc)));
    check("row meta splits tags from controls (no clipping on mobile)",
      doc.querySelector(".row-meta-tags") && doc.querySelector(".row-meta-ctrl .row-read-btn"));
    const trb = [...doc.querySelectorAll(".row")].find((r) => r.textContent.includes("Tribal"));
    check("earlier-session release carries a day tag", trb && /SUN 27/.test(trb.textContent));

    const icons = [...doc.querySelectorAll(".toolbar-btn.icon")];
    const shape = (b) => [...b.querySelector("svg").children].map((c) => c.tagName).join(",");
    check("theme and options icons are distinct SVGs", icons.length === 2 && shape(icons[0]) !== shape(icons[1]));

    check("button reads Force refresh", !!btn(doc, "Force refresh"));
    btn(doc, "Force refresh").click(); await wait(200);
    key(window, "R"); await wait(200);
    check("no request ever sends hard=true", !calls.some((c) => c.url.includes("hard=true")));
    check("force refresh sends force=true", calls.some((c) => c.url.includes("force=true")));

    check("sends the ETag back on the next poll",
      calls.filter((c) => c.url.includes("/api/rns?")).slice(1).some((c) => (c.opts.headers || {})["If-None-Match"] === '"v1"'));

    key(window, "j"); await wait(120);
    check("j focuses a row", !!doc.querySelector(".row.focused"));
    key(window, "Enter"); await wait(300);
    const exp = doc.querySelector(".expand");
    check("Enter opens the focused announcement", !!exp);
    check("Investegate link sits above the summary",
      exp && exp.querySelector(".detail-actions") && exp.firstElementChild.classList.contains("detail-actions"));
  }

  // ── Row levelling (headline clamp and same-row marking) ─────────────────────
  {
    const { doc } = boot();
    await wait(400);
    const cards = [...doc.querySelectorAll(".row")];
    const hl = cards[0].querySelector(".row-headline");
    check("closed headline carries the full title on hover", hl && hl.getAttribute("title") === hl.textContent);
    const css = (html.match(/<style>([\s\S]*?)<\/style>/) || [, ""])[1];   // the built page's stylesheet
    check("grid no longer forces top alignment on every card",
      !/\.grid \{[^}]*align-items:\s*start/.test(css) && /\.grid > \.row\[data-beside-open\]/.test(css));
    cards[0].querySelector(".row-body").click();
    await wait(300);
    // jsdom has no layout, so every card reports the same top edge: all closed cards count
    // as sharing the open card's row. This checks the marking and unmarking, not geometry.
    check("cards beside an open card are marked", cards.slice(1).every((c) => c.hasAttribute("data-beside-open")));
    check("the open card itself is not marked", !cards[0].hasAttribute("data-beside-open"));
    cards[0].querySelector(".row-body").click();
    await wait(300);
    check("marks are removed when the card closes", !cards.some((c) => c.hasAttribute("data-beside-open")));
  }

  // ── Expanded card: summary and the source link, nothing else ─────────────
  {
    const base = defaultServer();
    const server = (url, opts) => url.includes("/api/rns/detail")
      ? res(200, { ai_summary: "Grainger reported rental growth.", key_figures: [], body_text: "", body_html: "", body_available: false, fetched_at: new Date().toISOString() })
      : base(url, opts);
    const { doc } = boot({ server });
    await wait(400);
    doc.querySelectorAll(".row-body")[0].click(); await wait(300);
    const exp = doc.querySelector(".row.expanded .expand");
    const t = (exp && exp.textContent) || "";
    check("expanded card shows the AI summary", t.includes("Grainger reported rental growth."));
    check("expanded card links to Investegate", !!(exp && [...exp.querySelectorAll("a")].some(a => /View on Investegate/.test(a.textContent))));
    check("reader and paste options are gone",
      !btn(doc, "Open in reader") && !btn(doc, "Paste the release") && !btn(doc, "Show full announcement") && !doc.querySelector("iframe"));
  }

  // ── Price chart link ──────────────────────────────────────────────────────
  {
    const feed = [
      ann({ ticker: "GTLY", company: "Gateley", url: "https://x/gtly" }),
      ann({ ticker: "TW.", company: "Taylor Wimpey", url: "https://x/tw" }),
      ann({ ticker: "0QT8", company: "I-RES (Dublin line)", url: "https://x/ires" }),
    ];
    const { doc } = boot({ server: defaultServer({ feed }) });
    await wait(400);
    const linkFor = async (name) => {
      const row = [...doc.querySelectorAll(".row")].find((r) => r.textContent.includes(name));
      row.querySelector(".row-body").click(); await wait(250);
      const a = [...row.querySelectorAll("a")].find((x) => /Price chart/.test(x.textContent));
      row.querySelector(".row-body").click(); await wait(150);
      return a ? a.getAttribute("href") : null;
    };
    check("price chart link opens the TradingView page for a London ticker", (await linkFor("Gateley")) === "https://www.tradingview.com/symbols/LSE-GTLY/");
    check("tickers ending in a dot use the chart address", (await linkFor("Taylor Wimpey")) === "https://www.tradingview.com/chart/?symbol=LSE%3ATW.");
    check("overseas codes get no price link rather than a broken one", (await linkFor("I-RES")) === null);
  }

  // ── No embedded price chart (removed November 2026) ──────────────────────
  {
    const { doc } = boot();
    await wait(400);
    doc.querySelectorAll(".row-body")[0].click(); await wait(300);
    const exp = doc.querySelector(".row.expanded");
    check("expanded card has no embedded TradingView chart",
      !exp.querySelector(".share-price") && !exp.querySelector('script[src*="tradingview"]'));
  }

  // ── Categories: watchlist safety net, new defaults, Non-Reg label ──────────
  {
    const feed = [
      ann({ ticker: "PSN", company: "Persimmon", headline: "Result of AGM", category: "Admin", is_watchlist: true, url: "https://x/psn-agm" }),
      ann({ ticker: "PSN", company: "Persimmon", headline: "Form 8.3 - Persimmon", category: "Dealing Disclosure", is_watchlist: true, url: "https://x/psn-f8" }),
      ann({ ticker: "XYZ", company: "Other Co", headline: "Total Voting Rights", category: "Admin", url: "https://x/xyz-tvr" }),
      ann({ ticker: "VOF", company: "VinaCapital", headline: "Monthly Report", category: "Fund Update", url: "https://x/vof" }),
      ann({ ticker: "SUS", company: "Suspended plc", headline: "Suspension of Listing", category: "Listing & Advisers", url: "https://x/sus" }),
      ann({ ticker: "PVN", company: "ProVen VCT", headline: "Issue of Equity", category: "Admin", source_feed: "GNW", is_regulatory: true, url: "https://x/pvn" }),
      ann({ ticker: "SGG", company: "Sensys", headline: "We expect a strong finish", category: "Non-Reg", source_feed: "MFN", is_regulatory: false, url: "https://x/sgg" }),
    ];
    const defaults = ["Fundraise","Buyback","Director Dealing","Shareholding","Admin","Dividend","Dealing Disclosure"];
    const server = (url, opts) => url.includes("/api/rns?")
      ? res(200, { date: "d", announcements: feed, errors: [], default_hidden_categories: defaults }, { ETag: '"c1"' })
      : defaultServer({ feed })(url, opts);
    const names = (doc) => [...doc.querySelectorAll(".row")].map((r) => r.textContent);
    {
      const { doc } = boot({ server });
      await wait(450);
      const t = names(doc).join(" | ");
      check("hidden Admin still shows a watchlist company's announcement", t.includes("Result of AGM"));
      check("hidden Admin still hides other companies' routine paperwork", !t.includes("Total Voting Rights"));
      check("dealing disclosures stay hidden even for watchlist companies", !t.includes("Form 8.3 - Persimmon"));
      check("fund updates and listing news are visible by default", t.includes("Monthly Report") && t.includes("Suspension of Listing"));
      const nonreg = [...doc.querySelectorAll(".filter-row")].find((r) => r.textContent.includes("Non-Reg only"));
      check("Non-Reg count follows Investegate's label, not the newswire name", nonreg && / 1$|1$/.test(nonreg.textContent.trim()));
    }
    {
      // Settings saved before this release: Admin deliberately un-hidden, no record of defaults.
      const prefs = { filter: "all", categoryFilter: "all", sortMode: "relevance", sourceFilter: "all",
        excludedCats: ["Fundraise","Buyback","Director Dealing","Shareholding","Dividend"], options: { watchlistIgnoresHidden: false } };
      const { window, doc } = boot({ server, prefs });
      await wait(450);
      const t = names(doc).join(" | ");
      check("a newly added default-hidden category is hidden for existing users", !t.includes("Form 8.3 - Persimmon"));
      check("categories the user chose to show stay shown", t.includes("Total Voting Rights"));
      const saved = JSON.parse(window.localStorage.getItem("rns_prefs_v1"));
      check("the new default is remembered so it's applied only once", (saved.seenDefaults || []).includes("Dealing Disclosure"));
    }
    {
      const prefs = { filter: "all", categoryFilter: "all", sortMode: "relevance", sourceFilter: "all",
        excludedCats: defaults, seenDefaults: defaults, options: { watchlistIgnoresHidden: false } };
      const { doc } = boot({ server, prefs });
      await wait(450);
      check("switching the safety net off hides watchlist routine paperwork too", !names(doc).join(" | ").includes("Result of AGM"));
    }
  }

  // ── Long-open tabs learn about deploys ────────────────────────────────────
  {
    const base = defaultServer();
    const server = (url, opts) => url.includes("/api/rns?")
      ? res(200, { date: "d", announcements: BASE_FEED, errors: [], default_hidden_categories: [] }, { ETag: '"v1"', "X-App-Version": "newbuild0002" })
      : base(url, opts);
    const { doc } = boot({ server, version: "oldbuild0001" });
    await wait(400);
    check("update notice shown when the server runs a newer version", /A new version of the RNS Monitor is available/.test(text(doc)) && !!btn(doc, "Reload now"));
    const same = boot({ server, version: "newbuild0002" });
    await wait(400);
    check("no update notice when versions match", !/A new version/.test(text(same.doc)));
  }

  // ── Card meta stays on one line on wide screens ───────────────────────────
  {
    const feed = [ann({ ticker: "GRI", company: "Grainger", headline: "Preliminary Results", category: "Results", is_watchlist: true, url: "https://x/gri" })];
    const base = defaultServer({ feed });
    const server = (url, opts) => url.includes("/api/readacross")
      ? res(200, { matrix: { GRI: { IRES: {}, FOXT: {}, BYG: {}, PHP: {}, NRR: {}, PSN: {} } } })
      : url.includes("/api/watchlist") ? res(200, { watchlist: { GRI: { name: "Grainger", tier: "read_across" } } }) : base(url, opts);
    const { doc } = boot({ server });
    await wait(400);
    const badge = doc.querySelector(".row-ra-map");
    check("long read-across lists show three names and a count", badge && /IRES FOXT BYG \+3/.test(badge.textContent) && /PSN/.test(badge.getAttribute("title")));
    const css = (html.match(/<style>([\s\S]*?)<\/style>/) || [, ""])[1];
    check("wide screens keep card tags on one line", /@media \(min-width: 881px\)\s*\{\s*\.row-meta \{ flex-wrap:nowrap; \}/.test(css));
  }

  // ── 304 keeps the feed ─────────────────────────────────────────────────────
  {
    let n = 0;
    const base = defaultServer();
    const server = (url, opts) => {
      if (url.includes("/api/rns?")) { n += 1; if (n > 1) return res(304, null, { ETag: '"v1"' }); }
      return base(url, opts);
    };
    const { doc } = boot({ server });
    await wait(400);
    btn(doc, "Force refresh").click(); await wait(300);
    check("304 response keeps the existing feed", doc.querySelectorAll(".row").length === 3);
    check("304 response is not treated as an error", !/Cannot reach the backend/.test(text(doc)));
  }

  // ── Expired sign-in ───────────────────────────────────────────────────────
  {
    let n = 0;
    const base = defaultServer();
    const server = (url, opts) => {
      if (url.includes("/api/rns?")) { n += 1; if (n > 1) return res(401, { detail: "Not authenticated" }); }
      return base(url, opts);
    };
    const prefs = { filter: "all", categoryFilter: "all", sortMode: "relevance", sourceFilter: "all", excludedCats: [], autoRefresh: true, options: { refreshSecs: 1 } };
    const { doc, calls } = boot({ server, prefs });
    await wait(400);
    btn(doc, "Force refresh").click(); await wait(300);
    check("expired sign-in shows a sign-in prompt", /Your sign-in has expired/.test(text(doc)) && !!btn(doc, "Sign in again"));
    check("expired sign-in is not reported as the backend being unreachable", !/Cannot reach the backend/.test(text(doc)));
    const before = calls.filter((c) => c.url.includes("/api/rns?")).length;
    await wait(2500);
    check("polling stops while signed out", calls.filter((c) => c.url.includes("/api/rns?")).length === before);
  }

  // ── Batched rendering ─────────────────────────────────────────────────────
  {
    const feed = Array.from({ length: 400 }, (_, i) => ann({ ticker: `T${i}`, company: `Co ${i}`, headline: `Trading Update ${i}`, url: `https://x/${i}` }));
    const { doc } = boot({ server: defaultServer({ feed }) });
    await wait(600);
    check("renders the first 150 of 400", doc.querySelectorAll(".row").length === 150);
    check("says how many are not yet shown", /250 not yet shown/.test(text(doc)));
    btn(doc, "Show 150 more").click(); await wait(300);
    check("Show more renders the next batch", doc.querySelectorAll(".row").length === 300);
  }

  // ── Mining and metals switch ──────────────────────────────────────────────
  {
    const feed = [
      ann({ ticker: "FRES", company: "Fresnillo", headline: "Production Report", url: "https://x/fres", excluded_sector: true }),
      ann({ ticker: "PAF", company: "Pan African Resources", headline: "Recommended Cash Offer", category: "M&A", url: "https://x/paf", excluded_sector: false }),
      ann({ ticker: "HBR", company: "Harbour Energy", headline: "Trading Update", url: "https://x/hbr" }),
    ];
    const { doc } = boot({ server: defaultServer({ feed }) });
    await wait(400);
    const names = () => [...doc.querySelectorAll(".row")].map((r) => r.textContent);
    check("mining company hidden by default", !names().some((t) => t.includes("Fresnillo")));
    check("M&A involving a mining company still shown", names().some((t) => t.includes("Pan African")));
    check("energy company shown", names().some((t) => t.includes("Harbour Energy")));
    check("a chip says mining is hidden", /1 mining and metals hidden/.test(text(doc)));
    [...doc.querySelectorAll(".filter-row")].find((r) => r.textContent.includes("Mining and metals")).click();
    await wait(200);
    check("switch reveals mining companies", names().some((t) => t.includes("Fresnillo")));
  }

  // ── Watchlist feedback ────────────────────────────────────────────────────
  {
    const base = defaultServer();
    const server = (url, opts) => {
      if (url.includes("/api/watchlist/add")) {
        if (url.includes("ticker=ZZZZ")) return res(400, { status: "error", error: "Investegate has no company page for ZZZZ." });
        return res(200, { status: "added", warning: "Saved for now, but it will be lost when the server restarts." });
      }
      return base(url, opts);
    };
    const { doc } = boot({ server });
    await wait(400);
    btn(doc, "WL").click(); await wait(200);
    const inputs = [...doc.querySelectorAll(".sheet input")];
    const setVal = (el, v) => { const d = Object.getOwnPropertyDescriptor(el.constructor.prototype, "value"); d.set.call(el, v); el.dispatchEvent(new doc.defaultView.Event("input", { bubbles: true })); };
    const tickerIn = inputs.find((i) => i.placeholder === "Ticker");
    const nameIn = inputs.find((i) => i.placeholder === "Company name");
    setVal(tickerIn, "ZZZZ"); setVal(nameIn, "Nobody plc"); await wait(100);
    btn(doc, "Add company").click(); await wait(300);
    check("rejected ticker shows the server's reason", /no company page for ZZZZ/.test(text(doc)));
    setVal(tickerIn, "KGH"); setVal(nameIn, "Knights Group"); await wait(100);
    btn(doc, "Add company").click(); await wait(300);
    check("accepted ticker shows the storage warning", /Added KGH\. Saved for now/.test(text(doc)));
  }

  // ── Diagnostics ───────────────────────────────────────────────────────────
  {
    const { doc } = boot();
    await wait(400);
    doc.querySelectorAll(".toolbar-btn.icon")[1].click(); await wait(200);
    btn(doc, "Show what the last scrape collected").click(); await wait(300);
    const d = (doc.querySelector(".diag") || {}).textContent || "";
    check("diagnostics show watchlist storage", /saved to Postgres/.test(d));
    check("diagnostics list each source with rows", /today-announcements\/2026-09-28/.test(d) && /140 rows/.test(d));
    check("diagnostics flag a failed source", !!doc.querySelector(".diag-row.bad"));
  }

  // ── Read state uses the London day ────────────────────────────────────────
  {
    const { window, doc } = boot();
    await wait(400);
    doc.querySelector(".row-read-btn").click(); await wait(150);
    const stored = JSON.parse(window.localStorage.getItem("rns_read_v1") || "{}");
    const [d, mth, y] = londonToday().split("/");
    check("read state is keyed on the London date", stored.date === `${y}-${mth}-${d}`);
  }

  // ── Detached panels ───────────────────────────────────────────────────────
  {
    const prefs = { filter: "all", categoryFilter: "all", sortMode: "relevance", sourceFilter: "all", excludedCats: [], options: { expandStyle: "panel" } };
    const { doc } = boot({ prefs });
    await wait(400);
    const bodies = doc.querySelectorAll(".row-body");
    bodies[0].click(); await wait(250); bodies[1].click(); await wait(250);
    check("two detached panels open at once", doc.querySelectorAll(".panel").length === 2);
    check("panels do not block the feed", !doc.querySelector(".sheet-wrap") && doc.querySelectorAll(".row").length === 3);
  }

  // ── Mobile ────────────────────────────────────────────────────────────────
  {
    const { doc } = boot({ width: 390 });
    await wait(400);
    check("mobile layout shows the tab bar", !!doc.querySelector(".mobile-tabbar"));
    [...doc.querySelectorAll(".toolbar-icon-btn")].pop().click(); await wait(150);
    const menu = (doc.querySelector(".overflow-menu") || {}).textContent || "";
    check("mobile menu offers Force refresh, theme and options",
      /Force refresh/.test(menu) && /Switch to (light|dark) mode/.test(menu) && /Options/.test(menu));
  }

  let failed = 0;
  for (const [name, ok] of results) { if (!ok) failed++; console.log((ok ? "  ok   " : "  FAIL ") + name); }
  console.log(`\n${results.length - failed}/${results.length} frontend checks passed`);
  process.exit(failed ? 1 : 0);
}

setTimeout(() => { console.error("Timed out"); process.exit(2); }, 60000);
main().catch((e) => { console.error(e); process.exit(1); });
