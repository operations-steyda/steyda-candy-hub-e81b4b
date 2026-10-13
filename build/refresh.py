#!/usr/bin/env python3
"""Daily marketing-data refresh for the STEYDA candy dashboard (Phase 1).

Pulls PPC + search-term data from the DataDoe REST API, rebuilds the ADS and
KEYWORDS data constants (with fresh TACOS denominators from the FBA 90-day
shipped-sales figures), then DECRYPTS the live index.html with SITE_PW, splices
in the fresh constants, and RE-ENCRYPTS it. The plaintext dashboard is never
written to disk in the repo, so nothing leaks.

Only ADS + KEYWORDS are touched. Inventory / replenishment / Joar / inbound /
sales are left exactly as the last manual "refresh the candy dashboard" run left
them (that run stays the source of truth for the inventory model — Phase 2 will
port it once its math is verified).

Env: DATADOE_API_KEY, SITE_PW.
"""
import os, re, json, csv, io, base64, time, datetime, subprocess, sys
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

API = "https://api.datadoe.com/api/v1"
SELLER = "602828c9-b688-463c-9ea5-3040f0e55dd1"
SRC_ADS = "08cdc77d3d"          # Ad Performance by Campaign & Date
SRC_TERMS = "e94e967198"        # Search Term Performance (Ads)
SRC_FBA = "44fc5ba0ce"          # FBA Inventory Health (has sales_shipped_last_90_days)
ITERS = 200000
HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.normpath(os.path.join(HERE, "..", "index.html"))

PNAME = {"B0F9PQQXCJ": "Squashies Mix", "B0DSW63TDX": "Bubs 0.5", "B0GK9FRNJJ": "Bubs 1.2",
         "B0H4ZVD6MN": "Bubs 2.0", "B0DKXMZN69": "Rail SS", "B0GPFK2RN7": "Tuttifrutti 0.5"}

KEY = os.environ["DATADOE_API_KEY"]
PW = os.environ["SITE_PW"]


# ---------- DataDoe REST ----------
_last_req = [0.0]  # throttle: DataDoe allows max 2 requests/sec per org


def _http(method, path, body=None):
    # Shell out to curl: the DataDoe edge rejects urllib (HTTP 406) but accepts curl.
    args = ["curl", "-sS", "-L", "--fail-with-body", "-X", method, API + path,
            "-H", "datadoe-api-key: " + KEY]
    inp = None
    if body is not None:
        args += ["-H", "Content-Type: application/json", "--data-binary", "@-"]
        inp = json.dumps(body).encode()
    for attempt in range(6):
        gap = time.time() - _last_req[0]
        if gap < 0.6:
            time.sleep(0.6 - gap)
        _last_req[0] = time.time()
        p = subprocess.run(args, input=inp, capture_output=True)
        out = (p.stdout or p.stderr).decode("utf-8", "replace")
        if p.returncode == 0:
            return p.stdout
        if ("RATE_LIMIT" in out or '"status":429' in out) and attempt < 5:
            time.sleep(1.5 + attempt)  # back off and retry
            continue
        raise RuntimeError("curl %s %s failed (%d): %s" % (method, path, p.returncode, out[:400]))
    raise RuntimeError("curl %s %s: rate-limit retries exhausted" % (method, path))


def export_csv(payload, timeout_s=240):
    """Create an export and return its CSV text once ready."""
    res = json.loads(_http("POST", "/exports", payload))
    eid = res.get("id") or res.get("exportId") or res.get("data", {}).get("exportId")
    if not eid:
        raise RuntimeError("no export id in response: " + json.dumps(res)[:300])
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        raw = _http("GET", "/exports/%s/raw" % eid)
        head = raw[:40].lstrip()
        if head.startswith(b'{"completed":false') or head.startswith(b'{"completed": false'):
            time.sleep(5); continue
        return raw.decode("utf-8", "replace")
    raise RuntimeError("export %s timed out" % eid)


def rows(payload):
    return list(csv.DictReader(io.StringIO(export_csv(payload))))


def num(v):
    try: return float(v)
    except Exception: return 0.0


