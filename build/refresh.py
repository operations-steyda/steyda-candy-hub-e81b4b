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
SRC_PROFIT = "57a0cb319c"       # Profit by SKU & Date (sales, COGS, fees, ad spend, profit, sessions per ASIN/day)
SRC_ADASIN = "d0017e92fb"       # Ad Performance by ASIN & Date (clicks/orders per ASIN/day)
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
# Irrelevant ROOT phrases: one negative-PHRASE blocks the whole family of queries
# that contain the root. Brand terms (bubs, squashies) are NEVER listed here.
NEG_ROOTS = ["squish", "haribo", "japanese", "german", "english candy", "foreign", "exotic",
             "weird", "cotton candy", "wax candy", "chocolate", "unreal", "sour patch",
             "vegan", "healthy", "sugar free", "sugar-free", "dye free", "dye-free", "keto",
             "organic", "nerds", "trolli", "albanese", "lifesavers", "jelly belly",
             "skittles", "starburst", "freeze dried"]


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
    # per-term (across campaigns) for phrase roots; per (term,campaign) for exact list
    byterm = {}
    bytc = {}
    scanned = 0
    for x in r:
        term = (x["ad_search_term"] or "").strip().lower()
        if not term or term == "*" or re.match(r"^b0[a-z0-9]{8}$", term):
            continue
        scanned += 1
        sp = num(x["spend"]); cl = int(num(x["clicks"])); od = int(num(x["orders"]))
        a = byterm.setdefault(term, [0.0, 0, 0]); a[0] += sp; a[1] += cl; a[2] += od
        b = bytc.setdefault((term, x["ad_campaign_name"]), [0.0, 0, 0]); b[0] += sp; b[1] += cl; b[2] += od

    phrase = []
    covered = set()
    for root in NEG_ROOTS:
        sp = cl = od = 0.0; terms = []
        for t, v in byterm.items():
            if root in t:
                sp += v[0]; cl += v[1]; od += v[2]; terms.append(t)
        if sp >= 2 and terms:
            phrase.append({"root": root, "sp": round(sp, 2), "terms": len(terms),
                           "cl": int(cl), "o": int(od)})
            covered.update(terms)
    phrase.sort(key=lambda d: -d["sp"])

    exact = []
    for (t, c), v in bytc.items():
        if t in covered:
            continue
        sp, cl, od = v
        if od == 0 and (cl >= 4 or sp >= 3):
            exact.append({"t": t, "c": c, "sp": round(sp, 2), "cl": cl, "o": od, "ac": None})
    exact.sort(key=lambda d: -d["sp"])

    ps = round(sum(d["sp"] for d in phrase), 2)
    es = round(sum(d["sp"] for d in exact), 2)
    return {"asof": TO, "window": WINDOW,
            "source": "DataDoe · Search Term Performance (Ads) · "
                      "amazon_ads_search_terms_by_campaign_by_date (%s) · broad-match only" % SRC_TERMS,
            "scanned": scanned, "wasteSpend": round(ps + es, 2),
            "phraseSpend": ps, "exactSpend": es,
            "phrase": phrase, "exact": exact}


# ---------- INBOUND (FBA inbound shipments) ----------
# Inbound SHIPMENT TRACKING view, from the FBA Inbound Shipments feed (per-shipment detail:
# id, status, destination, full quantity, and the SKU lines). CLOSED/cancelled plans are
# dropped so it mirrors Seller Central Manage Shipments' open shipments.
#
# STATUS MATCH (2026-10-19): the DataDoe inbound feed has NO received/located column and its
# status lags Seller Central (it can still say "Ready to ship"/"In transit" after SC flips a
# shipment to "Receiving"). To make this section read the SAME as SC Manage Shipments, we keep
# each shipment at its FULL quantity (so Units match SC) but relabel status to "Receiving" once
# any of its units are located — detected by allocating Amazon's fresh per-SKU still-inbound
# (PAYLOAD inv.incoming) to shipments newest-first (see _status_reconcile). The plan's separate
# Inbound column carries the still-inbound number; the subtitle notes that located units also
# count in Available. All automatic on every build — no manual "Refresh now" needed.
SRC_INBOUND = "8bc6f4bd09"
INBOUND_EXCLUDE = {"CLOSED", "CANCELLED", "DELETED", "ERROR"}
INBOUND_STALE_DAYS = 30   # Amazon hasn't re-reported it in this long -> treat as closed/received
INBOUND_LABEL = {"WORKING": "Ready to ship", "READY_TO_SHIP": "Ready to ship", "SHIPPED": "Shipped",
                 "IN_TRANSIT": "In transit", "RECEIVING": "Receiving", "CHECKED_IN": "Receiving",
                 "DELIVERED": "Receiving"}
