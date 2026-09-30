# ADR-0005: No live platform adapter in the MVP; adapters isolated behind BrokerAdapter

Status: accepted

Findings (2026-09-29, primary docs):
* MetaTrader 5 — the official `MetaTrader5` Python package obtains data "via interprocessor communication
  directly from the MetaTrader 5 terminal" (mql5.com docs) and ships Windows-only wheels (PyPI). A Linux/macOS
  host therefore needs a Windows machine/VM (or Wine bridge) running the terminal. Must be an isolated bridge
  process; not bundled.
* cTrader Open API — language-neutral, JSON or Protobuf messages, no terminal required (help.ctrader.com/open-api);
  best candidate for a native macOS/Linux adapter. Requires app registration + OAuth-style account consent.
* DXtrade / Match-Trader / TradeLocker — broker-deployed APIs; availability depends on the prop firm.
* Prop-firm rules decide whether automation is allowed at all (`api_trading` / `ea_policy`); for all
  researched firms this is UNKNOWN or CONDITIONAL today, so the engine denies automated trading there anyway.

Decision: ship interfaces + simulator; implement a live adapter only after (a) the chosen firm's automation
rules are CONFIRMED for that platform, (b) platform docs for order idempotency / client ids / fills are
verified, (c) the adapter passes the simulator-equivalent integration suite. Adapter must request read+trade
only, never withdrawal scope. Sources: https://www.mql5.com/en/docs/python_metatrader5 ,
https://pypi.org/project/mt5linux/ , https://help.ctrader.com/open-api/
