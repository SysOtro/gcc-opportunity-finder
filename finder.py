#!/usr/bin/env python3
"""GCC Product Opportunity Finder -- bestseller / sentiment / price-gap analysis.

Pulls Amazon.sa, Amazon.ae and Noon.com bestseller data, scores every category on
how under-served it is, and writes a self-contained HTML report.

  python finder.py                                  # all categories, all markets
  python finder.py --markets sa --open
  python finder.py --markets noon --categories toys,beauty
  python finder.py --selftest                       # no network, asserts only

Noon sits behind Akamai and geo-fences non-GCC IPs -- it silently drops the
connection rather than returning 403. From a GCC IP (or VPN) it is fetched
directly; from anywhere else it falls back to the r.jina.ai text proxy, which
reaches Noon's public catalog API. Set JINA_API_KEY to lift 20/min to 200/min.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import html
import json
import os
import re
import statistics as st
import sys
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from string import Template

import requests

ROOT = Path(__file__).parent
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "en-AE,en;q=0.9",
           "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}
TIMEOUT = 25

# Verified to return a parseable 30-product grid on both .sa and .ae.
# Slugs like computers/kitchen/sports return HTTP 200 with an empty grid -- dropped.
CATEGORIES = [
    "electronics", "beauty", "home", "toys", "automotive", "fashion",
    "baby-products", "pet-supplies", "office-products", "grocery",
    "videogames", "books", "garden", "home-improvement", "health",
]
MARKETS = {"sa": ("Amazon.sa", "SAR"), "ae": ("Amazon.ae", "AED"), "noon": ("Noon.com", "SAR")}
BLOCKED = {
    "amazon review text": "full review bodies need per-ASIN pages behind a bot check; star ratings only",
    "noon direct access": "Akamai geo-fences non-GCC IPs and drops the connection silently; reached "
                          "via the r.jina.ai text proxy unless you run from the GCC or a VPN",
    "noon listing counts": "nbHits is Noon's own reported match count for the query, not an audited "
                           "catalogue census -- treat it as relative, not absolute",
}

# Noon browses by search query, not by Amazon's slugs. Keys mirror CATEGORIES so
# both marketplaces land in the same category buckets when scored.
NOON_CATEGORIES = {
    "electronics": "electronics", "beauty": "beauty", "home": "home kitchen",
    "toys": "toys", "automotive": "automotive", "fashion": "fashion",
    "baby-products": "mom baby", "pet-supplies": "pet supplies",
    "office-products": "stationery", "grocery": "grocery",
    "videogames": "video games", "books": "books", "garden": "garden outdoor",
    "home-improvement": "tools home improvement", "health": "health nutrition",
}
NOON_API = ("https://www.noon.com/_svc/catalog/api/v3/search"
            "?q={q}&limit=100&sort[by]=popularity&sort[dir]=desc")
JINA = "https://r.jina.ai/"
# ponytail: keyless jina allows 20 req/60s. Sequential with this gap clears 15
# categories in ~48s. Knob, not a constant -- the cap moves and a key lifts it.
NOON_DELAY = 3.2

# ponytail: SAR and AED are both USD-pegged, so this is stable, not a live rate.
# Knob is here because pegs get re-fixed and the report should not silently lie.
AED_TO_SAR = 1.0212

CARD = re.compile(
    r'<div id="([A-Z0-9]{10})" class="p13n-sc-uncoverable-faceout"'
    r'(.*?)(?=<div id="[A-Z0-9]{10}" class="p13n-sc-uncoverable-faceout"|</ol>|\Z)', re.S)
RE_TITLE = re.compile(r'p13n-sc-css-line-clamp-\d+_\w+">(.*?)</div>', re.S)
RE_STARS = re.compile(r'aria-label="([\d.]+) out of 5 stars, ([\d,]+) ratings?"')
RE_PRICE = re.compile(r'p13n-sc-price_\w+">\s*([A-Z]{3})?\s*([\d,]+\.?\d*)')
RE_IMG = re.compile(r'src="(https://images[^"]+?)"')


# --------------------------------------------------------------------------- fetch
def fetch(market: str, cat: str) -> tuple[str, str, str]:
    """-> (market, category, html). Empty html on any failure; caller reports it."""
    url = f"https://www.amazon.{market}/gp/bestsellers/{cat}/?language=en_AE"
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        return market, cat, r.text if r.status_code == 200 else ""
    except requests.RequestException:
        return market, cat, ""


def parse(page: str, market: str, cat: str) -> list[dict]:
    """Bestseller grid -> product dicts. Page order IS rank order."""
    out = []
    for i, (asin, blk) in enumerate(CARD.findall(page)):
        title = RE_TITLE.search(blk)
        stars = RE_STARS.search(blk)
        price = RE_PRICE.search(blk)
        if not (title and price):
            continue
        cur = price.group(1) or MARKETS[market][1]
        amount = float(price.group(2).replace(",", ""))
        out.append({
            "asin": asin,
            "market": market,
            "category": cat,
            "rank": i + 1,
            "title": html.unescape(title.group(1)).strip(),
            "rating": float(stars.group(1)) if stars else None,
            "reviews": int(stars.group(2).replace(",", "")) if stars else 0,
            "price": amount,
            "currency": cur,
            "price_sar": round(amount * (AED_TO_SAR if cur == "AED" else 1.0), 2),
            "img": (RE_IMG.search(blk) or [None, ""])[1],
            "url": f"https://www.amazon.{market}/dp/{asin}",
        })
    return out


def fetch_noon(cat: str) -> dict | None:
    """Noon catalog API -> raw JSON. Direct first (fast, works from GCC/VPN), then
    the jina text proxy (works from anywhere Akamai has geo-fenced)."""
    url = NOON_API.format(q=requests.utils.quote(NOON_CATEGORIES.get(cat, cat)))
    key = os.environ.get("JINA_API_KEY", "").strip()
    # The proxy hop must stay minimal: a browser User-Agent trips Cloudflare's
    # challenge on r.jina.ai, and Accept:application/json makes it return 0 hits.
    proxy = (JINA + url, {"x-respond-with": "text",
                          **({"Authorization": f"Bearer {key}"} if key else {})}, 60)
    # Direct, then the proxy twice -- the proxy occasionally truncates a large
    # payload mid-string, which is transient and clears on a retry.
    attempts = [(url, {**HEADERS, "Accept": "application/json"}, 8), proxy, proxy]
    for target, headers, timeout in attempts:
        try:
            r = requests.get(target, headers=headers, timeout=timeout)
            if r.status_code == 200:
                return json.loads(r.text)
        except (requests.RequestException, json.JSONDecodeError):
            continue
    return None


def parse_noon(payload: dict, cat: str) -> list[dict]:
    """Noon hits -> the same product dict parse() emits, so scoring needs no changes."""
    out = []
    for i, h in enumerate(payload.get("hits") or []):
        rating = h.get("product_rating") or {}
        # sale_price is what the customer actually pays; price is the struck-through list.
        paid = h.get("sale_price") or h.get("price")
        listed = h.get("price") or paid
        if not paid:
            continue
        out.append({
            "asin": h.get("sku", ""),
            "market": "noon",
            "category": cat,
            "rank": i + 1,
            "title": (h.get("name") or "").strip(),
            "rating": float(rating["value"]) if rating.get("value") else None,
            "reviews": int(rating.get("count") or 0),
            "price": float(paid),
            "currency": "SAR",
            "price_sar": round(float(paid), 2),
            "img": h.get("image_url", ""),
            "url": f"https://www.noon.com/saudi-en/{h.get('url', '')}",
            "seller": h.get("store_name") or "unknown",
            "list_price": float(listed),
            "discount": round(1 - float(paid) / float(listed), 3) if listed and listed > 0 else 0.0,
            "is_bestseller": bool(h.get("is_bestseller")),
        })
    return out


def collect(markets: list[str], cats: list[str], workers: int = 10, noon_delay: float = NOON_DELAY):
    """Amazon pages fetch concurrently; Noon is paced to respect the proxy rate cap."""
    amz = [m for m in markets if m != "noon"]
    products, misses, depth = [], [], {}

    jobs = [(m, c) for m in amz for c in cats]
    if jobs:
        with cf.ThreadPoolExecutor(workers) as ex:
            for market, cat, page in ex.map(lambda a: fetch(*a), jobs):
                got = parse(page, market, cat) if page else []
                products.extend(got)
                if not got:
                    misses.append(f"amazon.{market}/{cat}")

    if "noon" in markets:
        for n, cat in enumerate(cats):
            if n:
                time.sleep(noon_delay)
            payload = fetch_noon(cat)
            got = parse_noon(payload, cat) if payload else []
            products.extend(got)
            if got:
                depth[cat] = {"listings": payload.get("nbHits") or 0, "items": got}
            else:
                misses.append(f"noon/{cat}")
    return products, misses, depth


# --------------------------------------------------------------------------- score
def pct_rank(value: float, pool: list[float]) -> float:
    """Percentile of value within pool, 0-100. Ties count as half."""
    if len(pool) < 2:
        return 50.0
    below = sum(1 for p in pool if p < value) + sum(0.5 for p in pool if p == value)
    return 100.0 * below / len(pool)


def category_stats(products: list[dict]) -> list[dict]:
    """Raw per-category signals. Opportunity scoring happens after, across categories."""
    by_cat: dict[str, list[dict]] = {}
    for p in products:
        by_cat.setdefault(p["category"], []).append(p)

    rows = []
    for cat, items in by_cat.items():
        prices = sorted(p["price_sar"] for p in items)
        rated = [p for p in items if p["rating"] is not None]
        reviews = sorted(p["reviews"] for p in items)

        # Largest empty price band inside the p10-p90 body, as % of that body.
        body = prices[len(prices) // 10: max(len(prices) - len(prices) // 10, 1)] or prices
        span = (body[-1] - body[0]) or 1.0
        gap = max((b - a for a, b in zip(body, body[1:])), default=0.0)

        rows.append({
            "category": cat,
            "n": len(items),
            "med_reviews": st.median(reviews) if reviews else 0,
            "avg_rating": round(st.mean(p["rating"] for p in rated), 2) if rated else 0.0,
            "weak_share": sum(1 for p in rated if p["rating"] < 4.2) / len(rated) if rated else 0.0,
            "entrant_share": sum(1 for p in items if p["reviews"] < 100) / len(items),
            "price_gap": round(100 * gap / span, 1),
            "med_price": round(st.median(prices), 2),
            "price_lo": round(body[0], 2),
            "price_hi": round(body[-1], 2),
            "gap_lo": round(max(zip(body, body[1:]), key=lambda ab: ab[1] - ab[0])[0], 2) if len(body) > 1 else 0,
            "gap_hi": round(max(zip(body, body[1:]), key=lambda ab: ab[1] - ab[0])[1], 2) if len(body) > 1 else 0,
        })
    return rows


def score(rows: list[dict]) -> list[dict]:
    """Opportunity 0-100 from cross-category percentiles. No magic constants that
    break when Amazon's absolute numbers drift -- everything is relative to the run."""
    moats = [r["med_reviews"] for r in rows]
    weaks = [r["weak_share"] for r in rows]
    gaps = [r["price_gap"] for r in rows]
    entrants = [r["entrant_share"] for r in rows]

    for r in rows:
        r["s_moat"] = 100 - pct_rank(r["med_reviews"], moats)      # few reviews = beatable
        r["s_weak"] = pct_rank(r["weak_share"], weaks)             # unhappy buyers = room
        r["s_gap"] = pct_rank(r["price_gap"], gaps)                # empty price band
        r["s_entrant"] = pct_rank(r["entrant_share"], entrants)    # new sellers chart = low barrier
        r["opportunity"] = round(
            0.35 * r["s_moat"] + 0.30 * r["s_weak"] + 0.20 * r["s_gap"] + 0.15 * r["s_entrant"], 1)
    return sorted(rows, key=lambda r: -r["opportunity"])


