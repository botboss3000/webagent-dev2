# Structured web scraping (bring-your-own provider)

This skill is attached to the **Web Scraper** ability. It loads on demand — the
agent sees the one-line "when to use" in its `# [SKILLS]` list and pulls this full
body with `load_skill` only when a task actually needs structured scraping.

## When to use

Use these tools when a task asks you to **extract structured data** (product
titles, prices, listings, search results, contact info, tables) from a real
website that has **no public API** and that a plain fetch can't get — typically
because the page is JavaScript-rendered or actively blocks bots.

This ability routes through a **bring-your-own scraping provider** (Apify,
RapidAPI, or a custom HTTP endpoint) that an admin configures with an API key. The
provider does the heavy lifting (headless rendering, proxy rotation,
anti-bot handling) and returns structured results.

## The tools — ALWAYS use these for scraping, never `web_search`

- **`web_scrape_url(url, render_js=false)`** — scrape a single page. Set
  `render_js=true` for SPAs / JS-heavy pages. Returns the page's structured/extracted
  content from the provider.
- **`web_scrape_search(query, location="", filters={}, max_results=20)`** — run a
  search through the configured scraper actor and get structured result items.

> **Do not substitute `web_search` for scraping.** `web_search` (from the Web
> Access ability) returns a handful of search-engine snippets — it will NOT give
> you the product titles / prices / listings on a specific page, and looping it
> with ever-more-specific queries is a dead end. When the user says "scrape",
> "extract", "pull the listings/titles/prices from this site", reach for
> `web_scrape_url` / `web_scrape_search`. If you only need a single static page's
> raw HTML and the site doesn't block you, `http_request` (Browser Control) or
> driving the in-app browser are also fine — but the scrape tools are the right
> default for structured extraction at scale.

## If the scraper is NOT configured (no API key)

These tools require a provider + API key set by an admin in
**App Config → Agent Settings → Web → Web Scraper**. When that isn't set, the very
first scrape call returns:

```
{"status": "not_configured", "provider": "scraper", "message": "Web Scraper is not configured …"}
```

When you see `not_configured` (or a message about a missing/empty API key,
endpoint, Actor ID, or RapidAPI host):

1. **Stop. Do not loop, and do not fall back to `web_search` to guess the
   answer.** Without the provider you cannot scrape the page, and search snippets
   are not the page's real data.
2. **Tell the user plainly** that the Web Scraper ability has no provider/API key
   configured yet, and that an admin needs to set one (provider + API key, plus the
   Apify Actor ID / RapidAPI host / custom endpoint that provider needs) in
   App Config → Agent Settings → Web → Web Scraper.
3. **Do not fabricate results.** Never invent product titles, prices, or listings
   you didn't actually retrieve. It is correct to return nothing and explain the
   missing-credential state.

## Reading the result

Every call returns JSON with a `status`. Beyond `not_configured`:

- `status: "ok"` — `results` / `body` holds the provider's structured output.
  Summarize it for the user; cite the source URL.
- `status: "error"` — surface the `message` (e.g. an Apify Actor ID is required, a
  RapidAPI host/endpoint is missing, or an upstream HTTP error). These usually mean
  a provider field still needs filling in — relay that to the user rather than
  retrying blindly.
