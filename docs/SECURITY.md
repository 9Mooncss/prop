# Security

## Threat model (single owner, self-hosted)

Assets: the challenge/account itself (rule violations), broker/API credentials, owner identity/KYC data,
wallet addresses, the integrity of rule data and audit trail. Adversaries: accidental misuse, bugs, a
compromised/hallucinating LLM output, hostile web content in monitored pages, network attackers against the
dashboard, supply chain.

## Controls

| Concern | Control |
|---|---|
| Secrets in code/git | none committed; `.env` git-ignored; `.env.example` has no values; `pydantic.SecretStr` for all secrets |
| Secrets in logs | `logging_setup.redact`: API-key/token/bearer/password/cookie patterns, private keys, 64-hex, seed-phrase-like word runs, URL credentials, plus registered literal secret values; JSON logs pass through redaction |
| Secrets to LLM | only the changed fragment (≤ 4 kB) after redaction is sent; never credentials, never full pages; LLM disabled by default |
| LLM authority | output stored only as a PENDING `RuleChange` proposal; cannot confirm rules, clear kill switches, change limits; `risk/` and `execution/` never import `llm/` |
| Order bypass | only `BrokerAdapter.submit(ApprovedOrder)`; HMAC (per-process random key) over all order fields + decision id + adapter + expiry; single-use; 5 s TTL; subclasses cannot override `submit`; static AST test |
| Accidental LIVE | PAPER_ONLY default; LIVE needs `PROPGUARD_ALLOW_LIVE=true` **and** `PROPGUARD_EXECUTION_MODE=LIVE_ALLOWED` **and** a passing acceptance marker for the current code fingerprint **and** a per-account approval via CLI with the phrase `I UNDERSTAND THIS SENDS REAL ORDERS` (bound to the code fingerprint; any code change revokes it). No API endpoint can enable LIVE. Checked at construction and before every submission |
| Broker permissions | adapters must use the minimum scope: read + trade, never withdrawal; document per adapter |
| Payments / signing | not implemented by design: no purchase, payment or blockchain-signing code paths exist |
| Wallet addresses | stored, masked in lists (`TQ5s8f…Only77`), confirmation requires re-typing the last 6 chars; audited |
| Dashboard auth | bound to 127.0.0.1 by default (compose too); mutations require `Authorization: Bearer <PROPGUARD_API_TOKEN>` (constant-time compare) or the `pg_token` HttpOnly SameSite=Strict cookie; without a token only loopback clients may mutate. For remote access use a VPN/SSH tunnel or a TLS reverse proxy with its own auth |
| Audit integrity | append-only, SHA-256 hash chain (`propguard audit verify`); advisory lock on PostgreSQL prevents chain forks |
| Web content | HTML parsed with BeautifulSoup (no JS execution); scripts/iframes dropped; fetched text is data, never instructions |
| Scraping ethics | honest User-Agent, robots.txt respected, ≥ 10 s per host, conditional requests; CAPTCHA/anti-bot/login pages are recorded as BLOCKED and never bypassed |
| Container | non-root user (uid 10001), slim base, DB not exposed on host, secrets via env_file |

## Compliance boundaries (hard rules)

The system never helps circumvent KYC/AML, sanctions, citizenship/residency restrictions, geoblocks, VPN/VPS
bans, EA/bot/API/copy-trading/HFT bans or platform restrictions. Eligibility checks only *report*; the actual IP
country is a profile input used to flag restrictions, not something to change.

## Operational guidance

* Rotate `PROPGUARD_API_TOKEN` and broker keys periodically; prefer OS keychain for local installs
  (`pip install ".[keyring]"`, planned adapter hook).
* Keep `.env` mode `600`. Back up the DB encrypted (docs/DEPLOYMENT.md).
* Run `propguard audit verify` after restores.