def asin_of(name):
    m = re.search(r"B0[A-Z0-9]{8}", name or "")
    return m.group(0) if m else None


# ---------- window ----------
TODAY = datetime.date.today()
TO = TODAY.isoformat()
FROM = (TODAY - datetime.timedelta(days=90)).isoformat()


def win_label():
    def fmt(d):
        return datetime.date.fromisoformat(d).strftime("%b %-d") if os.name != "nt" else datetime.date.fromisoformat(d).strftime("%b ") + str(datetime.date.fromisoformat(d).day)
    return "%s – %s, %d" % (fmt(FROM), fmt(TO), TODAY.year)


WINDOW = win_label()


# ---------- FBA 90-day denominator ----------
def fba_r90():
    r = rows({"sellerOrVendorIds": [SELLER], "sourceId": SRC_FBA,
              "columns": ["date", "sku", "child_asin", "sales_shipped_last_90_days"],
              "from": (TODAY - datetime.timedelta(days=6)).isoformat(), "to": TO,
              "outputType": "CSV", "limit": 5000})
    if not r:
        return 0.0, {}
    latest = max(x.get("date", "") for x in r)
    by_asin = {}
    total = 0.0
    for x in r:
        if x.get("date") != latest:
            continue
        s = num(x.get("sales_shipped_last_90_days"))
        total += s
        a = x.get("child_asin")
        if a:
            by_asin[a] = by_asin.get(a, 0.0) + s
    return round(total, 2), by_asin


# ---------- ADS ----------
def build_ads(denom):
    r = rows({"sellerOrVendorIds": [SELLER], "sourceId": SRC_ADS,
              "columns": ["ad_campaign_name", "ad_campaign_type", "ad_campaign_status",
                          "spend", "sales", "impressions", "clicks", "orders", "units"],
              "groupBy": ["ad_campaign_name", "ad_campaign_type", "ad_campaign_status"],
              "aggregations": [{"column": "ad_spend", "aggregation": "sum", "alias": "spend"},
                               {"column": "ad_sales", "aggregation": "sum", "alias": "sales"},
                               {"column": "ad_impressions", "aggregation": "sum", "alias": "impressions"},
                               {"column": "ad_clicks", "aggregation": "sum", "alias": "clicks"},
                               {"column": "ad_orders", "aggregation": "sum", "alias": "orders"},
                               {"column": "ad_units_sold", "aggregation": "sum", "alias": "units"}],
              "from": FROM, "to": TO, "orderByColumn": "spend", "orderByDirection": "DESC",
              "outputType": "CSV", "limit": 5000})
    camps = []
    tot = dict(spend=0.0, sales=0.0, impr=0, clicks=0, orders=0, units=0)
    for x in r:
        c = dict(name=x["ad_campaign_name"], type=x["ad_campaign_type"], status=x["ad_campaign_status"],
                 spend=round(num(x["spend"]), 2), sales=round(num(x["sales"]), 2),
                 impr=int(num(x["impressions"])), clicks=int(num(x["clicks"])),
                 orders=int(num(x["orders"])), units=int(num(x["units"])))
        camps.append(c)
        tot["spend"] += c["spend"]; tot["sales"] += c["sales"]; tot["impr"] += c["impr"]
        tot["clicks"] += c["clicks"]; tot["orders"] += c["orders"]; tot["units"] += c["units"]
    tot["spend"] = round(tot["spend"], 2); tot["sales"] = round(tot["sales"], 2)
    tot["tacos"] = round(tot["spend"] / denom * 100, 1) if denom else 0
    tot["acos"] = round(tot["spend"] / tot["sales"] * 100, 1) if tot["sales"] else 0
    tot["roas"] = round(tot["sales"] / tot["spend"], 2) if tot["spend"] else 0
    tot["ctr"] = round(tot["clicks"] / tot["impr"] * 100, 2) if tot["impr"] else 0
    tot["cvr"] = round(tot["orders"] / tot["clicks"] * 100, 1) if tot["clicks"] else 0
    tot["cpc"] = round(tot["spend"] / tot["clicks"], 2) if tot["clicks"] else 0
    tot["total_sales"] = denom
    return {"total": tot, "campaigns": camps, "window": WINDOW}