INBOUND_ORDER = {"WORKING": 0, "READY_TO_SHIP": 0, "SHIPPED": 1, "IN_TRANSIT": 2,
                 "RECEIVING": 3, "CHECKED_IN": 3, "DELIVERED": 3}


def _inbound_created(name):
    """Shipment creation date (YYYY-MM-DD) parsed from the 'FBA STA (MM/DD/YYYY HH:MM)...'
    name, used only to order shipments newest-first for the reconcile allocation."""
    m = re.search(r"\((\d{2})/(\d{2})/(\d{4})", name or "")
    return "%s-%s-%s" % (m.group(3), m.group(1), m.group(2)) if m else ""


def _status_reconcile(shipments, real_inbound):
    """Make the status match Seller Central's Manage Shipments WITHOUT changing quantities.
    SC shows a shipment as "Receiving" once its units start being located at the FC, even while
    DataDoe's feed still carries a stale pre-arrival status (Working/Ready to ship/Shipped/In
    transit). We detect "located" by allocating Amazon's fresh still-inbound (real_inbound:
    {sku: inbound_quantity}) to shipments newest-first; any shipment with located>0 (i.e. some
    of its units are already in stock) is relabelled "Receiving" to mirror SC. A shipment whose
    units are entirely still-inbound keeps its pre-arrival status. Quantities stay FULL so the
    section's Units match SC's Units column; the plan's Inbound column carries the still-inbound.
    Adds per-shipment `open` (still inbound) and `loc` (already located) for reference."""
    remaining = {k: int(v or 0) for k, v in (real_inbound or {}).items()}
    for sh in sorted(shipments, key=lambda s: s.get("_created", "") or s.get("last_seen", ""),
                     reverse=True):
        still = 0
        for ln in sh["skus"]:
            rem = remaining.get(ln["sku"], 0)
            take = min(int(ln["units"]), rem)
            remaining[ln["sku"]] = rem - take
            still += take
        sh["open"] = still
        sh["loc"] = max(0, sh["units"] - still)
        if sh["loc"] > 0 and sh["status"] not in INBOUND_EXCLUDE:
            sh["status"] = "RECEIVING"
            sh["sl"] = INBOUND_LABEL["RECEIVING"]


def build_inbound(name_map, real_inbound=None):
    r = rows({"sellerOrVendorIds": [SELLER], "sourceId": SRC_INBOUND,
              "columns": ["fba_inbound_shipment_id", "shipment_name", "fba_inbound_shipment_status",
                          "destination_warehouse_id", "seller_sku", "child_asin", "quantity", "last_seen_at"],
              "outputType": "CSV", "limit": 5000})
    ships = {}
    for x in r:
        st = (x.get("fba_inbound_shipment_status") or "").upper()
        if st in INBOUND_EXCLUDE:
            continue
        sid = x.get("fba_inbound_shipment_id") or ""
        _nm0 = x.get("shipment_name") or sid
        sh = ships.setdefault(sid, {"id": sid, "name": _nm0, "status": st,
                                    "sl": INBOUND_LABEL.get(st, st.title()),
                                    "fc": x.get("destination_warehouse_id") or "", "units": 0, "skus": [],
                                    "last_seen": "", "_created": _inbound_created(_nm0)})
        q = int(num(x.get("quantity")))
        sh["units"] += q
        sku = x.get("seller_sku")
        sh["skus"].append({"sku": sku, "name": name_map.get(sku, sku),
                           "asin": x.get("child_asin"), "units": q})
        ls = (x.get("last_seen_at") or "")[:10]   # YYYY-MM-DD
        if ls > sh["last_seen"]:
            sh["last_seen"] = ls
    # Amazon drops a shipment from the active-inbound feed once it's closed/received, but
    # DataDoe keeps its last-known status (e.g. still "RECEIVING"). So a shipment Amazon
    # hasn't re-reported in STALE_DAYS is treated as closed — this is what makes the view
    # tally with Seller Central's open Manage-Shipments set. (2026-10-15)
    def _fresh(sh):
        ls = sh.get("last_seen", "")
        if len(ls) < 10:
            return True
        try:
            return (TODAY - datetime.date.fromisoformat(ls)).days <= INBOUND_STALE_DAYS
        except Exception:
            return True
    shipments = [s for s in ships.values() if _fresh(s)]
    # Relabel status to match Seller Central Manage Shipments (received-but-not-"Closed"
    # shipments show as "Receiving"), WITHOUT reducing quantities. Skipped only if real_inbound
    # is unavailable. The plan's Inbound column (PAYLOAD incoming) remains the still-inbound.
    if real_inbound is not None:
        _status_reconcile(shipments, real_inbound)
    for sh in shipments:
        sh.pop("last_seen", None)   # internal only
        sh.pop("_created", None)    # internal only
        sh["skus"].sort(key=lambda z: -z["units"])
    shipments.sort(key=lambda s: (INBOUND_ORDER.get(s["status"], 9), -s["units"]))
    bys = {}
    for sh in shipments:
        b = bys.setdefault(sh["sl"], {"status": sh["sl"], "shipments": 0, "units": 0})
        b["shipments"] += 1; b["units"] += sh["units"]
    return {"shipments": shipments, "by_status": list(bys.values()),
            "total_units": sum(s["units"] for s in shipments),
            "total_shipments": len(shipments),
            "total_lines": sum(len(s["skus"]) for s in shipments)}