def noon_depth(depth: dict) -> list[dict]:
    """Signals only Noon exposes: competition density, discount pressure, seller spread."""
    rows = []
    for cat, d in depth.items():
        items = d["items"]
        cut = [p["discount"] for p in items if p["discount"] > 0]
        sellers = {p["seller"] for p in items}
        rows.append({
            "category": cat,
            "listings": d["listings"],
            "sampled": len(items),
            "med_discount": round(100 * st.median(cut), 1) if cut else 0.0,
            "discounted": round(100 * len(cut) / len(items)) if items else 0,
            "sellers": len(sellers),
            "seller_spread": round(100 * len(sellers) / len(items)) if items else 0,
            "bestsellers": sum(1 for p in items if p["is_bestseller"]),
        })
    return sorted(rows, key=lambda r: -r["listings"])


def per_source(products: list[dict], ranked: list[dict]) -> list[dict]:
    """Score the Amazon-only and Noon-only pools separately for side-by-side columns.
    A category missing from a marketplace gets None -- never a fabricated zero."""
    def sub(pool):
        return {r["category"]: r["opportunity"] for r in score(category_stats(pool))} if pool else {}
    amz = sub([p for p in products if p["market"] != "noon"])
    noon = sub([p for p in products if p["market"] == "noon"])
    for r in ranked:
        r["amzn_score"] = amz.get(r["category"])
        r["noon_score"] = noon.get(r["category"])
    return ranked