# ---------- KEYWORDS ----------
def build_keywords():
    r = rows({"sellerOrVendorIds": [SELLER], "sourceId": SRC_TERMS,
              "columns": ["ad_search_term", "ad_campaign_name", "ad_match_type", "clicks", "orders", "sales", "spend"],
              "groupBy": ["ad_search_term", "ad_campaign_name", "ad_match_type"],
              "aggregations": [{"column": "ad_clicks", "aggregation": "sum", "alias": "clicks"},
                               {"column": "ad_orders", "aggregation": "sum", "alias": "orders"},
                               {"column": "ad_sales", "aggregation": "sum", "alias": "sales"},
                               {"column": "ad_spend", "aggregation": "sum", "alias": "spend"}],
              "having": {"combinator": "and", "rules": [{"field": "orders", "operator": ">", "value": "0", "not": False}]},
              "from": FROM, "to": TO, "orderByColumn": "sales", "orderByDirection": "DESC",
              "outputType": "CSV", "limit": 5000})
    agg = {}
    for x in r:
        a = asin_of(x["ad_campaign_name"])
        if not a:
            continue
        kw = (x["ad_search_term"] or "").strip().lower()
        if not kw or kw == "*" or re.match(r"^b0[a-z0-9]{8}$", kw):
            continue
        d = agg.setdefault((kw, a), {"c": 0, "o": 0, "s": 0.0})
        d["c"] += int(num(x["clicks"])); d["o"] += int(num(x["orders"])); d["s"] += num(x["sales"])
    tree = {}
    for (kw, a), d in agg.items():
        t = tree.setdefault(kw, {"o": 0, "s": 0.0, "asins": []})
        t["asins"].append({"a": a, "n": PNAME.get(a, a), "o": d["o"], "s": round(d["s"]), "c": d["c"]})
        t["o"] += d["o"]; t["s"] += d["s"]
    items = []
    for kw, t in tree.items():
        t["asins"].sort(key=lambda z: -z["s"])
        items.append({"kw": kw, "o": t["o"], "s": round(t["s"]), "n": len(t["asins"]), "asins": t["asins"]})
    items.sort(key=lambda z: -z["s"])
    return {"window": WINDOW,
            "source": "Sponsored Products & Brands search-term report — converting shopper queries",
            "items": items}


# ---------- NEGATIVES (broad-match search terms wasting spend) ----------
# Signatures that mark a term as a wrong product / category / brand / diet mismatch
# -> negate EXACT with high confidence. Everything else is flagged for review.
NEG_MISMATCH = ["japanese", "german", "english", " uk ", "uk candy", "foreign", "exotic",
                "weird", "unique", "cotton candy", "wax candy", "chocolate", "haribo",
                "unreal", "squish", "squashies", "sour patch", "vegan", "healthy",
                "sugar free", "sugar-free", "dye free", "dye-free", "keto", "organic",
                "nerds", "trolli", "albanese", "lifesavers", "jelly belly", "skittles", "starburst"]


def _neg_mismatch(term):
    t = " " + (term or "") + " "
    return any(m in t for m in NEG_MISMATCH)