# ---------- RESERVED (units Amazon counts in FBA total but aren't sellable) ----------
def build_reserved():
    """Per-SKU reserved(customer+processing) / researching / unfulfillable — the
    bridge from Pool (sellable+incoming) to the Seller Central FBA headline."""
    r = rows({"sellerOrVendorIds": [SELLER], "sourceId": SRC_FBA,
              "columns": ["date", "sku", "reserved_customer_order", "reserved_fc_processing",
                          "afn_researching_quantity", "unfulfillable_quantity"],
              "from": (TODAY - datetime.timedelta(days=6)).isoformat(), "to": TO,
              "outputType": "CSV", "limit": 5000})
    if not r:
        return {}
    latest = max(x.get("date", "") for x in r)
    out = {}
    for x in r:
        if x.get("date") != latest:
            continue
        sku = x.get("sku")
        res = int(num(x.get("reserved_customer_order"))) + int(num(x.get("reserved_fc_processing")))
        rsh = int(num(x.get("afn_researching_quantity")))
        unf = int(num(x.get("unfulfillable_quantity")))
        if sku and (res or rsh or unf):
            out[sku] = {"res": res, "rsh": rsh, "unf": unf}
    return out


# ---------- PPC COMMAND CENTRE (profit-aware, all DataDoe) ----------
def build_ppcx():
    """Fetch the 4 DataDoe tables the PPC command centre needs and hand them to ppcx.build().
    60 days of daily history -> two 30-day windows + a PPC-conversion baseline; search terms for
    the last 30 days (keyword opportunities). Windows end 2 days before the newest data day
    (Amazon's still-settling tail) -- ppcx.py handles that."""
    import ppcx
    frm = (TODAY - datetime.timedelta(days=62)).isoformat()
    prof = rows({"sellerOrVendorIds": [SELLER], "sourceId": SRC_PROFIT,
                 "columns": ["date", "child_asin", "sales_s", "adspend_s", "adsales_s", "profit_s",
                             "orders_s", "units_s", "cogs_s", "sessions_s"],
                 "groupBy": ["date", "child_asin"],
                 "aggregations": [{"column": "total_sales", "aggregation": "sum", "alias": "sales_s"},
                                  {"column": "ad_spend", "aggregation": "sum", "alias": "adspend_s"},
                                  {"column": "ad_sales", "aggregation": "sum", "alias": "adsales_s"},
                                  {"column": "profit", "aggregation": "sum", "alias": "profit_s"},
                                  {"column": "total_orders", "aggregation": "sum", "alias": "orders_s"},
                                  {"column": "total_units_sold", "aggregation": "sum", "alias": "units_s"},
                                  {"column": "cogs_total", "aggregation": "sum", "alias": "cogs_s"},
                                  {"column": "total_sessions", "aggregation": "sum", "alias": "sessions_s"}],
                 "from": frm, "to": TO, "outputType": "CSV", "limit": 5000})
    adasin = rows({"sellerOrVendorIds": [SELLER], "sourceId": SRC_ADASIN,
                   "columns": ["date", "child_asin", "spend", "sales", "orders", "clicks", "impr"],
                   "groupBy": ["date", "child_asin"],
                   "aggregations": [{"column": "ad_spend", "aggregation": "sum", "alias": "spend"},
                                    {"column": "ad_sales", "aggregation": "sum", "alias": "sales"},
                                    {"column": "ad_orders", "aggregation": "sum", "alias": "orders"},
                                    {"column": "ad_clicks", "aggregation": "sum", "alias": "clicks"},
                                    {"column": "ad_impressions", "aggregation": "sum", "alias": "impr"}],
                   "from": frm, "to": TO, "outputType": "CSV", "limit": 5000})
    camp = rows({"sellerOrVendorIds": [SELLER], "sourceId": SRC_ADS,
                 "columns": ["date", "spend", "sales", "orders", "clicks", "impr"],
                 "groupBy": ["date"],
                 "aggregations": [{"column": "ad_spend", "aggregation": "sum", "alias": "spend"},
                                  {"column": "ad_sales", "aggregation": "sum", "alias": "sales"},
                                  {"column": "ad_orders", "aggregation": "sum", "alias": "orders"},
                                  {"column": "ad_clicks", "aggregation": "sum", "alias": "clicks"},
                                  {"column": "ad_impressions", "aggregation": "sum", "alias": "impr"}],
                 "from": frm, "to": TO, "outputType": "CSV", "limit": 5000})
    terms = rows({"sellerOrVendorIds": [SELLER], "sourceId": SRC_TERMS,
                  "columns": ["ad_search_term", "ad_match_type", "ad_campaign_name",
                              "clicks", "orders", "sales", "spend"],
                  "groupBy": ["ad_search_term", "ad_match_type", "ad_campaign_name"],
                  "aggregations": [{"column": "ad_clicks", "aggregation": "sum", "alias": "clicks"},
                                   {"column": "ad_orders", "aggregation": "sum", "alias": "orders"},
                                   {"column": "ad_sales", "aggregation": "sum", "alias": "sales"},
                                   {"column": "ad_spend", "aggregation": "sum", "alias": "spend"}],
                  "from": (TODAY - datetime.timedelta(days=32)).isoformat(), "to": TO,
                  "outputType": "CSV", "limit": 5000})
    return ppcx.build(prof, adasin, terms, camp)


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
    for name in ("PAYLOAD", "WH", "SHIP", "JOARHTML", "PLANNED"):
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
    print("NEGATIVES: %d phrase ($%.2f) + %d exact ($%.2f), $%.2f at stake"
          % (len(neg["phrase"]), neg["phraseSpend"], len(neg["exact"]),
             neg["exactSpend"], neg["wasteSpend"]))

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
        # Amazon's fresh per-SKU still-inbound (the plan's "Inbound" column) — authoritative
        # truth we reconcile the shipment view against so it matches the plan + Seller Central.
        _ri = {rr["sku"]: int(rr.get("incoming") or 0) for rr in _pm["DATA"]["inv"]}
    except Exception:
        _nm = {}; _ri = None
    try:
        inb = build_inbound(_nm, _ri)
        if (inb["shipments"] or _ri is not None) and plain.count("const INBOUND = ") == 1:
            plain = splice_const(plain, "INBOUND", inb)
            print("Inbound: %d shipments, %d units, %d SKU lines"
                  % (inb["total_shipments"], inb["total_units"], inb["total_lines"]))
    except Exception as e:
        print("Inbound refresh skipped:", e)
    try:
        rsv = build_reserved()
        if rsv and plain.count("const RESERVED = ") == 1:
            plain = splice_const(plain, "RESERVED", rsv)
            print("Reserved: %d SKUs with reserved/researching/unfulfillable" % len(rsv))
    except Exception as e:
        print("Reserved refresh skipped:", e)
    try:
        px = build_ppcx()
        if px and plain.count("const PPCX = ") == 1:
            plain = splice_line(plain, "PPCX", json.dumps(px, ensure_ascii=False).replace("</", "<" + chr(92) + "/"))
            print("PPCX: %d ASINs, %d daily rows, %d warnings, %d keyword opportunities (asof %s)"
                  % (len(px["asins"]), len(px["daily"]), len(px["warns"]), px["harvest_n"], px["asof"]))
        elif px:
            print("PPCX const not in page yet; skipping splice (deploy the page update first).")
    except Exception as e:
        print("PPCX refresh skipped:", e)
    if plain == plain0:
        print("No data change; leaving index.html unchanged.")
        return
    out = encrypt(plain)
    with io.open(INDEX, "w", encoding="utf-8", newline="") as fh:
        fh.write(out)
    print("Re-encrypted -> index.html (%d bytes)" % len(out))


if __name__ == "__main__":
    main()
