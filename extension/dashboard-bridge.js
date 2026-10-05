/* Dashboard side — how Billy hands the order to the extension.
 * I (Claude) will wire this into Billy once you install the extension and give me
 * its Extension ID. Shown here so you can see the whole picture.
 *
 * chrome.runtime.sendMessage(EXT_ID, msg, cb) works from a normal web page ONLY
 * when the extension's manifest lists this page's origin under
 * "externally_connectable" (it does: operations-steyda.github.io).
 */
const STEYDA_EXT_ID = "PASTE_EXTENSION_ID_HERE"; // from chrome://extensions after loading unpacked

function extAvailable() {
  return new Promise((resolve) => {
    try {
      if (!chrome || !chrome.runtime || !chrome.runtime.sendMessage) return resolve(false);
      chrome.runtime.sendMessage(STEYDA_EXT_ID, { type: "STEYDA_PING" }, (resp) => {
        resolve(!chrome.runtime.lastError && resp && resp.ok);
      });
    } catch (e) { resolve(false); }
  });
}

// order = { ert:[{sku,qty,name}], dagab:[...], privab:[...] }  (from CandyPlanner.order())
function sendOrderToExtension(order) {
  return new Promise((resolve, reject) => {
    chrome.runtime.sendMessage(STEYDA_EXT_ID, { type: "STEYDA_ORDER", order }, (resp) => {
      if (chrome.runtime.lastError) return reject(chrome.runtime.lastError.message);
      resolve(resp);
    });
  });
}