def weak_incumbents(products: list[dict], limit: int = 40) -> list[dict]:
    """Bestsellers customers actively dislike: proven demand, poor satisfaction."""
    hits = [p for p in products if p["rating"] and p["rating"] <= 4.2 and p["reviews"] >= 30]
    for p in hits:
        # Low rating + high rank = the most exposed. Reviews cap the upside.
        p["weakness"] = round((4.6 - p["rating"]) * 40 + (31 - p["rank"]) * 1.2, 1)
    return sorted(hits, key=lambda p: -p["weakness"])[:limit]


def arbitrage(products: list[dict], limit: int = 25) -> list[dict]:
    """Same ASIN, both markets, price delta > 15% after peg conversion."""
    by_asin: dict[str, dict[str, dict]] = {}
    for p in products:
        by_asin.setdefault(p["asin"], {})[p["market"]] = p
    out = []
    for asin, mk in by_asin.items():
        if "sa" not in mk or "ae" not in mk:   # Noon SKUs share no namespace with ASINs
            continue
        sa, ae = mk["sa"], mk["ae"]
        lo, hi = sorted((sa["price_sar"], ae["price_sar"]))
        if lo <= 0 or (hi - lo) / lo < 0.15:
            continue
        out.append({"title": sa["title"], "asin": asin, "sa": sa["price_sar"], "ae": ae["price_sar"],
                    "delta": round(100 * (hi - lo) / lo, 1), "url": sa["url"],
                    "cheaper": "SA" if sa["price_sar"] < ae["price_sar"] else "AE"})
    return sorted(out, key=lambda r: -r["delta"])[:limit]


