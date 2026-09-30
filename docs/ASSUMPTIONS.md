# Assumptions & known unknowns (registry)

## Assumptions (safe defaults, changeable)
| # | Assumption | Where |
|---|---|---|
| A1 | Owner citizenship = UA; residence, tax residency, KYC documents, IP country are unset until the owner sets them (unset ⇒ UNKNOWN, never pass) | profile |
| A2 | Default payout requirement DIRECT_CRYPTO | profile |
| A3 | Orders reach platforms via an API channel (`api_trading` rule applies); set `automation_channel=ea` for EA bridges | profile/session |
| A4 | Internal buffer 30 % of each allowance, +20 % low rule confidence, +10 % stale quotes, + volatility; max risk/trade 1 % | SafetyPolicy |
| A5 | Stop loss mandatory for new positions (internal policy) | SafetyPolicy |
| A6 | Unknown weekend/overnight/news policy treated as prohibited | engine |
| A7 | Trading day = daily-loss reset of the firm; if none, 00:00 UTC | state |
| A8 | Replay: simultaneous MAE for open trades; day-start = first balance of day | replay |
| A9 | Monitoring every hour, ≥10 s per host, rules stale after 24 h | settings |
| A10 | Simulation may treat unverified rules as confirmed only with `--assume-confirmed` and says so | simulation |

## Known unknowns
| # | Unknown | Impact | Resolution path |
|---|---|---|---|
| U1 | Exact floating-PnL treatment and daily reference for most firms | daily limit math | owner verification from Terms/Rules pages |
| U2 | Automation (API/EA) permission per firm/platform | trading allowed at all | primary docs + firm support confirmation |
| U3 | Crypto networks for FTMO/The5ers (snippet-level) | payout filter | payout policy page / KYC dashboard |
| U4 | FundedNext, FundingPips, Breakout pages returned 403 | INSUFFICIENT_DATA | owner opens pages manually; add sources |
| U5 | Ukraine status on Bybit for HyroTrader | eligibility | Bybit primary docs |
| U6 | Platform adapter behaviour (client ids, partial fills, reconnect) | live execution | ADR-0005 |
| U7 | News calendar source | news blackout | add a provider; until then only firms with news ALLOWED can trade |
