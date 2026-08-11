#!/usr/bin/env python3
"""GCC Product Opportunity Finder -- bestseller / sentiment / price-gap analysis.

Pulls Amazon.sa + Amazon.ae bestseller lists, scores every category on how
under-served it is, and writes a self-contained HTML report.

  python finder.py                                  # all categories, both markets
  python finder.py --markets sa --open
  python finder.py --categories electronics,beauty,health
  python finder.py --selftest                       # no network, asserts only

Noon.com is not fetched: it sits behind Cloudflare and refuses datacenter/non-GCC
IPs (ConnectionError/ReadTimeout on every endpoint). See BLOCKED below -- the
report says so out loud instead of quietly shipping half the market as fact.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import html
import re
import statistics as st
import sys
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
MARKETS = {"sa": ("Amazon.sa", "SAR"), "ae": ("Amazon.ae", "AED")}
BLOCKED = {
    "noon.com": "Cloudflare + geo-fenced; every endpoint times out from outside GCC residential IPs",
    "amazon review text": "full review bodies need per-ASIN pages behind a bot check; star ratings only",
}

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


def collect(markets: list[str], cats: list[str], workers: int = 10):
    jobs = [(m, c) for m in markets for c in cats]
    products, misses = [], []
    with cf.ThreadPoolExecutor(workers) as ex:
        for market, cat, page in ex.map(lambda a: fetch(*a), jobs):
            got = parse(page, market, cat) if page else []
            products.extend(got)
            if not got:
                misses.append(f"amazon.{market}/{cat}")
    return products, misses


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
        if len(mk) < 2:
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
  <div class="kpi"><b>$n_arb</b><span>SA/AE price gaps</span></div>
  <div class="kpi"><b>$top_cat</b><span>best opportunity</span></div>
</div>

<h2>1 &middot; Category opportunity ranking</h2>
<div class="tw"><table>
<tr><th>#</th><th>Category</th><th class="num">Score</th><th class="num">Median reviews</th>
<th class="num">Avg rating</th><th class="num">Rated &lt;4.2</th><th class="num">Newcomers</th>
<th class="num">Price band (SAR)</th><th class="num">Biggest gap</th></tr>
$cat_rows
</table></div>
<div class="note"><b>Score</b> = 35% low review moat (median reviews of the chart &mdash; low means incumbents
are beatable) + 30% weak sentiment (share of bestsellers rated under 4.2) + 20% price gap (largest empty
band in the p10&ndash;p90 price body) + 15% newcomer share (chart entries under 100 reviews &mdash; proves a
new seller can rank). All four are percentiles <i>within this run</i>, so the ranking stays valid as
Amazon's absolute numbers drift.</div>

<h2>2 &middot; Weak incumbents &mdash; proven demand, unhappy buyers</h2>
<div class="sub">Bestsellers rated 4.2 or below with 30+ ratings. Demand is confirmed by the chart position;
the rating says the current product does not satisfy it.</div>
<div class="tw"><table>
<tr><th>Product</th><th>Category</th><th class="num">Rank</th><th class="num">Rating</th>
<th class="num">Ratings</th><th class="num">Price</th></tr>
$weak_rows
</table></div>

<h2>3 &middot; Cross-market price gaps (SA vs AE)</h2>
<div class="sub">Identical ASIN priced 15%+ apart after SAR/AED peg conversion at $peg.
Signals thin local competition on the expensive side.</div>
<div class="tw"><table>
<tr><th>Product</th><th class="num">Amazon.sa</th><th class="num">Amazon.ae</th>
<th class="num">Delta</th><th>Cheaper</th></tr>
$arb_rows
</table></div>

<h2>4 &middot; What this does not cover</h2>
<div class="tw"><table><tr><th>Source</th><th>Why it is missing</th></tr>$blocked_rows</table></div>
$misses
<div class="sub" style="margin-top:28px">Bestseller ranks are a 24h snapshot and rotate daily. Re-run before
acting &mdash; <code>python finder.py</code>. Star ratings stand in for review sentiment; full review text is
behind a per-ASIN bot check.</div>
</div>""")


def pill(v: float) -> str:
    cls = "p-hi" if v >= 66 else "p-mid" if v >= 40 else "p-lo"
    return f'<span class="pill {cls}">{v:.0f}</span>'


