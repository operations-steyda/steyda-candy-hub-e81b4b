"""PPC command-centre data (DataDoe only) -> one small JSON blob spliced into the page as PPCX.

Pure functions: take already-fetched row lists, return the blob. refresh.py does the fetching
(so this stays unit-testable against CSV exports). Sources:
  * Profit by SKU & Date   -> sales / ad spend / profit after COGS+fees+ads / sessions per ASIN per day
  * Ad Performance by ASIN -> ad clicks / impressions / orders per ASIN per day (PPC conversion)
  * Search Term Performance -> converting shopper queries and the match type that caught them

Windows: "recent" = last 30 days ending the latest data day; "prior" = the 30 days before it.
Everything profit-aware uses DataDoe's own COGS, so break-even ACOS needs no manual input.
"""
import datetime, re

DAYS = 30
BASE_FROM_DAYS = 44          # PPC-conversion baseline = days 15..44 back (recent = last 14)
MIN_SPEND_ASIN = 25.0        # ignore ASINs that barely advertise
MIN_CLICKS_CVR = 120         # sample size for a PPC conversion read
HARVEST_MIN_ORDERS = 2
LAG_DAYS = 2                 # Amazon sales/fees/COGS for the newest days are still settling


def _f(v):
    try:
        return float(v)
    except Exception:
        return 0.0


def _asin_in(name):
    m = re.search(r"B0[A-Z0-9]{8}", name or "")
    return m.group(0) if m else None


def _blank():
    return dict(sales=0.0, adsales=0.0, spend=0.0, profit=0.0, orders=0.0, units=0.0,
                cogs=0.0, sessions=0.0, aclicks=0.0, aorders=0.0, aimpr=0.0, asales=0.0, aspend=0.0)