# --------------------------------------------------------------------------- render
PAGE = Template("""<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>GCC Product Opportunity Finder</title>
<style>
:root{--bg:#0d1117;--card:#161b22;--line:#26303d;--fg:#e6edf3;--dim:#8b949e;--hi:#3fb950;--mid:#d29922;--lo:#6e7681;--acc:#58a6ff}
@media(prefers-color-scheme:light){:root{--bg:#f6f8fa;--card:#fff;--line:#d8dee4;--fg:#1f2328;--dim:#636c76;--acc:#0969da}}
*{box-sizing:border-box}body{margin:0;padding:32px 20px;background:var(--bg);color:var(--fg);
font:15px/1.55 -apple-system,Segoe UI,Roboto,sans-serif}
.wrap{max-width:1180px;margin:0 auto}h1{font-size:26px;margin:0 0 4px}h2{font-size:18px;margin:38px 0 12px}
.sub{color:var(--dim);font-size:13px;margin-bottom:24px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:20px 0}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}
.kpi b{display:block;font-size:24px;font-weight:650}.kpi span{color:var(--dim);font-size:12px}
.tw{overflow-x:auto;border:1px solid var(--line);border-radius:10px;background:var(--card)}
table{border-collapse:collapse;width:100%;font-size:13.5px;min-width:640px}
th{text-align:left;padding:10px 12px;border-bottom:1px solid var(--line);color:var(--dim);font-weight:600;
font-size:12px;text-transform:uppercase;letter-spacing:.4px;white-space:nowrap}
td{padding:10px 12px;border-bottom:1px solid var(--line);vertical-align:top}
tr:last-child td{border-bottom:0}.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
a{color:var(--acc);text-decoration:none}a:hover{text-decoration:underline}
.pill{display:inline-block;padding:2px 9px;border-radius:99px;font-size:12px;font-weight:600}
.p-hi{background:rgba(63,185,80,.16);color:var(--hi)}.p-mid{background:rgba(210,153,34,.16);color:var(--mid)}
.p-lo{background:rgba(110,118,129,.16);color:var(--lo)}
.bar{height:5px;border-radius:3px;background:var(--hi);margin-top:5px}
.note{background:var(--card);border:1px solid var(--line);border-left:3px solid var(--mid);
border-radius:8px;padding:12px 16px;font-size:13px;color:var(--dim);margin:14px 0}
.cat{font-weight:600;text-transform:capitalize}.t{max-width:420px}
code{background:rgba(110,118,129,.18);padding:1px 5px;border-radius:4px;font-size:12px}
</style>
<div class="wrap">
<h1>GCC Product Opportunity Finder</h1>
<div class="sub">$n_products live bestseller listings &middot; $n_cats categories &middot; $markets &middot; generated $ts</div>

<div class="kpis">
  <div class="kpi"><b>$n_products</b><span>products analysed</span></div>
  <div class="kpi"><b>$n_cats</b><span>categories ranked</span></div>
  <div class="kpi"><b>$n_weak</b><span>weak incumbents</span></div>
  <div class="kpi"><b>$n_listings</b><span>Noon rival listings</span></div>
  <div class="kpi"><b>$n_arb</b><span>SA/AE price gaps</span></div>
  <div class="kpi"><b>$top_cat</b><span>best opportunity</span></div>
</div>

<h2>1 &middot; GCC category opportunity ranking</h2>
<div class="tw"><table>
<tr><th>#</th><th>Category</th><th class="num">GCC score</th><th class="num">Amzn</th><th class="num">Noon</th>
<th class="num">Median reviews</th><th class="num">Avg rating</th><th class="num">Rated &lt;4.2</th>
<th class="num">Newcomers</th><th class="num">Price band (SAR)</th><th class="num">Biggest gap</th></tr>
$cat_rows
</table></div>
<div class="note"><b>GCC score</b> = 35% low review moat (median reviews of the chart &mdash; low means
incumbents are beatable) + 30% weak sentiment (share of bestsellers rated under 4.2) + 20% price gap
(largest empty band in the p10&ndash;p90 price body) + 15% newcomer share (chart entries under 100 reviews
&mdash; proves a new seller can rank). All four are percentiles <i>within this run</i>, so the ranking stays
valid as absolute numbers drift. <b>Amzn</b> and <b>Noon</b> re-run the same four signals on that
marketplace alone; <b>&mdash;</b> means the category did not return data there.</div>

<h2>2 &middot; Noon competition depth</h2>
<div class="sub">Signals only Noon exposes. <b>Listings</b> is how many products compete for the query &mdash;
the most direct read on crowding. <b>Median discount</b> is margin pressure: heavy discounting means
incumbents are buying their rank. <b>Sellers/100</b> is fragmentation &mdash; a high number means no
entrenched winner, a low number means one seller owns the category.</div>
<div class="tw"><table>
<tr><th>Category</th><th class="num">Listings</th><th class="num">Sampled</th><th class="num">Median discount</th>
<th class="num">On discount</th><th class="num">Sellers/100</th><th class="num">Flagged bestseller</th></tr>
$noon_rows
</table></div>

<h2>3 &middot; Weak incumbents &mdash; proven demand, unhappy buyers</h2>
<div class="sub">Bestsellers rated 4.2 or below with 30+ ratings. Demand is confirmed by the chart position;
the rating says the current product does not satisfy it.</div>
<div class="tw"><table>
<tr><th>Product</th><th>Market</th><th>Category</th><th class="num">Rank</th><th class="num">Rating</th>
<th class="num">Ratings</th><th class="num">Price</th></tr>
$weak_rows
</table></div>

<h2>4 &middot; Cross-market price gaps (Amazon SA vs AE)</h2>
<div class="sub">Identical ASIN priced 15%+ apart after SAR/AED peg conversion at $peg.
Signals thin local competition on the expensive side.</div>
<div class="tw"><table>
<tr><th>Product</th><th class="num">Amazon.sa</th><th class="num">Amazon.ae</th>
<th class="num">Delta</th><th>Cheaper</th></tr>
$arb_rows
</table></div>

<h2>5 &middot; Caveats and coverage</h2>
<div class="tw"><table><tr><th>Item</th><th>What to know</th></tr>$blocked_rows</table></div>
$misses
<div class="sub" style="margin-top:28px">Bestseller ranks are a 24h snapshot and rotate daily. Re-run before
acting &mdash; <code>python finder.py</code>. Star ratings stand in for review sentiment; full review text is
behind a per-ASIN bot check.</div>
</div>""")