def build_negatives():
    r = rows({"sellerOrVendorIds": [SELLER], "sourceId": SRC_TERMS,
              "columns": ["ad_search_term", "ad_campaign_name", "spend", "sales",
                          "clicks", "orders", "impressions"],
              "groupBy": ["ad_search_term", "ad_campaign_name"],
              "aggregations": [{"column": "ad_spend", "aggregation": "sum", "alias": "spend"},
                               {"column": "ad_sales", "aggregation": "sum", "alias": "sales"},
                               {"column": "ad_clicks", "aggregation": "sum", "alias": "clicks"},
                               {"column": "ad_orders", "aggregation": "sum", "alias": "orders"},
                               {"column": "ad_impressions", "aggregation": "sum", "alias": "impressions"}],
              "filters": {"combinator": "and",
                          "rules": [{"field": "ad_match_type", "operator": "=",
                                     "value": "BROAD", "not": False}]},
              "from": FROM, "to": TO, "orderByColumn": "spend", "orderByDirection": "DESC",
              "outputType": "CSV", "limit": 5000})
    out = []
    for x in r:
        term = (x["ad_search_term"] or "").strip().lower()
        camp = x["ad_campaign_name"]
        if not term or term == "*" or re.match(r"^b0[a-z0-9]{8}$", term):
            continue
        spend = num(x["spend"]); sales = num(x["sales"])
        clicks = int(num(x["clicks"])); orders = int(num(x["orders"]))
        acos = round(spend / sales * 100) if sales else None
        mism = _neg_mismatch(term)
        if orders == 0 and (clicks >= 4 or spend >= 3):
            cf = "high" if mism else "med"
            out.append({"t": term, "c": camp, "m": "negativeExact",
                        "r": "0 orders / mismatch" if cf == "high" else "0 orders, wasted spend",
                        "sp": round(spend, 2), "cl": clicks, "o": orders, "ac": None,
                        "cf": cf, "cat": "waster"})
        elif orders > 0 and sales > 0 and acos and acos > 60 and spend >= 2:
            cf = "high" if mism else "med"
            out.append({"t": term, "c": camp,
                        "m": "negativeExact" if mism else "negativePhrase",
                        "r": ("ACOS %d%% (mismatch)" % acos) if cf == "high" else ("ACOS %d%% unprofitable" % acos),
                        "sp": round(spend, 2), "cl": clicks, "o": orders, "ac": acos,
                        "cf": cf, "cat": "bleeder"})
    out.sort(key=lambda d: -d["sp"])
    waster = round(sum(d["sp"] for d in out if d["cat"] == "waster"), 2)
    bleeder = round(sum(d["sp"] for d in out if d["cat"] == "bleeder"), 2)
    rows_out = [{k: d[k] for k in ("t", "c", "m", "r", "sp", "cl", "o", "ac", "cf")} for d in out]
    return {"asof": TO, "window": WINDOW,
            "wasteSpend": round(waster + bleeder, 2),
            "wasterSpend": waster, "bleederSpend": bleeder,
            "nHigh": sum(1 for d in out if d["cf"] == "high"),
            "nReview": sum(1 for d in out if d["cf"] == "med"),
            "rows": rows_out}


# ---------- INBOUND (FBA inbound shipments) ----------
SRC_INBOUND = "8bc6f4bd09"
STATUS_ORDER = ["IN_TRANSIT", "RECEIVING", "READY_TO_SHIP"]


def build_inbound(name_map):
    r = rows({"sellerOrVendorIds": [SELLER], "sourceId": SRC_INBOUND,
              "columns": ["seller_sku", "child_asin", "quantity",
                          "fba_inbound_shipment_status", "destination_warehouse_id"],
              "outputType": "CSV", "limit": 5000})
    by = {}
    for x in r:
        st = x.get("fba_inbound_shipment_status") or ""
        d = by.setdefault(st, {"status": st, "lines": 0, "units": 0, "skus": set(), "items": []})
        q = int(num(x.get("quantity")))
        d["lines"] += 1; d["units"] += q; d["skus"].add(x.get("seller_sku"))
        d["items"].append({"sku": x.get("seller_sku"), "asin": x.get("child_asin"),
                           "qty": q, "fc": x.get("destination_warehouse_id") or ""})

    def rollup(items):
        agg = {}
        for it in items:
            a = agg.setdefault(it["sku"], {"sku": it["sku"], "asin": it["asin"], "units": 0, "fcs": set()})
            a["units"] += it["qty"]; a["fcs"].add(it["fc"])
        out = [{"sku": a["sku"], "name": name_map.get(a["sku"], a["sku"]), "asin": a["asin"],
                "units": a["units"], "fc": ",".join(sorted(y for y in a["fcs"] if y))} for a in agg.values()]
        out.sort(key=lambda z: -z["units"]); return out

    status = [{"status": k, "lines": v["lines"], "units": v["units"], "skus": len(v["skus"])}
              for k, v in by.items()]
    groups = []
    for st in STATUS_ORDER + [k for k in by if k not in STATUS_ORDER]:
        if st in by:
            v = by[st]
            groups.append({"status": st, "lines": v["lines"], "units": v["units"], "skus": rollup(v["items"])})
    return {"status": status, "groups": groups,
            "total_units": sum(s["units"] for s in status),
            "total_lines": sum(s["lines"] for s in status)}


