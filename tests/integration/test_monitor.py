import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from propguard.db.models import Alert, Evidence, LLMCall, Rule, RuleChange, Source
from propguard.db.session import session_scope
from propguard.db.stores import SessionAudit
from propguard.llm.client import ChangeAnalyzer, ChangeProposal
from propguard.monitor.fetch import Fetcher
from propguard.monitor.pipeline import SqlLLMLedger, check_source
from propguard.notify.base import NullNotifier
from propguard.registry import service as reg

pytestmark = pytest.mark.integration
FIX = Path(__file__).resolve().parents[1] / "fixtures" / "firm_demo.json"

PAGE_V1 = """<html><head><title>Trading Rules</title><script>var x=1</script></head><body>
<nav>Home | Rules</nav><main><h1>Trading Rules</h1>
<p>Maximum daily loss is 5% of the initial balance.</p><p>Maximum loss is 10%.</p>
<p>News trading is allowed.</p><p>© 2026 Demo Firm</p></main><footer>cookies</footer></body></html>"""
PAGE_V1_COSMETIC = PAGE_V1.replace("var x=1", "var x=2").replace("© 2026", "© 2027")
PAGE_V2 = PAGE_V1.replace("Maximum daily loss is 5%", "Maximum daily loss is 4%")


class Server:
    def __init__(self):
        self.body = PAGE_V1
        self.etag = '"v1"'
        self.calls = []
        self.status = 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if self.status != 200:
            return httpx.Response(self.status, text="blocked")
        if request.headers.get("if-none-match") == self.etag:
            return httpx.Response(304)
        return httpx.Response(200, text=self.body, headers={"etag": self.etag})


class FakeClient:
    """Stands in for anthropic.Anthropic in tests; counts calls, never hits the network."""

    def __init__(self, proposal):
        self.n = 0
        self.proposal = proposal
        self.models = []
        outer = self

        class M:
            def parse(self, **kw):
                outer.n += 1
                outer.models.append(kw["model"])
                assert "sk-ant" not in kw["messages"][0]["content"]
                assert len(kw["messages"][0]["content"]) < 6000  # fragment only, never whole page

                class R:
                    stop_reason = "end_turn"
                    parsed_output = outer.proposal

                    class usage:
                        input_tokens = 500
                        output_tokens = 100
                return R()
        self.messages = M()


@pytest.fixture
def setup(sf):
    with session_scope(sf) as s:
        reg.load_seed(s, json.loads(FIX.read_text()), SessionAudit(s))
        for r in s.scalars(select(Rule).where(Rule.is_current.is_(True), Rule.kind != "custom")).all():
            reg.verify_rule(s, r.id, "owner")
    server = Server()
    fetcher = Fetcher("test-agent", min_delay_per_host_s=0,
                      client=httpx.Client(transport=httpx.MockTransport(server.handler)))
    return sf, server, fetcher


def _rules_src(s):
    return s.scalar(select(Source).where(Source.doc_type == "TRADING_RULES"))