def pill(v: float) -> str:
    cls = "p-hi" if v >= 66 else "p-mid" if v >= 40 else "p-lo"
    return f'<span class="pill {cls}">{v:.0f}</span>'


def render(cats, noon, weak, arb, products, misses, out: Path) -> Path:
    e = html.escape
    sub = lambda v: f"{v:.0f}" if v is not None else "&mdash;"  # noqa: E731 -- absent, not zero
    cat_rows = "\n".join(
        f'<tr><td class="num">{i}</td><td class="cat">{e(r["category"].replace("-", " "))}</td>'
        f'<td class="num">{pill(r["opportunity"])}<div class="bar" style="width:{r["opportunity"]:.0f}%"></div></td>'
        f'<td class="num">{sub(r.get("amzn_score"))}</td><td class="num">{sub(r.get("noon_score"))}</td>'
        f'<td class="num">{r["med_reviews"]:,.0f}</td><td class="num">{r["avg_rating"]}</td>'
        f'<td class="num">{r["weak_share"]*100:.0f}%</td><td class="num">{r["entrant_share"]*100:.0f}%</td>'
        f'<td class="num">{r["price_lo"]:,.0f} &ndash; {r["price_hi"]:,.0f}</td>'
        f'<td class="num">{r["gap_lo"]:,.0f} &rarr; {r["gap_hi"]:,.0f}</td></tr>'
        for i, r in enumerate(cats, 1))

    noon_rows = "\n".join(
        f'<tr><td class="cat">{e(r["category"].replace("-", " "))}</td>'
        f'<td class="num">{r["listings"]:,}</td><td class="num">{r["sampled"]}</td>'
        f'<td class="num">{r["med_discount"]:.0f}%</td><td class="num">{r["discounted"]}%</td>'
        f'<td class="num">{r["seller_spread"]}</td><td class="num">{r["bestsellers"]}</td></tr>'
        for r in noon) or \
        '<tr><td colspan="7">Noon returned no data this run &mdash; add <code>noon</code> to ' \
        '<code>--markets</code>, or the proxy hop failed (see caveats).</td></tr>'

    weak_rows = "\n".join(
        f'<tr><td class="t"><a href="{p["url"]}" target="_blank" rel="noopener">{e(p["title"][:110])}</a></td>'
        f'<td>{MARKETS[p["market"]][0]}</td><td class="cat">{e(p["category"].replace("-", " "))}</td>'
        f'<td class="num">#{p["rank"]}</td>'
        f'<td class="num">{p["rating"]}</td><td class="num">{p["reviews"]:,}</td>'
        f'<td class="num">{p["currency"]} {p["price"]:,.0f}</td></tr>' for p in weak) or \
        '<tr><td colspan="7">No bestseller fell below 4.2 with enough ratings.</td></tr>'

    arb_rows = "\n".join(
        f'<tr><td class="t"><a href="{r["url"]}" target="_blank" rel="noopener">{e(r["title"][:110])}</a></td>'
        f'<td class="num">{r["sa"]:,.0f}</td><td class="num">{r["ae"]:,.0f}</td>'
        f'<td class="num">{pill(min(r["delta"], 100))}</td><td>{r["cheaper"]}</td></tr>' for r in arb) or \
        '<tr><td colspan="5">No ASIN charted in both markets &mdash; run with <code>--markets sa,ae</code>.</td></tr>'

    blocked_rows = "\n".join(f"<tr><td><b>{e(k)}</b></td><td>{e(v)}</td></tr>" for k, v in BLOCKED.items())
    miss_html = (f'<div class="note">Returned no parseable grid this run: {e(", ".join(misses))}</div>'
                 if misses else "")

    listings = sum(r["listings"] for r in noon)
    out.write_text(PAGE.substitute(
        n_products=f"{len(products):,}", n_cats=len(cats), n_weak=len(weak), n_arb=len(arb),
        n_listings=f"{listings:,}" if listings else "&mdash;",
        top_cat=cats[0]["category"].replace("-", " ").title() if cats else "n/a",
        markets=", ".join(sorted({MARKETS[p["market"]][0] for p in products})) or "none",
        ts=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), peg=f"1 AED = {AED_TO_SAR} SAR",
        cat_rows=cat_rows, noon_rows=noon_rows, weak_rows=weak_rows, arb_rows=arb_rows,
        blocked_rows=blocked_rows, misses=miss_html), encoding="utf-8")
    return out


