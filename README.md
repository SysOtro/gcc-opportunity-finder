# GCC Product Opportunity Finder

Finds under-served product categories on GCC e-commerce by scoring live **Amazon.sa**,
**Amazon.ae** and **Noon.com** listings on review sentiment, competitive moat, price gaps and
competition density. Outputs a single self-contained HTML report.

No API keys. No paid data. One file, one dependency. ~2,200 live products per run.

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
absolute numbers drift. Each category is scored three times: combined (the GCC score), Amazon-only
and Noon-only. Divergence is the interesting part — beauty scored 26 on Amazon and 64 on Noon in a
recent run, which is a different business decision depending on where you list.

Noon exposes three signals Amazon does not, reported separately:

| Noon-only signal | Read |
|---|---|
| Competing listings | How many products fight for the query. `home` = 13.8M, `baby products` = 11.8K. |
| Median discount | Margin pressure. Heavy discounting means incumbents are buying their rank. |
| Sellers per 100 | Fragmentation. High = no entrenched winner; low = one seller owns it. |

## Report sections

1. **GCC category opportunity ranking** — 15 categories scored 0–100, with Amazon and Noon
   sub-scores side by side. `—` means that marketplace returned no data, never a fabricated zero.
2. **Noon competition depth** — listings, discount pressure and seller fragmentation.
3. **Weak incumbents** — bestsellers rated ≤4.2 with 30+ ratings. Proven demand, unhappy buyers.
   This is the shortlist worth sourcing against.
4. **Cross-market price gaps** — same ASIN priced 15%+ apart between Amazon SA and AE after peg
   conversion. Signals thin local competition on the expensive side.
5. **Caveats and coverage** — stated explicitly rather than silently omitted.

## Usage

```bash
python finder.py                                 # all 3 marketplaces, 15 categories
python finder.py --markets sa --open             # Amazon Saudi only, open when done
python finder.py --markets noon --categories toys,beauty
python finder.py --noon-delay 0.5                # faster, if you set JINA_API_KEY
python finder.py --selftest                      # asserts only, no network
```

## How Noon is reached

Noon sits behind Akamai and geo-fences non-GCC traffic — it **silently drops the connection**
rather than returning 403, which reads as a timeout. Driving a real browser does not help: it is an
IP-level block, so Chrome on the same connection fails identically.

The tool therefore tries `www.noon.com` **directly first** (fast, and what you get from a GCC IP or
VPN), then falls back to the [r.jina.ai](https://jina.ai/reader/) text proxy, which reaches Noon's
public catalog API from a permitted IP. Keyless that proxy allows 20 requests/minute, so Noon calls
are paced by `--noon-delay` (default 3.2s; 15 categories ≈ 48s). Set `JINA_API_KEY` to lift the cap
to 200/min and drop the delay.

This is a third-party dependency that can rate-limit or truncate a response. Failures degrade per
category into the report's caveats table instead of silently shrinking the dataset.

## Coverage and limits

- **Star ratings stand in for review sentiment.** Full review bodies need per-ASIN pages behind a
  bot check. The distribution of stars across a chart is the signal used.
- **Noon's listing counts are Noon's own reported match count** for the query — relative, not an
  audited catalogue census.
- **Bestseller ranks are a 24h snapshot** and rotate daily. Re-run before acting.
- Amazon slugs `computers`, `kitchen` and `sports` return HTTP 200 with an empty grid and are
  excluded; a few `.ae` slugs intermittently return no grid and are reported per run.
- SAR/AED conversion uses the USD peg (`AED_TO_SAR = 1.0212`), tunable at the top of `finder.py`.

## How it works

`requests` + stdlib `re` against Amazon's `p13n` bestseller grid, and stdlib `json` against Noon's
catalog API — no BeautifulSoup, no headless browser. Amazon's 30 pages fetch concurrently; Noon is
paced. Full run ≈ 2,200 products in about 70 seconds.

Run `python finder.py --selftest` for the network-free check: Amazon HTML parsing, Noon JSON
normalisation, percentile ties, currency conversion, the absent-marketplace `None` contract, and the
core scoring thesis (a cheap, badly-rated, low-review chart must outrank an entrenched, well-loved
one).

## Licence

MIT
