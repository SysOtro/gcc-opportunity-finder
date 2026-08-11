# GCC Product Opportunity Finder

Finds under-served product categories on GCC e-commerce by scoring live Amazon.sa and
Amazon.ae bestseller charts on three signals: **review sentiment**, **competitive moat**, and
**price gaps**. Outputs a single self-contained HTML report.

No API keys. No paid data. One file, one dependency.

```bash
pip install -r requirements.txt
python finder.py --open
```

## What it actually measures

A bestseller chart is a list of things people already buy. The opportunity is not the chart —
it is the **weak spots inside it**: products selling well *despite* poor ratings, categories where
nobody has built a review moat, and price bands nobody occupies.

| Signal | Weight | Read |
|---|---|---|
| Low review moat | 35% | Median ratings count of the chart. Low = incumbents are beatable. |
| Weak sentiment | 30% | Share of bestsellers rated under 4.2. High = demand is real, product isn't. |
| Price gap | 20% | Largest empty band in the p10–p90 price body. Room to position. |
| Newcomer share | 15% | Chart entries with under 100 ratings. Proves a new seller can rank. |

All four are **percentiles within the run**, not fixed thresholds — the ranking stays valid as
Amazon's absolute numbers drift.

## Report sections

1. **Category opportunity ranking** — all 15 categories scored 0–100, with the price band and
   the biggest empty gap in it.
2. **Weak incumbents** — bestsellers rated ≤4.2 with 30+ ratings. Proven demand, unhappy buyers.
   This is the shortlist worth sourcing against.
3. **Cross-market price gaps** — same ASIN priced 15%+ apart between SA and AE after peg
   conversion. Signals thin local competition on the expensive side.
4. **What is not covered** — stated explicitly rather than silently omitted.

## Usage

```bash
python finder.py                                 # both markets, 15 categories
python finder.py --markets sa --open             # Saudi only, open when done
python finder.py --categories electronics,beauty,health
python finder.py --out weekly.html
python finder.py --selftest                      # asserts only, no network
```

## Coverage and limits

- **Noon.com is not fetched.** Every endpoint sits behind Cloudflare and refuses
  datacenter/non-GCC IPs (`ConnectionError` / `ReadTimeout`). The report names this instead of
  shipping half the market as if it were complete.
- **Star ratings stand in for review sentiment.** Full review bodies need per-ASIN pages behind a
  bot check. The distribution of stars across a chart is the signal used.
- **Bestseller ranks are a 24h snapshot** and rotate daily. Re-run before acting.
- Category slugs `computers`, `kitchen` and `sports` return HTTP 200 with an empty grid on
  amazon.sa and are excluded; a handful of `.ae` slugs intermittently return no grid and are
  reported per run rather than silently dropped.
- SAR/AED conversion uses the USD peg (`AED_TO_SAR = 1.0212`), tunable at the top of `finder.py`.

## How it works

`requests` + stdlib `re` against the `p13n` bestseller grid markup — no BeautifulSoup, no headless
browser. 30 pages fetched concurrently, ~730 products per full run in about 20 seconds.

Run `python finder.py --selftest` to check parsing, percentile maths, currency conversion and the
core scoring thesis (a cheap, badly-rated, low-review chart must outrank an entrenched, well-loved
one) without touching the network.

## Licence

MIT
