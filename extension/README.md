# STEYDA Candy Order Helper — Chrome extension

Lets the dashboard's **Billy** push the candy shopping list into the **ERT / Dagab /
Privab** carts. **Add-to-cart only** — it never checks out, pays, or enters addresses.
You review each cart and place the order yourself.

## How it fits together
- **Billy (dashboard):** builds the per-supplier order (candy · SKU · qty).
- **This extension:** has permission to act on the three supplier sites. Billy sends
  it the order; it searches each SKU and clicks add-to-cart.
- You must be **logged in** to each supplier in this Chrome profile first.

## Install (one-time)
1. Open `chrome://extensions`.
2. Turn on **Developer mode** (top-right).
3. Click **Load unpacked** and select this `extension/` folder.
4. Copy the **Extension ID** shown on its card (a long letter string).
5. **Send me that Extension ID** — I'll wire the "🧩 Add to cart" button into Billy
   (it goes into `STEYDA_EXT_ID`, then I re-encrypt + push the dashboard).

## Supplier status
- **ERT — ✅ automated.** Uses the Quick-order bulk grid at
  `webbshop.ertgodis.se/sv/Meny/Snabborder` (fills rows, Check, then Shop = add to cart).
- **Privab — ✅ automated.** Uses the Express-purchase page `privab.se/expresskoep`:
  per SKU it searches the article number, sets the quantity, and clicks
  "Add everything to cart". Use **Privab article numbers** (e.g. `47-16137`).
- **Dagab — ✋ manual.** Dagab's shop is behind a human-verification ("confirm you
  are human") check, which we will not bypass. Order Dagab by hand.

All of it is **add-to-cart only** — the extension never clicks checkout / "Cash".

## Safety
- No checkout, no payment, no address entry — ever. It stops at "added to cart".
- No passwords stored; it uses your existing logged-in session.
- Only talks to the dashboard origin (`operations-steyda.github.io`) and the three
  supplier domains (declared in `manifest.json`).
- If a supplier changes their page layout, the selectors need updating — that's the
  ongoing maintenance cost of the extension route.

## Files
- `manifest.json` — permissions + which page may message it.
- `background.js` — receives the order, drives each supplier (selectors live here).
- `dashboard-bridge.js` — reference for how Billy messages the extension.
