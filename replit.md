# Mirage / Scraper Engine Dashboard

An eCommerce data extraction and management dashboard for scraping product data from high-end fashion retailers (Coach, Michael Kors, Cruise Fashion, Marc Jacobs, Karl Lagerfeld, Tory Burch), processing with AI, and exporting Shopify-ready CSVs.

## Run & Operate

```bash
npm run dev        # Express + Vite dev server on port 5000
python app.py      # Flask API on port 8000
bash start.sh      # Production: both servers in parallel
```

Required secrets: `GEMINI_API_KEY`, `DATABASE_URL`, `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`  
Optional (Shopify publisher): `SHOPIFY_STORE_URL`, `SHOPIFY_ACCESS_TOKEN`

## Stack

- **Frontend**: React 19 + Vite + Tailwind CSS + TanStack Query + Framer Motion
- **Backend (Node/TS)**: Express (`server.ts`, port 5000) — Vite dev middleware + Gemini AI proxy
- **Backend (Python)**: Flask (`app.py`, port 8000) — scraping jobs, DB, CSV export, Shopify API
- **Database**: PostgreSQL via Supabase (`core/db.py`) with connection pooling
- **AI**: Google Gemini 2.0 Flash for product data extraction and tag refinement

## Where things live

```
app.py                        # Flask entry point (port 8000)
server.ts                     # Express + Vite dev server (port 5000)
scrapers_run.py               # Scraper orchestrator
scrapers_registry.json        # Scraper config (id, base_url, type)
start.sh                      # Production startup script

core/
  db.py                       # Database layer (PostgreSQL / Supabase)
  shopify_transformer.py      # Shopify CSV generation + normalisation
  shopify_publisher.py        # Shopify Admin API — upload/update/check-oos/delete-oos
  tag_engine.py               # Product tagging, gender detection, handle generation
  pricing_engine.py           # GBP/USD → INR pricing formula with markup tiers

scrapers/coach/coach.py       # Coach scraper + tag/CSV pipeline
scrapers/cruise_fashion/      # Cruise Fashion scraper
scrapers/michael_kors/        # Michael Kors scraper
scrapers/hoka/hoka.py         # Hoka (AU/NZ svelox strategy)

scraped_files/coach_latest.csv    # 11,081 rows, 2,155 products — fully audited
exports/coach_shopify.csv         # Same file, export-ready
scripts/fix_coach_csv.py          # Standalone Coach CSV audit + fix script

src/App.tsx                   # React frontend (dashboard + scraper cards)
```

## Architecture decisions

- **Dual backend**: Flask handles long-running scrape jobs (threads + stop events); Express handles Vite HMR + Gemini proxy. They share no state — frontend polls Flask for progress.
- **Dual-verification safety**: Every Shopify delete checks the `RudraScrapper-{scraper_id}` tag AND the local `shopify_products` DB registry. Falls back to tag-only if registry is empty (fresh install).
- **CSV path resolution**: `_shopify_csv_path()` checks `scraped_files/{id}_latest.csv` first (freshest scraper output), then `exports/{id}_shopify.csv` as fallback. Always uses most current data for Shopify ops.
- **Async Shopify ops**: Upload/Update/Delete OOS run in daemon threads; progress is emitted via `scraper_progress` dict (keyed by scraper_id or `'__global__'` for bulk ops) which the frontend polls every 2s.
- **Immutable audit logs**: Every Shopify action (upload/update/delete/check-oos) writes an immutable row to `shopify_activity_logs` in Supabase. Registry of pushed products tracked in `shopify_products` table.
- **Coach scraper timeout**: coach.py times out after ~600s (Akamai). CSV is rebuilt by re-processing existing `scraped_files/coach_latest.csv` using `scripts/fix_coach_csv.py`.

## Product

- 10 scrapers: Coach, Michael Kors, Karl Lagerfeld, Marc Jacobs, Tory Burch, Cruise Fashion, Mytheresa, The Designer Box UK, UK Polene, Hoka
- Dashboard: per-scraper cards with Run / Restart / Stop / Download CSV + 4 Shopify buttons (Upload / Update / Check OOS / Delete OOS)
- **Global Shopify Command Center**: Upload All / Update All / Check OOS All / Delete OOS All / Full Sync — fan-out across all 10 scrapers with live progress
- **Activity Logs tab**: filterable (scraper, action, status, date, search), paginated, CSV-exportable audit history
- Shopify Admin API integration via `core/shopify_publisher.py` — rate-limited, dual-safety-guarded

## User preferences

- Tags use Cruise Fashion store taxonomy (womens-handbags, mens-footwear, etc.)
- Pricing in INR: `((price × (rate + 12)) + 2000) × markup` — markup 1.25 (USD<$1k), 1.22 (USD≥$1k), 1.25 (GBP)
- All Shopify products tagged `RudraScrapper-{scraper_id}` for safe bulk operations
- Vendor name is hardcoded canonical (not from SFCC sub-brand API)

## Gotchas

- `Men-apparel` is hardcoded in `tag_engine.py` STORE_TAG_TAXONOMY[MEN] with capital M — `fix_coach_csv.py` lowercases it at export time
- Cruise Fashion sitemap scraping starts a background colour-code cache on Flask startup (~12 min first run)
- Coach CSV "Coach Coachtopia" prefix fixed: template was prepending "Coach" to Coachtopia titles
- `map_to_store_tags("MEN", "bags", slug)` uses "bags"→"accessories" (MEN taxonomy has no "bags" key, uses "accessories" fallback)

## Coach CSV — Current State (2026-05-07)

- **11,081 rows, 2,155 products** in `exports/coach_shopify.csv` + `scraped_files/coach_latest.csv`
- All quality checks pass: 0 missing gender tags, 0 wrong broad category, 0 bad casing, 0 "Coach Coachtopia" prefix, 0 empty descriptions
- Gender: 1,534 women / 351 men / 270 unisex
- Broad categories: 679 bags, 653 accessories, 431 apparel, 281 footwear, 111 watches
- Price range: ₹5,044 – ₹1,309,016 (avg ₹34,291)

## Shopify Publisher API Routes

| Route | Method | Action |
|---|---|---|
| `/api/shopify/upload/<id>` | POST | Upload new products (skip existing SKUs) |
| `/api/shopify/update/<id>` | POST | Update title/body/price/images by SKU match |
| `/api/shopify/check-oos/<id>` | GET | List products in Shopify not in current CSV |
| `/api/shopify/delete-oos/<id>` | POST | Delete products not in current CSV (safety-checked) |
