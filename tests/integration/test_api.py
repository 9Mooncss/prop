import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from propguard.app_context import AppContext
from propguard.config import Settings
from propguard.db.session import session_scope
from propguard.db.stores import SessionAudit, SqlAudit
from propguard.registry import service as reg

pytestmark = pytest.mark.integration
AUTH = {"Authorization": "Bearer tok-abcdef"}
NOAUTH = {"Authorization": ""}
FIX = Path(__file__).resolve().parents[1] / "fixtures" / "firm_demo.json"
SAMPLE = Path(__file__).resolve().parents[2] / "examples" / "sample_trades.csv"


@pytest.fixture
def client(db_url, tmp_path):
    st = Settings(database_url=db_url, data_dir=tmp_path, api_token=SecretStr("tok-abcdef"),
                  acceptance_marker=tmp_path / "acc.json")
    ctx = AppContext.create(st)
    with session_scope(ctx.sf) as s:
        reg.load_seed(s, json.loads(FIX.read_text()), SessionAudit(s))
    from propguard.api.app import create_app
    return TestClient(create_app(ctx), headers=AUTH), ctx




def test_health_metrics_pages(client):
    c, _ = client
    assert c.get("/health").json()["status"] == "ok"
    assert "propguard_kill_switches_active" in c.get("/metrics").text
    for u in ("/", "/firms/demo-firm", "/changes", "/api/firms", "/api/alerts", "/api/profile"):
        assert c.get(u).status_code == 200, u
    assert c.get("/firms/nope").status_code == 404


def test_mutations_require_token(client):
    c, _ = client
    assert c.post("/api/profile", json={"residence_country": "PL"}, headers=NOAUTH).status_code == 401
    assert c.post("/api/profile", json={"residence_country": "PL"}, headers=AUTH).json()["version"] == 2
    assert c.post("/api/profile", json={"residence_country": "Poland"}, headers=AUTH).status_code == 400
    assert c.post("/api/profile", json={"payout_requirement": "WHATEVER"}, headers=AUTH).status_code == 400


def test_profile_versions_and_default_citizenship(client):
    c, _ = client
    p = c.get("/api/profile").json()
    assert p["citizenship"] == "UA" and p["residence_country"] is None and p["tax_residency"] is None


def test_history_upload_recommendation_traceable(client):
    c, _ = client
    c.post("/api/profile", json={"residence_country": "PL", "tax_residency": "PL", "ip_location_country": "PL",
                                 "kyc_documents": [{"type": "passport", "country": "UA"}]}, headers=AUTH)
    r = c.post("/api/histories", files={"file": ("t.csv", SAMPLE.read_bytes(), "text/csv")},
               data={"account_size": "100000"}, headers=AUTH)
    assert r.status_code == 200, r.text
    hid = r.json()["id"]
    rec = c.post("/api/recommendations", json={"history_id": hid, "mc_paths": 200}, headers=AUTH).json()
    full = c.get(f"/api/recommendations/{rec['id']}").json()
    assert full["inputs"]["history"]["content_hash"] == r.json()["content_hash"]
    assert full["inputs"]["profile"]["version"] == 2
    top = full["results"][0]
    assert top["eligible"] and top["components"] and top["hard_filters"] and top["rules"]
    assert all(x["rule_id"].count("@v") == 1 for x in top["rules"])
    assert top["rule_compatibility"]["monte_carlo"]["disclaimer"]
    assert c.get(f"/recommendations/{rec['id']}").status_code == 200


def test_kill_switch_and_clear_requires_note(client):
    c, ctx = client
    from propguard.app_context import ensure_account
    with session_scope(ctx.sf) as s:
        ensure_account(s, "a1", "demo-firm", "two-step", "phase1", 100000)
    assert c.post("/api/accounts/a1/kill-switch", json={"reason": "stop"}, headers=AUTH).json()["active"]
    assert c.post("/api/accounts/a1/kill-switch/MANUAL/clear", json={"note": ""}, headers=AUTH).status_code == 400
    assert c.post("/api/accounts/a1/kill-switch/MANUAL/clear", json={"note": "reviewed ok"},
                  headers=AUTH).json()["cleared"]
    assert c.get("/accounts/a1").status_code == 200
    assert SqlAudit(ctx.sf).verify_chain() == (True, None)


def test_rule_verify_via_api_and_wallet_confirmation(client):
    c, _ = client
    d = c.get("/api/firms/demo-firm").json()
    rid = next(r["id"] for r in d["rules"] if r["kind"] == "daily_loss_limit")
    assert c.post(f"/api/rules/{rid}/verify", json={"note": "checked"}, headers=NOAUTH).status_code == 401
    v = c.post(f"/api/rules/{rid}/verify", json={"note": "checked"}, headers=AUTH).json()
    assert v["status"] == "CONFIRMED"
    bad = c.post(f"/api/rules/{rid}/verify", json={"params": {"pct": 500}}, headers=AUTH)
    assert bad.status_code == 400  # superseded rule id / invalid params never accepted
    addr = "TQ5s8f3x9ExampleAddressForTestsOnly77"
    w = c.post("/api/wallets", json={"label": "main", "network": "TRC20", "currency": "USDT", "address": addr},
               headers=AUTH).json()
    listed = c.get("/api/wallets").json()[0]
    assert listed["address"] != addr and not listed["confirmed_by_owner"]  # masked until confirmed
    assert c.post(f"/api/wallets/{w['id']}/confirm", json={"last6": "000000"}, headers=AUTH).status_code == 400
    assert c.post(f"/api/wallets/{w['id']}/confirm", json={"last6": addr[-6:]}, headers=AUTH).json()["confirmed"]


def test_no_live_or_payment_endpoints(client):
    c, _ = client
    paths = {r.path for r in c.app.routes}
    assert not any(k in p for p in paths for k in ("live", "pay", "purchase", "sign", "withdraw"))


def test_reads_require_token_when_configured(client):
    c, _ = client
    for u in ("/api/profile", "/api/audit", "/api/accounts", "/api/wallets", "/api/firms"):
        assert c.get(u, headers=NOAUTH).status_code == 401, u
    r = c.get("/", headers=NOAUTH, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/ui/login"
    assert c.get("/health", headers=NOAUTH).status_code == 200
    assert c.get("/ui/login", headers=NOAUTH).status_code == 200
    login = c.post("/ui/login", data={"token": "tok-abcdef"}, headers=NOAUTH, follow_redirects=False)
    assert login.cookies.get("pg_token")