def build(profit_rows, ad_rows, term_rows, camp_rows=None, today=None):
    """profit_rows cols: date, child_asin, sales_s, adspend_s, adsales_s, profit_s, orders_s,
    units_s, cogs_s, sessions_s.  ad_rows cols: date, child_asin, spend, sales, orders, clicks, impr.
    term_rows cols: ad_search_term, ad_match_type, ad_campaign_name, clicks, orders, sales, spend.
    camp_rows cols: date, spend, sales, orders, clicks, impr (campaign level = Amazon's own ad totals,
    incl. brand halo) -> account KPIs + daily chart. Per-ASIN verdicts stay on the stricter same-SKU basis."""
    if not profit_rows:
        return None
    latest = max(r["date"] for r in profit_rows)
    end = datetime.date.fromisoformat(latest) - datetime.timedelta(days=LAG_DAYS)
    last = end.isoformat()
    # drop the still-settling tail everywhere so windows, charts and profit stay on complete days
    profit_rows = [r for r in profit_rows if r["date"] <= last]
    ad_rows = [r for r in ad_rows if r["date"] <= last]
    camp_rows = [r for r in (camp_rows or []) if r["date"] <= last]
    rec_from = (end - datetime.timedelta(days=DAYS - 1)).isoformat()
    pri_from = (end - datetime.timedelta(days=2 * DAYS - 1)).isoformat()
    d14 = (end - datetime.timedelta(days=13)).isoformat()
    d45 = (end - datetime.timedelta(days=BASE_FROM_DAYS - 1)).isoformat()

    def win(d):
        if d >= rec_from:
            return "cur"
        if d >= pri_from:
            return "pri"
        return None

    acct = {"cur": _blank(), "pri": _blank()}
    asin = {}
    daily = {}
    for r in profit_rows:
        d = r["date"]
        w = win(d)
        if not w or not r.get("child_asin"):
            continue
        a = r["child_asin"]
        x = asin.setdefault(a, {"cur": _blank(), "pri": _blank(), "s14": 0.0, "o14": 0.0, "ses14": 0.0,
                                "ses_b": 0.0, "ord_b": 0.0, "spend14": 0.0})
        for tgt in (acct[w], x[w]):
            tgt["sales"] += _f(r["sales_s"]); tgt["adsales"] += _f(r["adsales_s"])
            tgt["spend"] += _f(r["adspend_s"]); tgt["profit"] += _f(r["profit_s"])
            tgt["orders"] += _f(r["orders_s"]); tgt["units"] += _f(r["units_s"])
            tgt["cogs"] += _f(r["cogs_s"]); tgt["sessions"] += _f(r["sessions_s"])
        if d >= d14:                                   # recent 14d overall conversion + ad spend
            x["o14"] += _f(r["orders_s"]); x["ses14"] += _f(r["sessions_s"]); x["spend14"] += _f(r["adspend_s"])
        elif d >= d45:                                 # baseline: days 15..44 back
            x["ord_b"] += _f(r["orders_s"]); x["ses_b"] += _f(r["sessions_s"])
        dd = daily.setdefault(d, dict(sales=0.0, adsales=0.0, spend=0.0, profit=0.0))
        dd["sales"] += _f(r["sales_s"]); dd["adsales"] += _f(r["adsales_s"])
        dd["spend"] += _f(r["adspend_s"]); dd["profit"] += _f(r["profit_s"])
    # also keep earlier days for the momentum chart (60d window already covers it)

    # ---- PPC clicks / orders from the ad-by-ASIN table ----
    pc = {}                                            # asin -> {cur_cl, cur_or, rec14_cl, rec14_or, base_cl, base_or}
    for r in ad_rows:
        d = r["date"]
        a = r.get("child_asin")
        if not a:
            continue
        w = win(d)
        p = pc.setdefault(a, dict(c14=0.0, o14=0.0, cb=0.0, ob=0.0))
        if w:
            tgt_asin = asin.setdefault(a, {"cur": _blank(), "pri": _blank(), "s14": 0.0, "o14": 0.0,
                                           "ses14": 0.0, "ses_b": 0.0, "ord_b": 0.0, "spend14": 0.0})
            for tgt in (acct[w], tgt_asin[w]):
                tgt["aclicks"] += _f(r["clicks"]); tgt["aorders"] += _f(r["orders"])
                tgt["aimpr"] += _f(r["impr"]); tgt["asales"] += _f(r["sales"]); tgt["aspend"] += _f(r["spend"])
        if d >= d14:
            p["c14"] += _f(r["clicks"]); p["o14"] += _f(r["orders"])
        elif d >= d45:
            p["cb"] += _f(r["clicks"]); p["ob"] += _f(r["orders"])

    # ---- per-ASIN rows + verdicts ----
    asins = []
    for a, x in asin.items():
        c, p = x["cur"], x["pri"]
        if c["sales"] < 1 and c["spend"] < 1 and c["aspend"] < 1:
            continue
        spend = c["aspend"] or c["spend"]
        asales = c["asales"] or c["adsales"]
        acos = spend / asales * 100 if asales else None
        margin_pre = (c["profit"] + c["spend"]) / c["sales"] * 100 if c["sales"] else None
        growth = ((c["sales"] / p["sales"] - 1) * 100) if p["sales"] > 20 else None
        pcv = pc.get(a, {})
        cvr14 = pcv.get("o14", 0) / pcv["c14"] * 100 if pcv.get("c14") else None
        cvrb = pcv.get("ob", 0) / pcv["cb"] * 100 if pcv.get("cb") else None
        ovr14 = x["o14"] / x["ses14"] * 100 if x["ses14"] else None
        ovrb = x["ord_b"] / x["ses_b"] * 100 if x["ses_b"] else None
        cogs_ok = c["cogs"] > 0 or c["sales"] < 1
        org_c = c["sales"] - c["adsales"]
        org_p = p["sales"] - p["adsales"]
        row = dict(a=a,
                   sales=round(c["sales"]), sales_p=round(p["sales"]),
                   spend=round(spend, 2), spend_p=round((p["aspend"] or p["spend"]), 2),
                   asales=round(asales), acos=None if acos is None else round(acos, 1),
                   profit=round(c["profit"]), profit_p=round(p["profit"]),
                   orders=round(c["orders"]), sessions=round(c["sessions"]),
                   clicks=round(c["aclicks"]), aorders=round(c["aorders"]),
                   cvr=round(c["aorders"] / c["aclicks"] * 100, 1) if c["aclicks"] else None,
                   be=None if margin_pre is None else round(margin_pre, 1),
                   growth=None if growth is None else round(growth, 1),
                   org=round(org_c), org_p=round(org_p),
                   spend14=round(x["spend14"], 2), cvr14=None if cvr14 is None else round(cvr14, 1),
                   cvrb=None if cvrb is None else round(cvrb, 1),
                   ovr14=None if ovr14 is None else round(ovr14, 1), ovrb=None if ovrb is None else round(ovrb, 1),
                   cogs_ok=bool(cogs_ok), c14=round(pcv.get("c14", 0)))
        row["q"] = _quadrant(row)
        asins.append(row)
    asins.sort(key=lambda z: -(z["spend"] * 10 + z["sales"]))

    # ---- early warnings ----
    warns = []
    for z in asins:
        if z["spend"] >= MIN_SPEND_ASIN and z["cvr14"] is not None and z["cvrb"] and z["c14"] >= MIN_CLICKS_CVR \
                and z["cvr14"] < z["cvrb"] * 0.6:
            warns.append(dict(a=z["a"], k="ppc_cvr", now=z["cvr14"], was=z["cvrb"], clicks=z["c14"],
                              spend14=z["spend14"]))
        if z["ovr14"] is not None and z["ovrb"] and z["ovrb"] > 0 and z["ovr14"] < z["ovrb"] * 0.65 and z["sessions"] >= 150 and z["orders"] >= 30:
            warns.append(dict(a=z["a"], k="cr_cliff", now=z["ovr14"], was=z["ovrb"], sessions=z["sessions"]))
        if z["spend"] >= MIN_SPEND_ASIN and z["profit"] < 0:
            warns.append(dict(a=z["a"], k="loss", profit=z["profit"], spend=z["spend"]))

    # ---- keyword opportunities: converting queries with NO exact-match coverage ----
    exact = set()
    agg = {}
    for r in term_rows:
        t = (r.get("ad_search_term") or "").strip().lower()
        if not t or t == "*" or re.match(r"^b0[a-z0-9]{8}$", t):
            continue
        a = _asin_in(r.get("ad_campaign_name"))
        mt = (r.get("ad_match_type") or "").upper()
        if mt == "EXACT":
            exact.add((t, a))
            exact.add((t, None))
        k = (t, a)
        d = agg.setdefault(k, dict(o=0.0, s=0.0, c=0.0, sp=0.0, mts=set()))
        d["o"] += _f(r["orders"]); d["s"] += _f(r["sales"]); d["c"] += _f(r["clicks"]); d["sp"] += _f(r["spend"])
        d["mts"].add(mt)
    harvest = []
    for (t, a), d in agg.items():
        if d["o"] < HARVEST_MIN_ORDERS or (t, a) in exact or (t, None) in exact or not a:
            continue
        harvest.append(dict(t=t, a=a, o=int(d["o"]), s=round(d["s"]), c=int(d["c"]), sp=round(d["sp"], 2),
                            acos=round(d["sp"] / d["s"] * 100, 1) if d["s"] else None,
                            m=sorted(x.replace("TARGETING_EXPRESSION_PREDEFINED", "AUTO").replace("TARGETING_EXPRESSION", "TARGET")
                                     for x in d["mts"] if x)[:2]))
    harvest.sort(key=lambda z: -z["s"])

    # ---- account-level ad totals from the campaign report (ties to Amazon / Seller Central) ----
    camp = {"cur": dict(spend=0.0, sales=0.0, orders=0.0, clicks=0.0, impr=0.0),
            "pri": dict(spend=0.0, sales=0.0, orders=0.0, clicks=0.0, impr=0.0)}
    cdaily = {}
    for r in (camp_rows or []):
        w = win(r["date"])
        if w:
            for k in ("spend", "sales", "orders", "clicks", "impr"):
                camp[w][k] += _f(r[k])
        cdaily[r["date"]] = (_f(r["sales"]), _f(r["spend"]))
    cur, pri = acct["cur"], acct["pri"]
    for w in (cur, pri):
        for k in list(w):
            w[k] = round(w[k], 2)
    daily_rows = [dict(d=d, sales=round(v["sales"]),
                       adsales=round(cdaily[d][0]) if d in cdaily else round(v["adsales"]),
                       spend=round(cdaily[d][1], 2) if d in cdaily else round(v["spend"], 2),
                       profit=round(v["profit"]))
                  for d, v in sorted(daily.items())]
    for w in camp.values():
        for k in list(w):
            w[k] = round(w[k], 2)
    return dict(asof=last, cur_from=rec_from, pri_from=pri_from, cur=cur, pri=pri, camp=camp if camp_rows else None, latest=latest,
                daily=daily_rows,
                asins=asins, warns=warns, harvest=harvest[:30], harvest_n=len(harvest))


def _quadrant(z):
    """Profit-aware 2x2. Growth vs ads efficiency against the product's OWN break-even ACOS."""
    g, acos, be = z["growth"], z["acos"], z["be"]
    if z["spend"] < MIN_SPEND_ASIN:
        return "organic" if z["sales"] >= 50 else "quiet"
    if z["profit"] < 0:
        return "drain"
    eff = (acos is not None and be is not None and acos <= be)
    growing = g is not None and g >= 10
    if growing and eff:
        return "star"
    if growing:
        return "question"
    if eff:
        return "workhorse"
    return "drain"