def test_pipeline_no_llm_when_unchanged_and_fail_closed_on_change(setup):
    sf, server, fetcher = setup
    fake = FakeClient(ChangeProposal(affected_rule_kind="daily_loss_limit",
                                     new_params={"pct": 4, "reset_tz": "Europe/Prague"}, ambiguous=False,
                                     confidence=0.9, summary="daily loss lowered", quote="Maximum daily loss is 4%"))
    notifier = NullNotifier()
    with session_scope(sf) as s:
        an = ChangeAnalyzer("anthropic", "cheap-model", "strong-model", 5.0, SqlLLMLedger(s), client_factory=lambda: fake)
        src = _rules_src(s)
        out = check_source(s, src, fetcher, an, notifier)
        assert out.result == "BASELINE" and out.evidence_matched == 1
        ev = s.scalar(select(Evidence).where(Evidence.source_id == src.id))
        assert ev.verification_status == "VERIFIED"  # owner verified earlier; stays verified
        assert check_source(s, src, fetcher, an, notifier).result == "NOT_MODIFIED"
        server.etag = '"v1b"'
        server.body = PAGE_V1_COSMETIC
        assert check_source(s, src, fetcher, an, notifier).result == "UNCHANGED"  # script/© changes ignored
        assert fake.n == 0  # no LLM calls without a real change
        server.etag, server.body = '"v2"', PAGE_V2
        out = check_source(s, src, fetcher, an, notifier)
        assert out.result == "CHANGED" and out.severity == "CRITICAL"
        assert fake.n == 1 and fake.models == ["cheap-model"]  # confident cheap result: no escalation
        assert any("daily_loss_limit" in k for k in out.rules_marked_uncertain)
        dl = s.scalar(select(Rule).where(Rule.kind == "daily_loss_limit", Rule.is_current.is_(True)))
        assert dl.interpretation_status == "UNCERTAIN"
        rc = s.scalar(select(RuleChange).where(RuleChange.approval_state == "PENDING"))
        assert rc is not None and "4%" in rc.diff_fragment and rc.new_value["llm_proposals"][0]["valid"]
        assert s.scalar(select(Alert).where(Alert.severity == "CRITICAL")) is not None
        assert notifier.sent and notifier.sent[0][0] == "CRITICAL"
        assert s.scalar(select(LLMCall)).cost_usd > 0
    with session_scope(sf) as s:
        rs = reg.ruleset_for(s, "demo-firm", "two-step", "phase1", 100000)
        assert rs.pending_critical_change and rs.non_confirmed_critical()


def test_ambiguous_cheap_result_escalates_to_strong(setup):
    sf, server, fetcher = setup
    fake = FakeClient(ChangeProposal(ambiguous=True, confidence=0.3))
    with session_scope(sf) as s:
        an = ChangeAnalyzer("anthropic", "cheap-model", "strong-model", 5.0, SqlLLMLedger(s), client_factory=lambda: fake)
        src = _rules_src(s)
        check_source(s, src, fetcher, an)
        server.etag, server.body = '"v2"', PAGE_V2
        check_source(s, src, fetcher, an)
        assert fake.models == ["cheap-model", "strong-model"]


def test_budget_exhausted_means_no_call_and_rule_stays_uncertain(setup):
    sf, server, fetcher = setup
    fake = FakeClient(ChangeProposal(ambiguous=False, confidence=1, affected_rule_kind="profit_target",
                                     new_params={"pct": 8}))
    with session_scope(sf) as s:
        an = ChangeAnalyzer("anthropic", "c", "s", 0.0, SqlLLMLedger(s), client_factory=lambda: fake)
        src = _rules_src(s)
        check_source(s, src, fetcher, an)
        server.etag, server.body = '"v2"', PAGE_V2
        out = check_source(s, src, fetcher, an)
        assert fake.n == 0 and out.rules_marked_uncertain


def test_blocked_source_is_not_bypassed_and_alerts_after_threshold(setup):
    sf, server, fetcher = setup
    server.status = 403
    with session_scope(sf) as s:
        src = _rules_src(s)
        for _ in range(3):
            out = check_source(s, src, fetcher)
        assert out.result == "BLOCKED" and src.consecutive_failures == 3
        assert s.scalar(select(Alert).where(Alert.kind == "source_unavailable")) is not None
    ua = {c.headers.get("user-agent") for c in server.calls}
    assert ua == {"test-agent"}  # honest UA, no rotation / evasion


def test_llm_disabled_by_default_is_fail_closed():
    an = ChangeAnalyzer("none", "c", "s", 5.0, None)
    a = an.analyze("Maximum daily loss is 4%", "x", ["daily_loss_limit"], "TRADING_RULES")
    assert a.proposal.ambiguous and not a.valid_params and a.model is None
