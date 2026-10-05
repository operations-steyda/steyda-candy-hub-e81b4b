/* STEYDA Candy Order Helper — background service worker (MV3)
 *
 * Receives the shopping list from the dashboard (operations-steyda.github.io) and,
 * per supplier, adds the items to that site's cart. ADD-TO-CART ONLY — it never
 * checks out, pays, or enters addresses. The user must be LOGGED IN to each site.
 *
 * Order shape from Billy:
 *   { type:"STEYDA_ORDER", order:{
 *       ert:    [{sku:"ARO66906",  qty:5, name:"Aroma Pussmun"}],
 *       dagab:  [{sku:"101423296", qty:53, name:"BUBS Smultron"}],
 *       privab: [{sku:"47-16151",  qty:3, name:"..."}]
 *   }}
 *
 * ERT is implemented (uses its "Quick order" bulk grid). Dagab & Privab are stubs
 * until their sites are mapped — see README "Filling the selectors".
 */

// ---------- small helpers ----------
function openTab(url) { return chrome.tabs.create({ url, active: true }); }

function waitForLoad(tabId, timeoutMs) {
  return new Promise((resolve) => {
    let done = false;
    const finish = () => { if (!done) { done = true; chrome.tabs.onUpdated.removeListener(listener); setTimeout(resolve, 900); } };
    const listener = (id, info) => { if (id === tabId && info.status === "complete") finish(); };
    chrome.tabs.onUpdated.addListener(listener);
    setTimeout(finish, timeoutMs || 15000); // safety timeout
  });
}

async function runInTab(tabId, func, args) {
  const [res] = await chrome.scripting.executeScript({ target: { tabId }, func, args: args || [] });
  return res ? res.result : undefined;
}

// ---------- ERT: Quick-order bulk grid ----------
// Page: https://webbshop.ertgodis.se/sv/Meny/Snabborder
// item -> #MainContent_grdOrder_r{N}_c0 ; qty -> _c1 ; Check -> #MainContent_CheckOrderButton ;
// Shop(add to cart) -> #MainContent_BuyOrderButton  (ASP.NET postbacks reload the page)
function ertFillGrid(items) {
  function set(el, v) { if (!el) return false; el.value = v; el.dispatchEvent(new Event("input", { bubbles: true })); el.dispatchEvent(new Event("change", { bubbles: true })); return true; }
  var filled = 0, missingRows = 0;
  for (var i = 0; i < items.length; i++) {
    var c0 = document.getElementById("MainContent_grdOrder_r" + i + "_c0");
    var c1 = document.getElementById("MainContent_grdOrder_r" + i + "_c1");
    if (!c0 || !c1) { missingRows++; continue; }
    set(c0, items[i].sku); set(c1, String(items[i].qty)); filled++;
  }
  return { filled: filled, missingRows: missingRows, rowsAvailable: document.querySelectorAll('[id^="MainContent_grdOrder_r"][id$="_c0"]').length };
}
function ertClick(id) { var b = document.getElementById(id); if (b) { b.click(); return true; } return false; }

async function runErt(items) {
  const tab = await openTab("https://webbshop.ertgodis.se/sv/Meny/Snabborder");
  await waitForLoad(tab.id);
  // bail if not logged in (the quick-order grid won't be there)
  const hasGrid = await runInTab(tab.id, () => !!document.getElementById("MainContent_grdOrder_r0_c0"));
  if (!hasGrid) return { supplier: "ert", ok: false, note: "Quick-order grid not found — are you logged in to webbshop.ertgodis.se?" };
  const fill = await runInTab(tab.id, ertFillGrid, [items]);
  if (fill.missingRows) {
    // more items than rows available; ERT grid is fixed-size — report so the user can split
    return { supplier: "ert", ok: false, note: fill.filled + " filled, " + fill.missingRows + " didn't fit (grid has " + fill.rowsAvailable + " rows). Order the rest in a second pass." , fill };
  }
  await runInTab(tab.id, ertClick, ["MainContent_CheckOrderButton"]); // validate (postback)
  await waitForLoad(tab.id);
  await runInTab(tab.id, ertClick, ["MainContent_BuyOrderButton"]);    // add to cart (postback)
  await waitForLoad(tab.id);
  return { supplier: "ert", ok: true, note: "Filled " + fill.filled + " lines, checked, and added to cart. Review the ERT cart before checkout." };
}