# ---------- crypto (matches encrypt_site.py) ----------
def decrypt(index_html):
    def grab(name):
        m = re.search(name + r'\s*:\s*"([^"]*)"', index_html)
        return m.group(1)
    salt = base64.b64decode(grab("salt")); iv = base64.b64decode(grab("iv")); ct = base64.b64decode(grab("ct"))
    it = int(re.search(r"iters\s*:\s*(\d+)", index_html).group(1))
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=it)
    key = kdf.derive(PW.encode("utf-8"))
    return AESGCM(key).decrypt(iv, ct, None).decode("utf-8")


DECRYPTOR_TPL = r"""<!DOCTYPE html>
<html lang="en" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>STEYDA — Protected</title>
<!-- __STEYDA_ENC__ : content is AES-256-GCM encrypted; the password is never stored. -->
<style>
  :root{--bg:#eef1f4;--card:#fff;--ink:#182430;--ink2:#5b6b7a;--line:#e4e8ee;--accent:#2790CE;--accent2:#E0327E;--crit:#D6475A}
  @media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#0e1418;--card:#161e24;--ink:#eef3f6;--ink2:#a7b6c2;--line:#26313a;--accent:#4EB4E6;--accent2:#f06ba6}}
  *{box-sizing:border-box}html,body{height:100%}
  body{margin:0;font:15px/1.5 system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--ink);display:grid;place-items:center}
  .card{width:min(380px,calc(100vw - 32px));background:var(--card);border:1px solid var(--line);border-radius:16px;padding:28px 26px;box-shadow:0 12px 40px rgba(24,36,48,.14);text-align:center}
  .logo{width:44px;height:44px;border-radius:12px;margin:0 auto 14px;background:linear-gradient(135deg,var(--accent),var(--accent2))}
  h1{font-size:18px;margin:0 0 4px}p{color:var(--ink2);font-size:13px;margin:0 0 18px}
  input{width:100%;padding:12px 14px;border:1px solid var(--line);border-radius:10px;font:inherit;background:var(--bg);color:var(--ink);outline:none}
  input:focus{border-color:var(--accent)}
  button{width:100%;margin-top:12px;padding:12px;border:0;border-radius:10px;cursor:pointer;color:#fff;font:600 15px system-ui;background:linear-gradient(135deg,var(--accent),var(--accent2))}
  button:disabled{opacity:.6;cursor:default}
  .err{color:var(--crit);font-size:13px;margin-top:12px;min-height:18px}
  #app{position:fixed;inset:0;width:100%;height:100%;border:0;display:none;background:var(--bg)}
</style>
</head>
<body>
<div class="card" id="gate">
  <div class="logo"></div>
  <h1>STEYDA Dashboard</h1>
  <p>Protected. Enter the password to continue.</p>
  <form id="f">
    <input id="pw" type="password" autocomplete="current-password" placeholder="Password" autofocus>
    <button id="go" type="submit">Unlock</button>
  </form>
  <div class="err" id="err"></div>
</div>
<iframe id="app" title="STEYDA Dashboard"></iframe>
<script>
const ENC = { salt:"__SALT__", iv:"__IV__", ct:"__CT__", iters:__ITERS__ };
const b64d = s => Uint8Array.from(atob(s), c => c.charCodeAt(0));
async function unlock(pw){
  const enc = new TextEncoder();
  const km = await crypto.subtle.importKey("raw", enc.encode(pw), "PBKDF2", false, ["deriveKey"]);
  const key = await crypto.subtle.deriveKey(
    { name:"PBKDF2", salt:b64d(ENC.salt), iterations:ENC.iters, hash:"SHA-256" },
    km, { name:"AES-GCM", length:256 }, false, ["decrypt"]);
  const pt = await crypto.subtle.decrypt({ name:"AES-GCM", iv:b64d(ENC.iv) }, key, b64d(ENC.ct));
  return new TextDecoder().decode(pt);
}
const f=document.getElementById("f"), err=document.getElementById("err"), go=document.getElementById("go");
f.addEventListener("submit", async e => {
  e.preventDefault(); err.textContent=""; go.disabled=true; go.textContent="Unlocking…";
  try{
    const html = await unlock(document.getElementById("pw").value);
    const app=document.getElementById("app");
    app.srcdoc = html; app.style.display="block";
    document.getElementById("gate").style.display="none";
    document.title="STEYDA — Candy Operating Dashboard";
  }catch(_){
    err.textContent="Wrong password."; go.disabled=false; go.textContent="Unlock";
    document.getElementById("pw").select();
  }
});
</script>
</body>
</html>
"""


