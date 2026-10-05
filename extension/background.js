/* STEYDA Candy Order Helper — background service worker (MV3)
 *
 * Receives the shopping list from the dashboard (operations-steyda.github.io) and,
 * for each supplier, searches each SKU and adds it to that site's cart.
 * ADD-TO-CART ONLY — it never checks out, pays, or enters addresses.
 *
 * Billy (the dashboard) hands over an order shaped like:
 *   { type:"STEYDA_ORDER", order:{
 *       ert:    [{sku:"ARO66906",  qty:5, name:"Aroma Pussmun"}],
 *       dagab:  [{sku:"101423296", qty:53, name:"BUBS Smultron"}],
 *       privab: [{sku:"47-16151",  qty:3, name:"..."}]
 *   }}
 * The user must already be LOGGED IN to each supplier in this browser.
 *
 * ⚠ The per-site steps below are STUBS. Each supplier's search URL and the
 * selectors for the quantity box / add-to-cart button must be filled in by
 * inspecting the real site (see README.md → "Filling the selectors").
 */

// ---- per-supplier config -------------------------------------------------
// searchUrl(sku): a URL that lands on the product (or its search result).
// Fill these from the real sites.
const SUPPLIERS = {
  ert: {
    name: "ERT (ertgodis.se)",
    home: "https://www.ertgodis.se/",
    searchUrl: (sku) => "https://www.ertgodis.se/?s=" + encodeURIComponent(sku), // TODO verify
    addItem: ertAddItem,
  },
  dagab: {
    name: "Dagab (dagab.se)",
    home: "https://www.dagab.se/",
    searchUrl: (sku) => "https://www.dagab.se/search?q=" + encodeURIComponent(sku), // TODO verify
    addItem: dagabAddItem,
  },
  privab: {
    name: "Privab (privab.se)",
    home: "https://privab.se/",
    searchUrl: (sku) => "https://privab.se/sok?q=" + encodeURIComponent(sku), // TODO verify
    addItem: privabAddItem,
  },
};

// ---- injected per-site "add one item" functions (run IN the page) ---------
// Each receives {sku, qty, name} and returns {sku, ok, note}. Replace the TODO
// selectors with the real ones (right-click the field/button → Inspect).
function ertAddItem(item) {
  // TODO(ERT): on the search/product page, (1) open the first matching product,
  // (2) set the quantity field, (3) click add-to-cart.
  // const link = document.querySelector("SELECTOR_FOR_FIRST_RESULT"); if (link) link.click();
  // const q = document.querySelector("SELECTOR_FOR_QTY_INPUT");
  // if (q) { q.value = item.qty; q.dispatchEvent(new Event("input",{bubbles:true})); q.dispatchEvent(new Event("change",{bubbles:true})); }
  // const btn = document.querySelector("SELECTOR_FOR_ADD_TO_CART");
  // if (btn) { btn.click(); return { sku:item.sku, ok:true }; }
  return { sku: item.sku, ok: false, note: "TODO: fill ERT selectors in background.js" };
}
function dagabAddItem(item) {
  return { sku: item.sku, ok: false, note: "TODO: fill Dagab selectors in background.js" };
}
function privabAddItem(item) {
  return { sku: item.sku, ok: false, note: "TODO: fill Privab selectors in background.js" };
}

// ---- orchestration -------------------------------------------------------
function navigate(tabId, url) {
  return new Promise((resolve) => {
    chrome.tabs.update(tabId, { url });
    const listener = (id, info) => {
      if (id === tabId && info.status === "complete") {
        chrome.tabs.onUpdated.removeListener(listener);
        setTimeout(resolve, 900); // let the page settle / scripts run
      }
    };
    chrome.tabs.onUpdated.addListener(listener);
  });
}

async function processSupplier(key, items) {
  const cfg = SUPPLIERS[key];
  if (!cfg) return [{ error: "unknown supplier: " + key }];
  const tab = await chrome.tabs.create({ url: cfg.home, active: true });
  // give the home page a moment (confirms the user is logged in / session alive)
  await new Promise((r) => setTimeout(r, 1200));
  const results = [];
  for (const it of items) {
    try {
      await navigate(tab.id, cfg.searchUrl(it.sku));
      const [res] = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: cfg.addItem,
        args: [it],
      });
      results.push(res && res.result ? res.result : { sku: it.sku, ok: false, note: "no result" });
    } catch (e) {
      results.push({ sku: it.sku, ok: false, note: String(e) });
    }
  }
  return results;
}

// ---- message bridge from the dashboard -----------------------------------
chrome.runtime.onMessageExternal.addListener((msg, sender, sendResponse) => {
  if (!msg || typeof msg !== "object") return;
  if (msg.type === "STEYDA_PING") {
    sendResponse({ ok: true, ext: "steyda-candy-order-helper", version: "0.1.0" });
    return;
  }
  if (msg.type === "STEYDA_ORDER") {
    (async () => {
      const order = msg.order || {};
      const report = {};
      for (const key of Object.keys(order)) {
        if (!Array.isArray(order[key]) || !order[key].length) continue;
        report[key] = await processSupplier(key, order[key]);
      }
      sendResponse({ ok: true, report });
    })();
    return true; // keep the channel open for the async work
  }
});