# --------------------------------------------------------------------------- selftest
FIXTURE = '''<div id="B00NTCH52W" class="p13n-sc-uncoverable-faceout"><a href="/dp/x">
<img src="https://images-eu.ssl-images-amazon.com/images/I/81U.jpg"/></a>
<div class="_cDEzb_p13n-sc-css-line-clamp-3_g3dy1">Amazon Basics AA Batteries, 20-Pack</div>
<a aria-label="4.6 out of 5 stars, 1,018,645 ratings"><span class="a-icon-alt">4.6 out of 5 stars</span></a>
<span class="_cDEzb_p13n-sc-price_3mJ9Z">SAR 26.00</span>
<div id="B0000AISMR" class="p13n-sc-uncoverable-faceout">
<div class="_cDEzb_p13n-sc-css-line-clamp-2_xyz">Cheap Gadget &amp; Thing</div>
<a aria-label="3.9 out of 5 stars, 44 ratings"></a>
<span class="_cDEzb_p13n-sc-price_3mJ9Z">SAR 199.50</span></ol>'''


def selftest() -> int:
    got = parse(FIXTURE, "sa", "electronics")
    assert len(got) == 2, got
    a, b = got
    assert a["asin"] == "B00NTCH52W" and a["rank"] == 1
    assert a["rating"] == 4.6 and a["reviews"] == 1018645 and a["price"] == 26.0
    assert a["title"] == "Amazon Basics AA Batteries, 20-Pack", a["title"]
    assert b["title"] == "Cheap Gadget & Thing", b["title"]     # entity unescaped
    assert b["price"] == 199.5 and b["rank"] == 2

    # AED converts into SAR; SAR passes through untouched.
    ae = parse(FIXTURE.replace("SAR 26.00", "AED 100.00"), "ae", "electronics")[0]
    assert ae["price_sar"] == round(100 * AED_TO_SAR, 2), ae["price_sar"]

    assert pct_rank(5, [1, 2, 3, 4]) == 100.0
    assert pct_rank(1, [1, 2, 3, 4]) == 12.5          # ties count half
    assert pct_rank(7, [7]) == 50.0                   # degenerate pool

    # A cheap chart full of badly-rated, low-review items must outscore an
    # entrenched, well-loved one -- that is the entire thesis of the tool.
    easy = [{"category": "easy", "price_sar": 10 + i, "rating": 3.6, "reviews": 50, "rank": i}
            for i in range(30)]
    hard = [{"category": "hard", "price_sar": 500 + i, "rating": 4.8, "reviews": 90000, "rank": i}
            for i in range(30)]
    ranked = score(category_stats(easy + hard))
    assert ranked[0]["category"] == "easy", ranked
    assert ranked[0]["opportunity"] > ranked[1]["opportunity"]

    assert len(weak_incumbents(easy)) == 30 and not weak_incumbents(hard)

    # -- Noon normalisation: nested rating, sale_price wins, discount maths.
    payload = {"nbHits": 53556, "hits": [
        {"sku": "N70215854V", "name": "ASUS Vivobook 14", "price": 1529, "sale_price": 1449,
         "product_rating": {"value": 4.4, "count": 3687, "best_rating": 4.6},
         "store_name": "noon", "url": "asus-vivobook/p/", "is_bestseller": True},
        {"sku": "N999", "name": "No discount item", "price": 100, "sale_price": 100,
         "product_rating": {}, "store_name": "JXH", "url": "x/p/", "is_bestseller": False},
    ]}
    n = parse_noon(payload, "electronics")
    assert len(n) == 2 and n[0]["market"] == "noon" and n[0]["rank"] == 1
    assert n[0]["rating"] == 4.4 and n[0]["reviews"] == 3687        # off the nested object
    assert n[0]["price"] == 1449 and n[0]["price_sar"] == 1449.0    # sale_price, not list
    assert n[0]["discount"] == round(1 - 1449 / 1529, 3) and n[0]["seller"] == "noon"
    assert n[1]["rating"] is None and n[1]["reviews"] == 0 and n[1]["discount"] == 0.0
    assert set(n[0]) >= set(got[0]), "Noon dict must be a superset of the Amazon shape"

    d = noon_depth({"electronics": {"listings": 53556, "items": n}})[0]
    assert d["listings"] == 53556 and d["sellers"] == 2 and d["discounted"] == 50
    assert d["bestsellers"] == 1

    # Per-source columns: present in both -> two numbers; present in one -> None, not 0.
    both = easy + [dict(p, category="easy", market="noon") for p in hard]
    for p in both[:30]:
        p["market"] = "sa"
    ranked = per_source(both, score(category_stats(both)))
    assert ranked[0]["amzn_score"] is not None and ranked[0]["noon_score"] is not None
    solo = per_source(easy, score(category_stats(easy)))
    assert solo[0]["noon_score"] is None, "absent marketplace must be None, never 0"

    # Amazon-only ASIN matching must not pair a Noon SKU with an Amazon listing.
    assert arbitrage(n + [dict(p, market="sa", asin="N70215854V") for p in easy[:1]]) == []
    print("selftest OK")
    return 0


