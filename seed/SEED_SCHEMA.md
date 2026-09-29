# Seed file schema (`seed/firms/<slug>.json`)

One JSON file per prop firm. Loaded by `propguard seed load`. Every critical fact must
reference one or more `sources[].id` via an `evidence` array. Unknown -> use "UNKNOWN" / null,
never guess.

```jsonc
{
  "slug": "example-firm",                      // lowercase, dash separated
  "name": "Example Firm",
  "official_url": "https://example.com",
  "proposed_status": "VERIFIED|WATCHLIST|INSUFFICIENT_DATA|EXCLUDED",
  "status_reason": "short explanation",
  "researched_at": "2026-09-29",               // date research was performed
  "jurisdiction": {
    "prohibited_countries": ["IR", "KP"],      // ISO-3166 alpha-2, as listed by the firm
    "ukraine_citizens": "ALLOWED|PROHIBITED|UNKNOWN",
    "ukraine_residents": "ALLOWED|PROHIBITED|UNKNOWN",
    "restriction_basis": "citizenship|residence|both|ip|unknown",
    "notes": "",
    "evidence": ["s1"]
  },
  "kyc": {
    "required_before": "payout|funded|purchase|unknown",
    "documents": ["passport", "proof_of_address"],
    "notes": "",
    "evidence": ["s2"]
  },
  "payout": {
    "classification": "DIRECT_CRYPTO|CRYPTO_VIA_PROVIDER|FIAT_ONLY|UNKNOWN",
    "methods": [
      {"rail": "crypto_wallet|rise|deel|bank|other", "provider": null,
       "currencies": ["USDT", "USDC"], "networks": ["TRC20", "ERC20"],
       "min_amount": null, "max_amount": null, "fee": null, "processing_time": null,
       "kyc_required": true, "evidence": ["s3"]}
    ],
    "frequency": "e.g. biweekly / on-demand / unknown",
    "profit_split_pct": 80,
    "evidence": ["s3"]
  },
  "platforms": ["MT5", "cTrader", "DXtrade", "MatchTrader", "TradeLocker", "proprietary"],
  "automation": {
    "ea_bots": "ALLOWED|PROHIBITED|CONDITIONAL|UNKNOWN",
    "api_trading": "ALLOWED|PROHIBITED|CONDITIONAL|UNKNOWN",
    "copy_trading": "ALLOWED|PROHIBITED|CONDITIONAL|UNKNOWN",
    "vps_vpn": "ALLOWED|PROHIBITED|CONDITIONAL|UNKNOWN",
    "hft": "ALLOWED|PROHIBITED|CONDITIONAL|UNKNOWN",
    "conditions": "text of conditions",
    "evidence": ["s4"]
  },
  "programs": [
    {
      "slug": "two-step-100k",
      "name": "Two-Step Challenge",
      "platforms": ["MT5"],
      "account_sizes": [{"size": 100000, "price_usd": 499, "currency": "USD"}],
      "refund": "fee refunded with first payout | none | unknown",
      "phases": [
        {
          "name": "phase1",                   // phase1 | phase2 | funded | evaluation
          "rules": [
            {"type": "profit_target", "params": {"pct": 8}, "text": "verbatim/near-verbatim rule text", "evidence": ["s5"]},
            {"type": "daily_loss_limit", "params": {"pct": 5, "basis": "start_of_day_balance_or_equity_max", "includes_floating": true, "reset_time": "00:00", "reset_tz": "Europe/Prague"}, "text": "...", "evidence": ["s5"]},
            {"type": "max_loss", "params": {"pct": 10, "mode": "static|trailing|eod_trailing|trailing_lock_at_initial", "basis": "equity|balance"}, "text": "...", "evidence": ["s5"]},
            {"type": "min_trading_days", "params": {"days": 4}, "text": "...", "evidence": ["s5"]},
            {"type": "max_duration", "params": {"days": null}, "text": "unlimited", "evidence": ["s5"]}
          ]
        }
      ]
    }
  ],
  "sources": [
    {"id": "s1", "url": "https://example.com/terms", "title": "Terms and Conditions",
     "doc_type": "TERMS|TRADING_RULES|FAQ|KYC_POLICY|PAYOUT_POLICY|RESTRICTED_COUNTRIES|PLATFORM_RULES|MARKETING|COMMUNITY",
     "retrieved_at": "2026-09-29T12:00:00Z", "fragment": "short relevant quote (<= 400 chars)"}
  ],
  "conflicts": [
    {"field": "daily_loss_limit.pct", "values": ["5", "4"], "sources": ["s1", "s5"], "notes": ""}
  ],
  "risk_signals": [
    {"source": "s9", "summary": "community reports of delayed payouts (unverified)"}
  ],
  "unknowns": ["list of things that could not be confirmed"]
}
```

Rule `type` values (see docs/RULE_SCHEMA.md for full param definitions):
profit_target, daily_loss_limit, max_loss, min_trading_days, max_duration, inactivity_limit,
leverage, max_lot_size, max_open_positions, instrument_restrictions, overnight_holding,
weekend_holding, news_trading, ea_policy, api_trading, copy_trading, consistency_rule,
prohibited_strategies, hft_restriction, latency_arbitrage, ip_vps_restriction, scaling,
payout_eligibility, payout_frequency, profit_split, refund_policy, trading_day,
cost_treatment, platform_requirement, stop_loss_required, max_risk_per_trade.
Unknown kinds may be emitted as `{"type": "custom", "params": {"kind": "..."}}`.
