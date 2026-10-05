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

// ---------- Dagab / Privab: not yet mapped ----------
async function runDagab(items) {
  return { supplier: "dagab", ok: false, note: "Dagab not configured yet — needs its site mapped (log in and we'll add it)." };
}
async function runPrivab(items) {
  return { supplier: "privab", ok: false, note: "Privab not configured yet — needs its site mapped (log in and we'll add it)." };
}

const RUNNERS = { ert: runErt, dagab: runDagab, privab: runPrivab };

// ---------- message bridge from the dashboard ----------
chrome.runtime.onMessageExternal.addListener((msg, sender, sendResponse) => {
  if (!msg || typeof msg !== "object") return;
  if (msg.type === "STEYDA_PING") {
    sendResponse({ ok: true, ext: "steyda-candy-order-helper", version: "0.2.0", suppliers: { ert: true, dagab: false, privab: false } });
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
