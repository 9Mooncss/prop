"""ORM models. Works on PostgreSQL (production) and SQLite (local mode)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


TS = DateTime(timezone=True)


# --------------------------------------------------------------------------- registry


class Firm(Base):
    __tablename__ = "firms"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    official_url: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(32), default="INSUFFICIENT_DATA")  # VERIFIED|WATCHLIST|...
    status_reason: Mapped[str] = mapped_column(Text, default="")
    payout_classification: Mapped[str] = mapped_column(String(32), default="UNKNOWN")
    payout: Mapped[dict] = mapped_column(JSON, default=dict)
    jurisdiction: Mapped[dict] = mapped_column(JSON, default=dict)
    kyc: Mapped[dict] = mapped_column(JSON, default=dict)
    automation: Mapped[dict] = mapped_column(JSON, default=dict)
    platforms: Mapped[list] = mapped_column(JSON, default=list)
    risk_signals: Mapped[list] = mapped_column(JSON, default=list)
    unknowns: Mapped[list] = mapped_column(JSON, default=list)
    researched_at: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(TS, default=utcnow, onupdate=utcnow)
    programs: Mapped[list["Program"]] = relationship(back_populates="firm", cascade="all, delete-orphan")
    sources: Mapped[list["Source"]] = relationship(back_populates="firm", cascade="all, delete-orphan")


class Source(Base):
    __tablename__ = "sources"
    __table_args__ = (UniqueConstraint("firm_id", "canonical_url", name="uq_source_firm_url"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    firm_id: Mapped[int] = mapped_column(ForeignKey("firms.id", ondelete="CASCADE"), index=True)
    seed_ref: Mapped[str | None] = mapped_column(String(32))
    url: Mapped[str] = mapped_column(String(1000))
    canonical_url: Mapped[str] = mapped_column(String(1000))
    title: Mapped[str] = mapped_column(String(500), default="")
    doc_type: Mapped[str] = mapped_column(String(40), default="MARKETING")
    priority: Mapped[int] = mapped_column(Integer, default=50)  # lower = more authoritative
    is_primary: Mapped[bool] = mapped_column(Boolean, default=True)
    monitored: Mapped[bool] = mapped_column(Boolean, default=True)
    etag: Mapped[str | None] = mapped_column(String(300))
    last_modified: Mapped[str | None] = mapped_column(String(100))
    last_hash: Mapped[str | None] = mapped_column(String(64))
    last_checked_at: Mapped[datetime | None] = mapped_column(TS)
    last_changed_at: Mapped[datetime | None] = mapped_column(TS)
    last_ok_at: Mapped[datetime | None] = mapped_column(TS)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    firm: Mapped[Firm] = relationship(back_populates="sources")


class DocumentSnapshot(Base):
    __tablename__ = "document_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), index=True)
    fetched_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    http_status: Mapped[int | None] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    canonical_text: Mapped[str] = mapped_column(Text)
    blocks: Mapped[list] = mapped_column(JSON, default=list)
    title: Mapped[str] = mapped_column(String(500), default="")
    parser_version: Mapped[str] = mapped_column(String(40))
    version: Mapped[int] = mapped_column(Integer, default=1)


class Evidence(Base):
    __tablename__ = "evidence"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    firm_id: Mapped[int] = mapped_column(ForeignKey("firms.id", ondelete="CASCADE"), index=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id", ondelete="SET NULL"))
    snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("document_snapshots.id", ondelete="SET NULL"))
    fragment: Mapped[str] = mapped_column(Text)
    fragment_hash: Mapped[str] = mapped_column(String(64))
    retrieved_at: Mapped[datetime | None] = mapped_column(TS)
    last_verified_at: Mapped[datetime | None] = mapped_column(TS)
    parser_version: Mapped[str | None] = mapped_column(String(40))
    model_version: Mapped[str | None] = mapped_column(String(80))
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    verification_status: Mapped[str] = mapped_column(String(32), default="UNVERIFIED")
    method: Mapped[str] = mapped_column(String(40), default="seed_research")


class Program(Base):
    __tablename__ = "programs"
    __table_args__ = (UniqueConstraint("firm_id", "slug", name="uq_program_firm_slug"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    firm_id: Mapped[int] = mapped_column(ForeignKey("firms.id", ondelete="CASCADE"), index=True)
    slug: Mapped[str] = mapped_column(String(120))
    name: Mapped[str] = mapped_column(String(300))
    platforms: Mapped[list] = mapped_column(JSON, default=list)
    refund: Mapped[str] = mapped_column(Text, default="")
    phases: Mapped[list] = mapped_column(JSON, default=list)  # ordered phase names
    firm: Mapped[Firm] = relationship(back_populates="programs")
    challenges: Mapped[list["Challenge"]] = relationship(back_populates="program", cascade="all, delete-orphan")


class Challenge(Base):
    """A purchasable account size variant of a program."""

    __tablename__ = "challenges"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    program_id: Mapped[int] = mapped_column(ForeignKey("programs.id", ondelete="CASCADE"), index=True)
    account_size: Mapped[float] = mapped_column(Float)
    price: Mapped[float | None] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    program: Mapped[Program] = relationship(back_populates="challenges")


class Rule(Base):
    """One version of one normalized rule. Current version has is_current=True."""

    __tablename__ = "rules"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rule_key: Mapped[str] = mapped_column(String(300), index=True)  # firm:program:phase:kind
    firm_id: Mapped[int] = mapped_column(ForeignKey("firms.id", ondelete="CASCADE"), index=True)
    program_id: Mapped[int | None] = mapped_column(ForeignKey("programs.id", ondelete="CASCADE"))
    phase: Mapped[str] = mapped_column(String(40), default="all")
    kind: Mapped[str] = mapped_column(String(60))
    params: Mapped[dict] = mapped_column(JSON, default=dict)  # normalized, schema-validated
    raw_params: Mapped[dict] = mapped_column(JSON, default=dict)  # as extracted
    raw_text: Mapped[str] = mapped_column(Text, default="")
    criticality: Mapped[str] = mapped_column(String(16), default="NORMAL")
    interpretation_status: Mapped[str] = mapped_column(String(16), default="UNVERIFIED")
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    interpretation_notes: Mapped[str] = mapped_column(Text, default="")
    evidence_ids: Mapped[list] = mapped_column(JSON, default=list)
    version: Mapped[int] = mapped_column(Integer, default=1)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    verified_at: Mapped[datetime | None] = mapped_column(TS)
    verified_by: Mapped[str | None] = mapped_column(String(80))
    parser_version: Mapped[str | None] = mapped_column(String(40))
    model_version: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)


class RuleChange(Base):
    __tablename__ = "rule_changes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rule_key: Mapped[str] = mapped_column(String(300), index=True)
    firm_id: Mapped[int] = mapped_column(ForeignKey("firms.id", ondelete="CASCADE"), index=True)
    old_value: Mapped[dict | None] = mapped_column(JSON)
    new_value: Mapped[dict | None] = mapped_column(JSON)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id", ondelete="SET NULL"))
    old_doc_hash: Mapped[str | None] = mapped_column(String(64))
    new_doc_hash: Mapped[str | None] = mapped_column(String(64))
    diff_fragment: Mapped[str] = mapped_column(Text, default="")
    parser_version: Mapped[str | None] = mapped_column(String(40))
    model_version: Mapped[str | None] = mapped_column(String(80))
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    criticality: Mapped[str] = mapped_column(String(16), default="NORMAL")
    approval_state: Mapped[str] = mapped_column(String(20), default="PENDING")  # PENDING|APPROVED|REJECTED|AUTO
    decided_by: Mapped[str | None] = mapped_column(String(80))
    decided_at: Mapped[datetime | None] = mapped_column(TS)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)


class Conflict(Base):
    __tablename__ = "conflicts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    firm_id: Mapped[int] = mapped_column(ForeignKey("firms.id", ondelete="CASCADE"), index=True)
    field: Mapped[str] = mapped_column(String(200))
    rule_kind: Mapped[str | None] = mapped_column(String(60))
    values: Mapped[list] = mapped_column(JSON, default=list)
    source_refs: Mapped[list] = mapped_column(JSON, default=list)
    notes: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="OPEN")
    resolution: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)


class Alert(Base):
    __tablename__ = "alerts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    severity: Mapped[str] = mapped_column(String(12))  # INFO|WARNING|HIGH|CRITICAL
    kind: Mapped[str] = mapped_column(String(60))
    firm_id: Mapped[int | None] = mapped_column(ForeignKey("firms.id", ondelete="SET NULL"))
    account_id: Mapped[str | None] = mapped_column(String(80))
    message: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)
    notified: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow, index=True)


# --------------------------------------------------------------------------- user side


class UserProfile(Base):
    """Versioned owner profile; every change creates a new row (history kept)."""

    __tablename__ = "user_profiles"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, index=True)
    citizenship: Mapped[str] = mapped_column(String(2), default="UA")
    residence_country: Mapped[str | None] = mapped_column(String(2))
    residence_region: Mapped[str | None] = mapped_column(String(80))
    tax_residency: Mapped[str | None] = mapped_column(String(2))
    kyc_documents: Mapped[list] = mapped_column(JSON, default=list)  # [{"type":"passport","country":"UA"}]
    ip_location_country: Mapped[str | None] = mapped_column(String(2))
    payout_requirement: Mapped[str] = mapped_column(String(32), default="DIRECT_CRYPTO")
    preferred_networks: Mapped[list] = mapped_column(JSON, default=list)
    platforms: Mapped[list] = mapped_column(JSON, default=list)
    automation_channel: Mapped[str] = mapped_column(String(10), default="api")
    max_budget_usd: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)


class WalletAddress(Base):
    __tablename__ = "wallet_addresses"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    label: Mapped[str] = mapped_column(String(80))
    network: Mapped[str] = mapped_column(String(20))
    currency: Mapped[str] = mapped_column(String(10))
    address: Mapped[str] = mapped_column(String(200))
    confirmed_by_owner: Mapped[bool] = mapped_column(Boolean, default=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(TS)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)


class TradeHistory(Base):
    __tablename__ = "trade_histories"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    content_hash: Mapped[str] = mapped_column(String(64))
    account_size: Mapped[float] = mapped_column(Float)
    trades: Mapped[list] = mapped_column(JSON, default=list)
    uploaded_at: Mapped[datetime] = mapped_column(TS, default=utcnow)


class Recommendation(Base):
    __tablename__ = "recommendations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    profile_version: Mapped[int] = mapped_column(Integer)
    history_id: Mapped[int | None] = mapped_column(ForeignKey("trade_histories.id", ondelete="SET NULL"))
    inputs: Mapped[dict] = mapped_column(JSON, default=dict)
    results: Mapped[list] = mapped_column(JSON, default=list)
    engine_version: Mapped[str] = mapped_column(String(40))


# --------------------------------------------------------------------------- trading side


class TradingAccount(Base):
    __tablename__ = "trading_accounts"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    firm_slug: Mapped[str] = mapped_column(String(80))
    program_slug: Mapped[str] = mapped_column(String(120))
    phase: Mapped[str] = mapped_column(String(40))
    initial_balance: Mapped[float] = mapped_column(Float)
    adapter: Mapped[str] = mapped_column(String(40), default="simulated")
    mode: Mapped[str] = mapped_column(String(16), default="PAPER")  # PAPER | LIVE
    live_approved: Mapped[bool] = mapped_column(Boolean, default=False)
    live_approved_at: Mapped[datetime | None] = mapped_column(TS)
    live_approval_fingerprint: Mapped[str | None] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")  # ACTIVE|PASSED|FAILED|CLOSED
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)


class RiskStateRow(Base):
    __tablename__ = "risk_states"
    account_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    state: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(TS, default=utcnow, onupdate=utcnow)


class OrderRow(Base):
    __tablename__ = "orders"
    client_order_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(80), index=True)
    status: Mapped[str] = mapped_column(String(24), index=True)
    request: Mapped[dict] = mapped_column(JSON)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(TS, default=utcnow, onupdate=utcnow)


class KillSwitchRow(Base):
    __tablename__ = "kill_switches"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[str] = mapped_column(String(80), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    reason: Mapped[str] = mapped_column(Text)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    activated_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    cleared_at: Mapped[datetime | None] = mapped_column(TS)
    cleared_by: Mapped[str | None] = mapped_column(String(80))
    note: Mapped[str | None] = mapped_column(Text)


class LedgerPosition(Base):
    __tablename__ = "ledger_positions"
    account_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    position_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    data: Mapped[dict] = mapped_column(JSON)


class ProcessedEvent(Base):
    __tablename__ = "processed_events"
    account_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[str] = mapped_column(String(40))
    kind: Mapped[str] = mapped_column(String(60), index=True)
    account_id: Mapped[str | None] = mapped_column(String(80), index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64), unique=True)


class LLMCall(Base):
    __tablename__ = "llm_calls"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(TS, default=utcnow)
    tier: Mapped[str] = mapped_column(String(16))
    model: Mapped[str] = mapped_column(String(80))
    purpose: Mapped[str] = mapped_column(String(60))
    input_hash: Mapped[str] = mapped_column(String(64), index=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    result: Mapped[dict] = mapped_column(JSON, default=dict)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(TS, default=utcnow, onupdate=utcnow)