def render(cats, weak, arb, products, misses, out: Path) -> Path:
    e = html.escape
    cat_rows = "\n".join(
        f'<tr><td class="num">{i}</td><td class="cat">{e(r["category"].replace("-", " "))}</td>'
        f'<td class="num">{pill(r["opportunity"])}<div class="bar" style="width:{r["opportunity"]:.0f}%"></div></td>'
        f'<td class="num">{r["med_reviews"]:,.0f}</td><td class="num">{r["avg_rating"]}</td>'
        f'<td class="num">{r["weak_share"]*100:.0f}%</td><td class="num">{r["entrant_share"]*100:.0f}%</td>'
        f'<td class="num">{r["price_lo"]:,.0f} &ndash; {r["price_hi"]:,.0f}</td>'
        f'<td class="num">{r["gap_lo"]:,.0f} &rarr; {r["gap_hi"]:,.0f}</td></tr>'
        for i, r in enumerate(cats, 1))

    weak_rows = "\n".join(
        f'<tr><td class="t"><a href="{p["url"]}" target="_blank" rel="noopener">{e(p["title"][:110])}</a></td>'
        f'<td class="cat">{e(p["category"].replace("-", " "))}</td><td class="num">#{p["rank"]}</td>'
        f'<td class="num">{p["rating"]}</td><td class="num">{p["reviews"]:,}</td>'
        f'<td class="num">{p["currency"]} {p["price"]:,.0f}</td></tr>' for p in weak) or \
        '<tr><td colspan="6">No bestseller fell below 4.2 with enough ratings.</td></tr>'

    arb_rows = "\n".join(
        f'<tr><td class="t"><a href="{r["url"]}" target="_blank" rel="noopener">{e(r["title"][:110])}</a></td>'
        f'<td class="num">{r["sa"]:,.0f}</td><td class="num">{r["ae"]:,.0f}</td>'
        f'<td class="num">{pill(min(r["delta"], 100))}</td><td>{r["cheaper"]}</td></tr>' for r in arb) or \
        '<tr><td colspan="5">No ASIN charted in both markets &mdash; run with <code>--markets sa,ae</code>.</td></tr>'

    blocked_rows = "\n".join(f"<tr><td><b>{e(k)}</b></td><td>{e(v)}</td></tr>" for k, v in BLOCKED.items())
    miss_html = (f'<div class="note">Returned no parseable grid this run: {e(", ".join(misses))}</div>'
                 if misses else "")

    out.write_text(PAGE.substitute(
        n_products=f"{len(products):,}", n_cats=len(cats), n_weak=len(weak), n_arb=len(arb),
        top_cat=cats[0]["category"].replace("-", " ").title() if cats else "n/a",
        markets=", ".join(sorted({f"Amazon.{p['market']}" for p in products})) or "none",
        ts=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), peg=f"1 AED = {AED_TO_SAR} SAR",
        cat_rows=cat_rows, weak_rows=weak_rows, arb_rows=arb_rows,
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
    print("selftest OK")
    return 0


# --------------------------------------------------------------------------- cli
def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--markets", default="sa,ae", help="comma list of: sa, ae (default both)")
    p.add_argument("--categories", default=",".join(CATEGORIES), help="comma list (default all 15)")
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

    print(f"Fetching {len(markets) * len(cats)} bestseller pages...")
    products, misses = collect(markets, cats)
    if not products:
        print("Nothing fetched -- Amazon returned no parseable grid. Retry or check connectivity.",
              file=sys.stderr)
        return 1

    rows = score(category_stats(products))
    weak = weak_incumbents(products)
    arb = arbitrage(products) if len(markets) > 1 else []
    print(f"{len(products)} products -> {len(rows)} categories, {len(weak)} weak incumbents, {len(arb)} price gaps")
    if misses:
        print(f"No grid from: {', '.join(misses)}")

    out = render(rows, weak, arb, products, misses, a.out)
    print(f"Report: {out.resolve()}")
    for r in rows[:5]:
        print(f"  {r['opportunity']:5.1f}  {r['category']}")
    if a.open:
        webbrowser.open(out.resolve().as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
