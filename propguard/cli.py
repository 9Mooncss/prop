"""propguard command-line interface."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from propguard.app_context import (
    AppContext,
    current_profile,
    ensure_account,
    save_account_status,
    status_from_session,
    update_profile,
)
from propguard.db.session import session_scope
from propguard.logging_setup import setup_logging


def _ctx() -> AppContext:
    ctx = AppContext.create()
    setup_logging(ctx.settings.log_level, ctx.settings.log_json)
    return ctx


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def cmd_db(args) -> None:
    from propguard.db.migrate import current, downgrade, upgrade
    url = _ctx().settings.database_url
    if args.action == "upgrade":
        upgrade(url)
        print(f"schema at {current(url)}")
    elif args.action == "downgrade":
        downgrade(url, args.revision)
    else:
        print(current(url))


def cmd_seed(args) -> None:
    from propguard.db.stores import SessionAudit
    from propguard.registry.service import load_seed_dir
    ctx = _ctx()
    with session_scope(ctx.sf) as s:
        _print(load_seed_dir(s, Path(args.dir), SessionAudit(s)))


def cmd_monitor(args) -> None:
    from propguard.monitor.pipeline import run_monitor
    ctx = _ctx()
    with session_scope(ctx.sf) as s:
        outs = run_monitor(s, ctx.fetcher(), ctx.analyzer(s), ctx.notifier, due_after_s=0 if args.force else 3600)
        _print([o.__dict__ for o in outs])


def cmd_profile(args) -> None:
    ctx = _ctx()
    with session_scope(ctx.sf) as s:
        if args.action == "show":
            p = current_profile(s)
        else:
            changes = {}
            for kv in args.set or []:
                k, _, v = kv.partition("=")
                changes[k] = json.loads(v) if v.startswith(("[", "{")) else v
            p = update_profile(s, changes)
        _print({c.name: getattr(p, c.name) for c in p.__table__.columns})


def cmd_history(args) -> None:
    from propguard.db.models import TradeHistory
    from propguard.recommender.replay import parse_trades_csv
    from propguard.recommender.service import trades_to_json
    ctx = _ctx()
    raw = Path(args.file).read_text(encoding="utf-8-sig")
    trades = parse_trades_csv(raw)
    with session_scope(ctx.sf) as s:
        h = TradeHistory(name=args.name or Path(args.file).name, content_hash=hashlib.sha256(raw.encode()).hexdigest(),
                         account_size=args.account_size, trades=trades_to_json(trades))
        s.add(h)
        s.flush()
        print(json.dumps({"history_id": h.id, "trades": len(trades)}))


def cmd_recommend(args) -> None:
    from propguard.db.models import TradeHistory
    from propguard.recommender.service import recommend
    ctx = _ctx()
    with session_scope(ctx.sf) as s:
        hist = s.get(TradeHistory, args.history) if args.history else None
        rec = recommend(s, current_profile(s), hist, mc_paths=args.paths)
        rows = [{"firm": r["firm"], "program": r["program"], "size": r["account_size"], "eligible": r["eligible"],
                 "conditional": r["conditional"], "total": r["total"],
                 "failed_filters": [h["name"] for h in r["hard_filters"] if h["result"] == "FAIL"],
                 "rule_compat": (r.get("rule_compatibility") or {}).get("score")} for r in rec.results]
        _print({"recommendation_id": rec.id, "results": rows})


def cmd_simulate(args) -> None:
    from propguard.registry.service import ruleset_for
    from propguard.simulation import run_simulated_challenge
    ctx = _ctx()
    with session_scope(ctx.sf) as s:
        rs = ruleset_for(s, args.firm, args.program, args.phase, args.size)
    rep = run_simulated_challenge(rs, days=args.days, seed=args.seed, assume_rules_confirmed=args.assume_confirmed)
    _print(rep.to_dict())


def cmd_paper(args) -> None:
    """Run a PAPER account on the simulated broker with persistent SQL stores (dashboard shows it)."""
    from propguard.db.stores import sql_stores
    from propguard.registry.service import ruleset_for
    from propguard.simulation import run_simulated_challenge
    ctx = _ctx()
    with session_scope(ctx.sf) as s:
        ensure_account(s, args.account, args.firm, args.program, args.phase, args.size)
        rs = ruleset_for(s, args.firm, args.program, args.phase, args.size)
    stores = sql_stores(ctx.sf)
    rep = run_simulated_challenge(rs, days=args.days, seed=args.seed, assume_rules_confirmed=args.assume_confirmed,
                                  stores=stores, account_id=args.account,
                                  on_status=lambda sess: save_account_status(ctx.sf, args.account,
                                                                             status_from_session(sess)))
    _print(rep.to_dict())


def cmd_acceptance(args) -> None:
    """Run the Risk Engine acceptance suite; write the marker only on success."""
    from propguard.execution.live_gate import code_fingerprint
    ctx = _ctx()
    root = Path(__file__).resolve().parent.parent
    tests = root / "tests"
    if not tests.exists():
        sys.exit("tests/ not found next to the package; run from a source checkout")
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-m", "risk", str(tests)], cwd=root)
    marker = ctx.settings.acceptance_marker
    marker.parent.mkdir(parents=True, exist_ok=True)
    data = {"passed": r.returncode == 0, "fingerprint": code_fingerprint(),
            "ran_at": datetime.now(timezone.utc).isoformat()}
    marker.write_text(json.dumps(data))
    _print(data)
    sys.exit(r.returncode)


def cmd_live(args) -> None:
    """Explicit, per-account LIVE approval. Requires env opt-in, passing acceptance suite, and the phrase."""
    from propguard.db.models import TradingAccount
    from propguard.db.stores import SessionAudit
    from propguard.execution.live_gate import CONFIRM_PHRASE, code_fingerprint
    ctx = _ctx()
    gate = ctx.live_gate()
    with session_scope(ctx.sf) as s:
        a = s.get(TradingAccount, args.account)
        if a is None:
            sys.exit("unknown account")
        if args.action == "disable":
            a.mode, a.live_approved = "PAPER", False
            SessionAudit(s).record("live.disabled", a.id, {})
            print("account set to PAPER")
            return
        problems = []
        if not ctx.settings.live_allowed_env:
            problems.append("set PROPGUARD_ALLOW_LIVE=true and PROPGUARD_EXECUTION_MODE=LIVE_ALLOWED")
        if not gate.acceptance_ok():
            problems.append("run `propguard acceptance` successfully on this exact code first")
        if a.adapter == "simulated":
            problems.append("account uses the simulated adapter; configure a live adapter first")
        if args.confirm != CONFIRM_PHRASE:
            problems.append(f'pass --confirm "{CONFIRM_PHRASE}"')
        if problems:
            sys.exit("LIVE not enabled:\n - " + "\n - ".join(problems))
        a.mode, a.live_approved = "LIVE", True
        a.live_approved_at, a.live_approval_fingerprint = datetime.now(timezone.utc), code_fingerprint()
        SessionAudit(s).record("live.enabled", a.id, {"fingerprint": a.live_approval_fingerprint})
        print(f"LIVE enabled for {a.id} (valid only for code fingerprint {a.live_approval_fingerprint})")


def cmd_rules(args) -> None:
    from propguard.db.models import Rule
    from propguard.db.stores import SessionAudit
    from propguard.registry.service import verify_rule
    ctx = _ctx()
    with session_scope(ctx.sf) as s:
        if args.action == "list":
            q = select(Rule).where(Rule.is_current.is_(True))
            if args.firm:
                from propguard.db.models import Firm
                q = q.join(Firm, Firm.id == Rule.firm_id).where(Firm.slug == args.firm)
            _print([{"id": r.id, "key": r.rule_key, "status": r.interpretation_status, "params": r.params,
                     "notes": r.interpretation_notes, "text": r.raw_text} for r in s.scalars(q)])
        else:
            params = json.loads(args.params) if args.params else None
            r = verify_rule(s, args.id, args.actor, params, args.note or "", SessionAudit(s))
            print(f"rule {r.rule_key} v{r.version} CONFIRMED")


def cmd_killswitch(args) -> None:
    from propguard.db.stores import SqlKillSwitchStore, SqlAudit
    from propguard.risk.policy import KillSwitchKind
    ctx = _ctx()
    ks = SqlKillSwitchStore(ctx.sf)
    if args.action == "list":
        _print([k.value for k in ks.active(args.account)])
    elif args.action == "activate":
        ks.activate(args.account, KillSwitchKind.MANUAL, args.note or "manual", {"actor": "cli"})
        SqlAudit(ctx.sf).record("killswitch.activate", args.account, {"kind": "MANUAL", "actor": "cli"})
    else:
        if not args.note or len(args.note) < 5:
            sys.exit("--note (>=5 chars) required")
        ok = ks.clear(args.account, KillSwitchKind(args.kind), "cli-owner", args.note)
        SqlAudit(ctx.sf).record("killswitch.clear", args.account, {"kind": args.kind, "ok": ok, "note": args.note})
        print("cleared" if ok else "not active")


def cmd_audit(args) -> None:
    from propguard.db.stores import SqlAudit
    ok, bad = SqlAudit(_ctx().sf).verify_chain()
    print("audit chain OK" if ok else f"audit chain BROKEN at id {bad}")
    sys.exit(0 if ok else 1)


def cmd_serve(args) -> None:
    import uvicorn
    ctx = _ctx()
    uvicorn.run("propguard.api.app:create_app", factory=True, host=args.host or ctx.settings.bind_host,
                port=args.port or ctx.settings.bind_port, log_config=None)


def cmd_worker(args) -> None:
    from propguard.worker import run_forever
    run_forever(_ctx(), once=args.once)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="propguard")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("db"); a.add_argument("action", choices=["upgrade", "downgrade", "current"])
    a.add_argument("--revision", default="-1"); a.set_defaults(fn=cmd_db)
    a = sub.add_parser("seed"); a.add_argument("action", choices=["load"]); a.add_argument("--dir", default="seed/firms")
    a.set_defaults(fn=cmd_seed)
    a = sub.add_parser("monitor"); a.add_argument("action", choices=["run"]); a.add_argument("--force", action="store_true")
    a.set_defaults(fn=cmd_monitor)
    a = sub.add_parser("profile"); a.add_argument("action", choices=["show", "set"])
    a.add_argument("set", nargs="*", help="field=value (JSON for lists)"); a.set_defaults(fn=cmd_profile)
    a = sub.add_parser("history"); a.add_argument("action", choices=["import"]); a.add_argument("file")
    a.add_argument("--account-size", type=float, required=True); a.add_argument("--name"); a.set_defaults(fn=cmd_history)
    a = sub.add_parser("recommend"); a.add_argument("--history", type=int); a.add_argument("--paths", type=int, default=1000)
    a.set_defaults(fn=cmd_recommend)
    for name, fn in (("simulate", cmd_simulate), ("paper", cmd_paper)):
        a = sub.add_parser(name)
        if name == "paper":
            a.add_argument("action", choices=["run"]); a.add_argument("--account", default="paper-1")
        a.add_argument("--firm", required=True); a.add_argument("--program", required=True)
        a.add_argument("--phase", default="phase1"); a.add_argument("--size", type=float, default=100000)
        a.add_argument("--days", type=int, default=20); a.add_argument("--seed", type=int, default=1)
        a.add_argument("--assume-confirmed", action="store_true",
                       help="SIMULATION ONLY: treat unverified rules as confirmed")
        a.set_defaults(fn=fn)
    a = sub.add_parser("acceptance"); a.set_defaults(fn=cmd_acceptance)
    a = sub.add_parser("live"); a.add_argument("action", choices=["enable", "disable"]); a.add_argument("--account", required=True)
    a.add_argument("--confirm", default=""); a.set_defaults(fn=cmd_live)
    a = sub.add_parser("rules"); a.add_argument("action", choices=["list", "verify"]); a.add_argument("--firm")
    a.add_argument("--id", type=int); a.add_argument("--actor", default="owner"); a.add_argument("--params")
    a.add_argument("--note"); a.set_defaults(fn=cmd_rules)
    a = sub.add_parser("killswitch"); a.add_argument("action", choices=["list", "activate", "clear"])
    a.add_argument("--account", required=True); a.add_argument("--kind", default="MANUAL"); a.add_argument("--note")
    a.set_defaults(fn=cmd_killswitch)
    a = sub.add_parser("audit"); a.add_argument("action", choices=["verify"]); a.set_defaults(fn=cmd_audit)
    a = sub.add_parser("serve"); a.add_argument("--host"); a.add_argument("--port", type=int); a.set_defaults(fn=cmd_serve)
    a = sub.add_parser("worker"); a.add_argument("--once", action="store_true"); a.set_defaults(fn=cmd_worker)
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