// ---------- Dagab: left manual (human-verification / CAPTCHA gate) ----------
// Dagab's shop (handla.dagab.se) is behind a "confirm you are human" bot check,
// which we will not bypass. Dagab orders stay manual.
async function runDagab(items) {
  return { supplier: "dagab", ok: false, note: "Dagab is not automatable — its shop is behind a human-verification check. Order Dagab manually." };
}

// ---------- Privab: express-purchase search flow ----------
// Page: https://privab.se/expresskoep
// Per SKU: type the SKU into the express search box -> a result row appears with a
// quantity input id="Quantity_{SKU}_" -> set qty -> click "Add everything to cart".
// (Exact-SKU search returns just that product, so "Add everything" adds only it.)
// Never clicks the "Cash" (checkout) button.

// Runs IN the page: searches one SKU and sets its quantity. Async (polls for the row).
async function privabSearchAndSetQty(sku, qty) {
  function sleep(ms){ return new Promise(r=>setTimeout(r, ms)); }
  function nativeSet(el, v){
    const d = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value");
    d.set.call(el, v);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }
  // express search box = the type=search named "q" that is NOT in the header
  const searches = [...document.querySelectorAll('input[type=search][name="q"]')];
  const search = searches.find(i => !i.closest("header,[role=banner]")) || searches[0];
  if (!search) return { sku, found: false, note: "express search box not found — are you logged in to privab.se?" };
  nativeSet(search, sku);
  // trigger the search (Enter)
  ["keydown","keypress","keyup"].forEach(t =>
    search.dispatchEvent(new KeyboardEvent(t, { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true })));
  const f = search.closest("form"); if (f && f.requestSubmit) { try { f.requestSubmit(); } catch(e){} }
  // poll for the qty input for this SKU
  const qtyId = "Quantity_" + sku + "_";
  let qtyEl = null;
  for (let i = 0; i < 24; i++) { qtyEl = document.getElementById(qtyId); if (qtyEl) break; await sleep(300); }
  if (!qtyEl) return { sku, found: false, note: "no result row for " + sku + " (check the Privab article number)" };
  nativeSet(qtyEl, String(qty));
  return { sku, found: true, qty: qtyEl.value };
}

// Runs IN the page: clicks "Add everything to cart" (the current search's results).
function privabClickAdd() {
  const btn = [...document.querySelectorAll("button")].find(b => /add everything to cart/i.test((b.textContent||"")));
  if (!btn) return false;
  btn.click();
  return true;
}

async function runPrivab(items) {
  const tab = await openTab("https://privab.se/expresskoep");
  await waitForLoad(tab.id);
  // logged-in / page check
  const hasSearch = await runInTab(tab.id, () =>
    [...document.querySelectorAll('input[type=search][name="q"]')].some(i => !i.closest("header,[role=banner]")));
  if (!hasSearch) return { supplier: "privab", ok: false, note: "Express-purchase search not found — are you logged in to privab.se?" };

  const lines = [];
  for (const it of items) {
    const r = await runInTab(tab.id, privabSearchAndSetQty, [it.sku, it.qty]);
    if (!r || !r.found) { lines.push({ sku: it.sku, ok: false, note: r ? r.note : "failed" }); continue; }
    const clicked = await runInTab(tab.id, privabClickAdd);
    await waitForLoad(tab.id, 8000); // add may postback/navigate
    lines.push({ sku: it.sku, ok: !!clicked, note: clicked ? "added " + it.qty : "qty set but add button missing" });
  }
  const okN = lines.filter(l => l.ok).length;
  return { supplier: "privab", ok: okN > 0, note: "Added " + okN + "/" + items.length + " lines to the Privab cart. Review before checkout.", lines };
}

const RUNNERS = { ert: runErt, dagab: runDagab, privab: runPrivab };

// ---------- message bridge from the dashboard ----------
chrome.runtime.onMessageExternal.addListener((msg, sender, sendResponse) => {
  if (!msg || typeof msg !== "object") return;
  if (msg.type === "STEYDA_PING") {
    sendResponse({ ok: true, ext: "steyda-candy-order-helper", version: "0.3.0", suppliers: { ert: true, dagab: false, privab: true } });
    return;
  }
  if (msg.type === "STEYDA_ORDER") {
    (async () => {
      const order = msg.order || {};
      const report = {};
      for (const key of Object.keys(order)) {
        if (!Array.isArray(order[key]) || !order[key].length) continue;
        const run = RUNNERS[key];
        try { report[key] = run ? await run(order[key]) : { ok: false, note: "unknown supplier" }; }
        catch (e) { report[key] = { ok: false, note: String(e) }; }
      }
      sendResponse({ ok: true, report });
    })();
    return true; // async
  }
});
