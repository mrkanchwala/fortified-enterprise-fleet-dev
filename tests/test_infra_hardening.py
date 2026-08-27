"""Infra hardening (added 2026-08-12, from the pre-submission /cie review).

Two CRITICAL findings:
  1. the rate limiter keyed on the FIRST X-Forwarded-For entry, which the caller
     supplies — measured live, a rotating header put 40/40 requests through a
     30/min bucket while a fixed header correctly produced 10 429s;
  2. the audit log was never pruned, so count() (billed per 1,000 index entries)
     made every dashboard load progressively more expensive.
"""

from fleet_hackathon import app as app_module
from fleet_hackathon import demo_stream
from fleet_hackathon.config import (
    AUDIT_COUNTER_DOC,
    COLLECTION_AUDIT_LOG,
    COLLECTION_STATS,
)
from fleet_hackathon.telemetry import AuditLogger

REAL_CLIENT = "203.0.113.9"
VPS = "46.225.110.140"


class _Req:
    """Minimal stand-in exposing only what _client_ip touches."""

    def __init__(self, xff=None, peer="10.0.0.1"):
        self.headers = {"x-forwarded-for": xff} if xff is not None else {}
        self.client = type("Peer", (), {"host": peer})()


def _write_entries(db, n):
    logger = AuditLogger(db)
    for i in range(n):
        logger.log(
            trace_id=logger.new_trace_id(),
            agent_name="invoice",
            action="issue_invoice",
            status="ok",
            detail=f"entry {i}",
            success_criterion="every closed-won deal has an invoice",
        )
    return logger


# --------------------------------------------------------------- client IP


def test_forged_leading_entry_is_ignored():
    """Straight to the run.app URL: Google appends the true client last."""
    assert app_module._client_ip(_Req(f"1.2.3.4, {REAL_CLIENT}")) == REAL_CLIENT


def test_trusted_proxy_is_skipped(monkeypatch):
    """Via the nginx path: nginx appends the true client, Google appends the VPS."""
    monkeypatch.setattr(app_module, "_TRUSTED_PROXY_IPS", frozenset({VPS}))
    assert app_module._client_ip(_Req(f"9.9.9.9, {REAL_CLIENT}, {VPS}")) == REAL_CLIENT


def test_client_cannot_hide_behind_a_forged_trusted_ip(monkeypatch):
    """Naming our proxy mid-chain must not make the real tail get skipped."""
    monkeypatch.setattr(app_module, "_TRUSTED_PROXY_IPS", frozenset({VPS}))
    assert app_module._client_ip(_Req(f"1.1.1.1, {VPS}, {REAL_CLIENT}")) == REAL_CLIENT


def test_rotating_forged_prefixes_all_collapse_to_one_key():
    """The actual regression. Every one of these was a *separate* rate-limit
    bucket before the fix, which is what let 40/40 requests through."""
    keys = {app_module._client_ip(_Req(f"198.51.100.{i}, {REAL_CLIENT}")) for i in range(40)}
    assert keys == {REAL_CLIENT}


def test_falls_back_to_peer_when_header_absent():
    assert app_module._client_ip(_Req(None, peer="10.0.0.7")) == "10.0.0.7"


def test_empty_and_whitespace_entries_are_discarded():
    assert app_module._client_ip(_Req(f" , ,{REAL_CLIENT} ")) == REAL_CLIENT


# --------------------------------------------------------------- audit pruning


def test_prune_bounds_the_log_and_banks_what_it_removed(fake_db):
    _write_entries(fake_db, 20)

    removed = demo_stream.prune_audit_log(fake_db, cap=10, max_per_run=50)

    assert removed == 10
    live = fake_db.collection(COLLECTION_AUDIT_LOG).count().get()[0][0].value
    assert live == 10
    banked = fake_db.collection(COLLECTION_STATS).document(AUDIT_COUNTER_DOC).get().to_dict()
    assert banked["pruned"] == 10


def test_prune_never_exceeds_its_per_run_budget(fake_db):
    """Guards the 300s scheduled-request deadline on the first big drain."""
    _write_entries(fake_db, 40)
    assert demo_stream.prune_audit_log(fake_db, cap=5, max_per_run=7) == 7


def test_prune_is_a_noop_below_the_cap(fake_db):
    _write_entries(fake_db, 5)
    assert demo_stream.prune_audit_log(fake_db, cap=10) == 0
    assert not fake_db.collection(COLLECTION_STATS).document(AUDIT_COUNTER_DOC).get().exists


def test_prune_removes_the_oldest_first(fake_db):
    _write_entries(fake_db, 12)
    surviving_before = [d.to_dict()["detail"] for d in fake_db.collection(COLLECTION_AUDIT_LOG).stream()]

    demo_stream.prune_audit_log(fake_db, cap=4, max_per_run=50)

    surviving = {d.to_dict()["detail"] for d in fake_db.collection(COLLECTION_AUDIT_LOG).stream()}
    assert "entry 0" not in surviving
    assert "entry 11" in surviving
    assert len(surviving_before) == 12


def test_lifetime_count_survives_pruning(fake_db):
    """The whole point of banking: the chip must keep reporting real history."""
    _write_entries(fake_db, 20)
    assert AuditLogger(fake_db).count_all() == 20

    demo_stream.prune_audit_log(fake_db, cap=6, max_per_run=50)

    assert AuditLogger(fake_db).count_all() == 20


def test_repeated_prunes_accumulate_the_bank(fake_db):
    _write_entries(fake_db, 30)
    demo_stream.prune_audit_log(fake_db, cap=20, max_per_run=50)
    _write_entries(fake_db, 10)
    demo_stream.prune_audit_log(fake_db, cap=20, max_per_run=50)

    assert AuditLogger(fake_db).count_all() == 40


def test_prune_skips_cleanly_when_backend_cannot_aggregate():
    """Fail-soft: no count() support means no pruning, never an exception."""

    class _NoCountCollection:
        def count(self):
            raise NotImplementedError

    class _NoAggregateDb:
        def collection(self, name):
            return _NoCountCollection()

    assert demo_stream.prune_audit_log(_NoAggregateDb()) == 0