# --------------------------------------------------------------------------- cli
def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--markets", default="sa,ae,noon", help="comma list of: sa, ae, noon (default all)")
    p.add_argument("--categories", default=",".join(CATEGORIES), help="comma list (default all 15)")
    p.add_argument("--noon-delay", type=float, default=NOON_DELAY,
                   help=f"seconds between Noon calls, proxy caps at 20/min (default {NOON_DELAY})")
    p.add_argument("--out", type=Path, default=ROOT / "report.html")
    p.add_argument("--open", action="store_true", help="open the report when done")
    p.add_argument("--selftest", action="store_true", help="run asserts, no network")
    a = p.parse_args(argv)

    if a.selftest:
        return selftest()

    markets = [m.strip() for m in a.markets.split(",") if m.strip() in MARKETS]
    cats = [c.strip() for c in a.categories.split(",") if c.strip()]
    if not markets:
        print(f"No valid market. Pick from: {', '.join(MARKETS)}", file=sys.stderr)
        return 2

    print(f"Fetching {len(markets) * len(cats)} listings pages"
          f"{' (Noon is paced for the proxy rate cap)' if 'noon' in markets else ''}...")
    products, misses, depth = collect(markets, cats, noon_delay=a.noon_delay)
    if not products:
        print("Nothing fetched -- no marketplace returned parseable data. Retry or check connectivity.",
              file=sys.stderr)
        return 1

    rows = per_source(products, score(category_stats(products)))
    noon = noon_depth(depth)
    weak = weak_incumbents(products)
    arb = arbitrage(products) if {"sa", "ae"} <= set(markets) else []
    print(f"{len(products)} products -> {len(rows)} categories, {len(weak)} weak incumbents, "
          f"{len(noon)} Noon categories, {len(arb)} price gaps")
    if misses:
        print(f"No data from: {', '.join(misses)}")

    out = render(rows, noon, weak, arb, products, misses, a.out)
    print(f"Report: {out.resolve()}")
    for r in rows[:5]:
        print(f"  {r['opportunity']:5.1f}  {r['category']}")
    if a.open:
        webbrowser.open(out.resolve().as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