def encrypt(plaintext):
    salt = os.urandom(16); iv = os.urandom(12)
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=ITERS)
    key = kdf.derive(PW.encode("utf-8"))
    ct = AESGCM(key).encrypt(iv, plaintext.encode("utf-8"), None)
    b = lambda x: base64.b64encode(x).decode("ascii")
    return (DECRYPTOR_TPL.replace("__SALT__", b(salt)).replace("__IV__", b(iv))
            .replace("__CT__", b(ct)).replace("__ITERS__", str(ITERS)))


def splice_const(html, name, obj):
    js = json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")
    repl = "const " + name + " = " + js + ";"
    pat = re.compile(r"const " + name + r" = \{.*?\};", re.S)
    new, n = pat.subn(lambda _m: repl, html, count=1)  # func repl: no backslash escaping
    if n != 1:
        raise RuntimeError("could not splice const %s (matched %d)" % (name, n))
    return new


def splice_line(html, name, value_js):
    """Replace a whole single-line `const NAME = ...;` with a prebuilt JS value."""
    repl = "const " + name + " = " + value_js + ";"
    pat = re.compile(r"^const " + name + r" = .*$", re.M)
    new, n = pat.subn(lambda _m: repl, html, count=1)  # func repl: no backslash escaping
    if n != 1:
        raise RuntimeError("could not splice line const %s (matched %d)" % (name, n))
    return new


def payload_skus(html):
    m = re.search(r"const PAYLOAD = (\{.*?\});\s*\nconst ", html, re.S)
    if not m:
        m = re.search(r"const PAYLOAD = (\{.*\})", html)
    p = json.loads(m.group(1).replace("<\\/", "</"))
    return p, [r["sku"] for r in p["DATA"]["inv"]]


def apply_inventory(plain0, consts_path):
    """Guarded splice of the inventory/warehouse consts from the private build.
    Returns (new_plain, note). On any guard failure, returns (plain0, reason) so
    the daily job still publishes the PPC refresh but never a suspect inventory plan.
    """
    try:
        with io.open(consts_path, encoding="utf-8") as fh:
            consts = json.load(fh)
    except Exception as e:
        return plain0, "no inventory consts (%s) — kept current inventory" % e
    try:
        curP, cur_skus = payload_skus(plain0)
        freshP = json.loads(consts["PAYLOAD"].replace("<\\/", "</"))
        fresh = {r["sku"]: r for r in freshP["DATA"]["inv"]}
        fresh_skus = list(fresh)
        # GUARD 1: SKU set must not change (DataDoe status lag can silently drop sellers)
        dropped = sorted(set(cur_skus) - set(fresh_skus))
        added = sorted(set(fresh_skus) - set(cur_skus))
        if dropped or added:
            return plain0, ("GUARD: SKU set changed (dropped %s, added %s) — inventory left "
                            "unchanged; run a manual refresh to review." % (dropped, added))
        # GUARD 2: velocities must not be globally zero (thin/failed data pull)
        if not any((r["m"]["blend"].get("vel_day") or 0) > 0 for r in fresh.values()):
            return plain0, "GUARD: all velocities zero — thin data; inventory left unchanged."
        # GUARD 3: no single SKU's order_qty may explode vs current (bad-data swing)
        cur = {r["sku"]: r for r in curP["DATA"]["inv"]}
        for sku, fr in fresh.items():
            fo = fr["m"]["blend"].get("order_qty") or 0
            co = cur.get(sku, {}).get("m", {}).get("blend", {}).get("order_qty") or 0
            if co and fo > max(co * 6, co + 5000):
                return plain0, ("GUARD: %s order_qty jumped %s->%s — inventory left unchanged." % (sku, co, fo))
    except Exception as e:
        return plain0, "GUARD: inventory validation error (%s) — inventory left unchanged." % e
    # guards passed: splice PAYLOAD + warehouse consts + stock
    plain = plain0
    for name in ("PAYLOAD", "WH", "SHIP", "JOARHTML"):
        if name in consts and ("const %s = " % name) in plain:
            plain = splice_line(plain, name, consts[name])
    if "stock" in consts:
        plain = re.sub(r"(, stock: )\{[^{}]*\}(, stockDate:)",
                       lambda m: m.group(1) + consts["stock"] + m.group(2), plain, count=1)
    if "stockDate" in consts:
        plain = re.sub(r'(stockDate:\s*")[^"]*(")',
                       lambda m: m.group(1) + consts["stockDate"] + m.group(2), plain, count=1)
    return plain, "inventory refreshed (%d SKUs, asof %s)" % (len(fresh_skus),
                                                              freshP["DATA"]["meta"].get("asof"))


