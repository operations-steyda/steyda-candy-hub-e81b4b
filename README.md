# STEYDA — Candy Operating Dashboard + Billy

A self-contained web dashboard for the STEYDA (Hali Design AB) Amazon FBA candy
business, with **Billy** — a built-in assistant that answers from the dashboard's
own data and drives the Candy Purchase Planner.


## Files
- **`index.html`** — the full Candy Operating Dashboard (Overview, PPC, Sales &
  Traffic, Inventory, Replenishment, Candy Purchase Planner, Warehouse, Stock
  Level, Shipments) **plus** the "Ask Billy" panel (bottom-right).
- **`planner.html`** — a standalone copy of the Candy Purchase Planner exposing a
  small `window.CandyPlanner` API. Billy loads it in a hidden iframe (same origin,
  shares saved recipe/stock via `localStorage`). Static recipe/price data — not
  affected by data refreshes.

## Billy — what he does
Tap **Ask Billy**, or ask **"what can you do"** (Billy recalls his own capabilities
and recent changes). He reads the dashboard's `PAYLOAD` client-side — no API key,
nothing leaves the page.

- **Inventory:** restock priorities, days of cover, velocity, stockout dates,
  order qty, top/slow sellers, overview.
- **Candy Purchase Planner:** set units to produce (`"produce 60 bubs 0.5"`),
  build the **per-supplier shopping list** with SKUs, check the **9,000 kr MOQ**
  (under-MOQ suppliers auto-topped-up from the cheapest alternative), look up
  article numbers per supplier (ERT / Privab / Dagab).
- **Editable orders:** every shopping-list line's quantity is editable; totals and
  MOQ recompute live and the edited numbers become the cart quantities.

## Purchasing (add-to-cart on supplier sites)
Buying on ertgodis.se / dagab.se / privab.se is **not** done by Billy (a web page
can't drive other sites). It's run by the team's Claude agent in Chrome on a
"go" signal: search each SKU, add to cart, **stop before checkout** — the team
finalizes payment.

## Refreshing the data (on-demand, from DataDoe)
Billy can't call DataDoe itself; the refresh is a Claude task, run on request
("refresh the dashboard"):
1. Pull FBA / Listings / Sales for Steyda US via the DataDoe MCP into `_build/`.
2. `dd_transform.py` → `rebuild_candy.py` → `customize.py` (build scripts in
   `Downloads/_build`).
3. `inject_billy.py` — re-injects Billy (+ noindex, MOQ 9,000) into the fresh
   dashboard and writes this repo's `index.html`. `planner.html` is left as-is.
4. Validate locally, then commit + push — GitHub Pages redeploys.

Caveats: DataDoe holds ~1 month of history so "YTD" sales undercount Amazon's
true totals (inventory/velocity are fresh + complete); DataDoe has flagged its
Sales & Traffic table deprecated (migrate to "Profit by SKU & Date" later); the
Joar warehouse numbers come from the existing sheet unless re-pulled.

## Optional: real LLM backend
`AGENT_CONFIG` in `index.html` has an `endpoint` hook. Point it at a serverless
proxy that holds an API key to have a real LLM answer open-ended questions (the
Cloudflare Worker code lived in `worker/`, kept in git history). Never put a key
in this file — it's a public page.
