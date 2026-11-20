# h2Radnor RNS Monitor

Internal tool for h2Radnor Capital Markets (IR advisory, London). It scrapes Investegate for
London Stock Exchange regulatory announcements and serves a filtered, categorised feed of
news relevant to the companies the team covers. The main user is not a developer: explain
changes in plain English and keep the app simple to operate.

## Layout

| File | What it is |
| --- | --- |
| `app.py` | FastAPI backend: scraping, categorisation, watchlist, API. Single file. |
| `index.src.html` | Frontend source (React, JSX). **Edit this one.** |
| `static/index.html` | Compiled frontend that Render serves. **Generated, never edit by hand.** |
| `build.js` | Compiles `index.src.html` into `static/index.html`. |
| `tests/test_backend.py` | Backend checks against a mock Investegate (and Postgres, optionally). |
| `tests/frontend_check.js` | Frontend checks in a simulated browser against a mock backend. |
| `test_categorise.py` | Headline categorisation spec and watchlist wiring checks. |

## Commands

```
npm install              # once, installs build and test tooling
npm run build            # after any change to index.src.html
npm test                 # build, then every frontend and backend check
```

Run `npm test` before every commit; every check must pass. Commit `index.src.html` and the
regenerated `static/index.html` together.

## Deployment

Render deploys automatically on every push to `main`. Anything merged is live within a few
minutes, so do not merge anything with failing checks.

Environment variables on Render: `APP_PASSWORD`, `SECRET_KEY`, `DATABASE_URL` (Postgres,
Internal URL; without it the watchlist resets on every restart), and optional tuning values
documented at the top of `app.py`. `requirements.txt` must include `psycopg[binary]`.

## Rules learned the hard way

- **Never clear the daily cache mid-day.** Investegate listing pages show 50 rows, so the
  cache is the only record of announcements that have scrolled off. A "hard refresh" that
  wiped it destroyed a morning's announcements. `hard=true` is now an alias for `force=true`.
- **Keep auto-refresh simple:** one interval, always running, including when the tab is
  hidden. Pausing hidden tabs let Render's free service sleep, and every return then
  needed a slow cold-start recovery.
- **Pagination must stop early.** Listings run newest first; stop at the first page whose
  rows are all cached or all older than the session. `parse_rows` has side effects on the
  shared dedup set, so never call it just to inspect a page (use `row_urls`).
- **Watchlist structures must agree:** `DEFAULT_WATCHLIST`, `READ_ACROSS_TICKERS` and
  `READ_ACROSS_MATRIX`. `test_categorise.py` enforces this.
- **Aliases:** some companies file under a different Investegate code (I-RES is `0QT8`).
  Add them to `TICKER_ALIASES`. The watchlist dialog warns when this is likely.
- **Hidden categories must only contain routine material.** Admin and Dealing Disclosure are
  hidden by default, so anything that can be news (suspensions, delistings, adviser changes,
  AGM statements, circulars, compulsory acquisitions) must land in a visible category such
  as Listing & Advisers. `MUST_BE_VISIBLE` in `test_categorise.py` enforces this; add to it
  whenever something important is found hiding. Watchlist companies ignore hidden categories
  by default (except Dealing Disclosure and Buyback); this is a user option.
- **Non-Reg comes from Investegate's own label** on each row (`regulatory_label`), not the
  newswire's name: GlobeNewswire, PR Newswire, Modular Finance and EQS carry regulatory news too.
- **Categories are recomputed at serve time** (`apply_live_watchlist`), so a rule change
  applies to the whole day's cache on the next refresh.
- **Adding a default-hidden category:** the frontend hides it once for users with saved
  settings (`seenDefaults`) without re-hiding anything they chose to show.
- **Categorisation uses word-boundary matching (`_kw`).** Plain substring checks caused
  mis-tags ("director" inside "directorate"). Takeover vocabulary is checked before
  Results, Contract and Regulatory because those used to capture M&A headlines first.
  Add a case to `test_categorise.py` for every mis-tag found in the live feed.
- **The session window** is today plus any preceding weekend days and bank holidays.
  `UK_BANK_HOLIDAYS` must be extended each year; `/api/health` flags when it runs out.
- **Energy is in scope** (since September 2026). Only mining and metals are excluded, and
  they are flagged rather than dropped so the user can reveal them. M&A is never excluded.
- **Investegate no longer serves release text (since late 2026).** Its announcement pages load
  the release in the browser, so a server-side fetch gets only the AI summary, disclaimer,
  sidebar and footer. `strip_page_chrome` detects this (`body_available: false`) so page
  furniture is never treated as release text. The expanded card shows the AI summary and a
  link to Investegate; an in-app reader and a paste-to-read option were built and then removed
  at the user's request. Investegate's terms restrict the site to private investors and
  prohibit commercial reuse; that question has been raised with the user and is theirs to resolve.
- **Share price charts come from Turquoise, not the LSE.** TradingView's free widgets are not
  allowed to show London Stock Exchange prices, but can show Turquoise (an LSEG venue), where
  UK shares use the ticker plus "L" (`turquoiseSymbol`: FNTL becomes TURQUOISE:FNTLL). Delayed
  15 minutes. Thinly traded AIM names may look sparse there, so the "Price chart" link to the
  full TradingView LSE page stays alongside. Codes starting with a digit get neither.
- **Open tabs never reload themselves**, because auto-refresh fetches data, not the page. The
  server stamps the page and every feed response with a version (`frontend_page`,
  `X-App-Version`); a mismatch shows an update notice and reloads when the tab is hidden.
- **Uploading through GitHub's web page flattens folders.** Files meant for `static/` or
  `tests/` must be uploaded from inside that folder, or they land in the top level.
- **`build.js` must keep `runtime: "classic"`.** Babel 8's default emits ES imports, which
  load to a blank page in a plain script tag. The build aborts if that happens.
- **Verify before assuming.** Two production misses came from assuming what Investegate
  shows. When an announcement is missed, fetch the real page and trace it through
  `categorise`, `in_window`, `extract_ticker` and the source list before changing code.

## House style

British English. No em dashes or double hyphens in UI text, comments or messages. Error
messages say what happened and what to do next. Code comments explain why, especially
where a past bug shaped the code.