def main():
    print("Window:", WINDOW)
    denom, _by = fba_r90()
    print("FBA 90-day denominator:", denom)
    ads = build_ads(denom)
    print("ADS: %d campaigns, spend %.2f, sales %.2f, TACOS %s%%"
          % (len(ads["campaigns"]), ads["total"]["spend"], ads["total"]["sales"], ads["total"]["tacos"]))
    kw = build_keywords()
    print("KEYWORDS:", len(kw["items"]), "converting keywords")
    neg = build_negatives()
    print("NEGATIVES: %d candidates, $%.2f at stake (%d high-confidence)"
          % (len(neg["rows"]), neg["wasteSpend"], neg["nHigh"]))

    with io.open(INDEX, encoding="utf-8") as fh:
        enc_html = fh.read()
    if "__STEYDA_ENC__" not in enc_html:
        sys.exit("index.html is not the encrypted page; aborting.")
    plain0 = decrypt(enc_html)
    for name in ("const ADS", "const KEYWORDS"):
        if plain0.count(name + " = ") != 1:
            sys.exit("expected exactly one %s in plaintext" % name)
    plain = splice_const(plain0, "ADS", ads)
    plain = splice_const(plain, "KEYWORDS", kw)
    if plain.count("const NEGATIVES = ") == 1:
        plain = splice_const(plain, "NEGATIVES", neg)
    else:
        print("NEGATIVES const not found in page; skipping splice.")
    consts_path = os.environ.get("INVENTORY_CONSTS", os.path.join(HERE, "_private", "out", "consts.json"))
    plain, invnote = apply_inventory(plain, consts_path)
    print("Inventory:", invnote)
    # Inbound shipments (DataDoe) — product names come from the current PAYLOAD
    try:
        _pm = json.loads(re.search(r"const PAYLOAD = (\{.*?\});", plain, re.S).group(1).replace("<\\/", "</"))
        _nm = {rr["sku"]: (rr.get("shortname") or rr["sku"]) for rr in _pm["DATA"]["inv"]}
    except Exception:
        _nm = {}
    try:
        inb = build_inbound(_nm)
        if inb["status"] and plain.count("const INBOUND = ") == 1:
            plain = splice_const(plain, "INBOUND", inb)
            print("Inbound: %d units, %d lines" % (inb["total_units"], inb["total_lines"]))
    except Exception as e:
        print("Inbound refresh skipped:", e)
    if plain == plain0:
        print("No data change; leaving index.html unchanged.")
        return
    out = encrypt(plain)
    with io.open(INDEX, "w", encoding="utf-8", newline="") as fh:
        fh.write(out)
    print("Re-encrypted -> index.html (%d bytes)" % len(out))


if __name__ == "__main__":
    main()
