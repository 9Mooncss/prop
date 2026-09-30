# Prop Firm Research (Ukraine-citizen owner)

Date of research: 2026-09-29. Workstream: Research / Prop-Firm. Seed data: `seed/firms/*.json`.

## Disclaimer
Prop-firm rules, country lists, payout rails and prices change frequently and without notice. Everything here is a point-in-time snapshot and MUST be re-verified by the monitoring service from primary sources before any purchase or live session. Nothing here is advice to circumvent KYC or country restrictions. No firm is marked VERIFIED.

## Method
- Located official pages via web search restricted to firm domains, then fetched them with a page-to-markdown fetch tool that returns a model-generated summary (not raw HTML). Quotes are therefore near-verbatim and should be re-checked against raw pages.
- Where an official page returned HTTP 403/404, the fact was taken from the search-result snippet of that official page and labelled "search-result snippet" in `sources[].title`. These are lower confidence.
- No CAPTCHA, login or geoblock was bypassed. Community sources were not used for rules (no risk signals collected).
- Aggregator sites (propfirmmap etc.) surfaced in search were ignored for facts.
- Owner is a Ukrainian citizen; residence/tax residency is not assumed.

## Summary table

| Firm | Proposed status | Ukraine citizens | Ukraine residents | Payout classification | Crypto networks | EA/API policy | Platforms | Key unknowns |
|---|---|---|---|---|---|---|---|---|
| FTMO | WATCHLIST | ALLOWED (country not restricted; occupied regions are) | ALLOWED (except Crimea, Sevastopol, Donetsk, Kherson, Luhansk, Zaporizhzhia) | DIRECT_CRYPTO (FAQ names no provider) | USDT TRC20, USDC ERC20 (+BTC/ETH/LTC), snippet | EA conditional (<2000 requests/day); API unknown | MT4, MT5, cTrader, TradingView | T&C PDF, KYC, refund, floating PnL, VPN/VPS |
| The5ers | WATCHLIST | ALLOWED | ALLOWED (except occupied regions) | DIRECT_CRYPTO (Rise listed as a separate method) | USDT TRC20, USDC ERC20, ETH, LTC (snippet) | EA only with own source code; copy/HFT prohibited; API unknown | not confirmed | KYC, platforms, min days, reset time, T&C |
| Alpha Capital Group | WATCHLIST | ALLOWED | ALLOWED (except sanctioned regions) | CRYPTO_VIA_PROVIDER (Rise only; firm says no direct crypto) | via Rise, n/a | unknown | not confirmed | challenge rules, automation, KYC |
| FundedNext | INSUFFICIENT_DATA | UNKNOWN (not in snippet list) | UNKNOWN | UNKNOWN (USDT ERC20/TRC20, Confirmo, Rise listed) | ERC20, TRC20 (snippet) | EA <$50k accounts only (snippet); copy limited | MT5 | pages 403; everything unconfirmed |
| FundingPips | INSUFFICIENT_DATA | UNKNOWN | UNKNOWN | UNKNOWN (crypto USDT/USDC + Rise) | ERC20, TRC20 (snippet) | unknown | unknown | pages 403 |
| E8 Markets | EXCLUDED | PROHIBITED (Ukraine on restricted list) | PROHIBITED | CRYPTO_VIA_PROVIDER (Rise, snippet; mixed) | n/a | not researched | n/a | basis (citizenship vs residence doc) |
| HyroTrader | WATCHLIST | UNKNOWN | UNKNOWN | UNKNOWN (USDT/USDC) | not stated | unknown | Bybit-linked | Bybit's Ukraine status, payout rail |
| Breakout (Kraken) | INSUFFICIENT_DATA | UNKNOWN | UNKNOWN | UNKNOWN (USDC on Ethereum, snippet) | ERC20 | unknown | proprietary | country list, rules (403) |

Not researched (effort budget / diminishing returns): Blue Guardian, Maven, Goat Funded Trader, Crypto Fund Trader, FundingTraders.

## Per-firm notes

### FTMO
- Ukraine: FAQ "Who can join FTMO?" lists "Ukraine (restrictions are limited to the following regions: Crimea, Sevastopol, Donetsk, Kherson, Luhansk, and Zaporizhzhia)". Citizen with residence outside those regions appears eligible; verify.
- 2-Step: targets 10% / 5%; max daily loss 5% recalculated daily at 00:00 CE(S)T; max loss 10% static; min 4 trading days; no time limit shown. 1-Step: 10% target, 3% daily, 10% max.
- Payout: bank wire, Visa Direct/Mastercard, Skrill, cryptocurrencies; crypto min $50; request from day 14; split 80% (2-Step, up to 90%), 90% (1-Step). Coin/network list came from a search snippet.
- Automation: EAs allowed but >2,000 server requests/day is hyperactivity; no third-party access; news restrictions; VPN/VPS answer not read.

### The5ers
- Ukraine country absent from both restricted lists; Crimea, Donetsk, Kherson, Luhansk, Zaporizhzhia listed. FAQ and disclaimer lists differ (conflict recorded).
- High Stakes: 8% (Classic) or 10% (New) then 5%; daily loss 3% of prior day's closing balance or equity, whichever higher (a search summary said 5%: conflict recorded); max loss 10% of initial balance; consistency 50% on funded.
- Payout: Rise, Crypto (wallet, $1,500 per withdrawal limit), bank; 3.5% commission; biweekly.

### Alpha Capital Group
- Nationals/residents restrictions list excludes Ukraine but excludes sanctioned Ukrainian areas.
- Explicit: "We do not facilitate withdrawals directly through cryptocurrency" - crypto only via Rise. Classified CRYPTO_VIA_PROVIDER.

### FundedNext / FundingPips / Breakout
Direct fetches blocked (403). Only snippets; see seed files. Treat as unverified leads.

### E8 Markets
Ukraine is on the restricted lists (Classic + Perpetuals, Futures); country determined by verification document. EXCLUDED; do not attempt workarounds.

### HyroTrader
Crypto-only firm on Bybit. Rules: 10% target, 4% daily, 6% max, 5 min days, unlimited time, deposit refundable. Site states no country restrictions, but Bybit's own restrictions may block live trading; check Ukraine on Bybit.

## Sources
See `sources[]` in each `seed/firms/<slug>.json`. Primary domains: ftmo.com, the5ers.com, help.alphacapitalgroup.uk, help.fundednext.com, help.fundingpips.com, intercom.help/e8, help.e8markets.com, hyrotrader.com, breakoutprop.com.

## Access problems
403: help.fundednext.com, fundednext.com/faq, fundingpips.com/trading-objectives, help.fundingpips.com, breakoutprop.com/program-rules, help.e8markets.com. 404 on several guessed URLs (the5ers help subdomain crypto page).

## How the system uses this research
Seed files are loaded as **UNVERIFIED** rules (confidence 0.6) with evidence rows (URL, doc type, fragment,
hash, retrieval time). Rules whose extracted params were ambiguous (e.g. FTMO `max_loss.basis =
"balance_or_equity_unconfirmed"`, `includes_floating = null`) are stored as **UNCERTAIN** with the raw values
kept. The monitor captures raw baselines of every primary source and upgrades matching fragments to
SOURCE_MATCHED; the owner then confirms each rule. Until then the Risk Engine blocks new risk for these firms
and the recommender filters them out — the table above is a lead list, not a green light.
