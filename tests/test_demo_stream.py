"""Tests for the continuous demo-data stream (demo_stream.py).

The load-bearing test here is `test_prune_never_touches_curated_seed_records`:
prune runs unattended every 30 minutes against production Firestore, so a bug
that let it delete the curated "hero" records would silently dismantle the
scripted demo narrative between now and judging, with nobody watching.
"""

import random
from datetime import UTC, datetime, timedelta

from fleet_hackathon import demo_stream
from fleet_hackathon.config import (
    COLLECTION_DEALS,
    COLLECTION_INVOICES,
    COLLECTION_LEADS,
)

NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)


def _rng():
    """Seeded so every assertion below is deterministic."""
    return random.Random(1234)


def _ids(db, collection):
    return {doc.id for doc in db.collection(collection).stream()}


def test_inject_creates_work_for_every_agent(fake_db):
    summary = demo_stream.inject(fake_db, now=NOW, rng=_rng())

    assert summary == {"leads": 1, "deals": 2, "invoices": 1}
    # a lead for Outreach-Check
    assert len(_ids(fake_db, COLLECTION_LEADS)) == 1
    # one closed-won deal with no invoice (Invoice Agent) + one carrying the
    # injected invoice (Payment-Followup / Account-Management)
    assert len(_ids(fake_db, COLLECTION_DEALS)) == 2
    assert len(_ids(fake_db, COLLECTION_INVOICES)) == 1


def test_injected_records_are_marked_and_identifiable(fake_db):
    demo_stream.inject(fake_db, now=NOW, rng=_rng())

    for collection in (COLLECTION_LEADS, COLLECTION_DEALS, COLLECTION_INVOICES):
        for doc in fake_db.collection(collection).stream():
            assert demo_stream.STREAM_MARKER in doc.id
            assert doc.to_dict()["injected"] is True
            assert doc.to_dict()["injected_ts"]


def test_injected_invoice_is_overdue_so_payment_followup_has_work(fake_db):
    """Regression guard for the reason this module injects invoices directly
    instead of relying on the Invoice Agent: agent-issued invoices are due in
    the future and would generate no follow-up activity for weeks."""
    demo_stream.inject(fake_db, now=NOW, rng=_rng())

    invoice = next(iter(fake_db.collection(COLLECTION_INVOICES).stream())).to_dict()
    assert datetime.fromisoformat(invoice["due_ts"]) < NOW


def test_prune_bounds_the_working_set(fake_db):
    for i in range(10):
        demo_stream.inject(fake_db, now=NOW + timedelta(minutes=30 * i), rng=_rng())

    assert len(_ids(fake_db, COLLECTION_DEALS)) == 20  # 2 per tick, unbounded so far

    demo_stream.prune(fake_db, cap=6)

    assert len(_ids(fake_db, COLLECTION_DEALS)) == 6
    assert len(_ids(fake_db, COLLECTION_LEADS)) == 6
    assert len(_ids(fake_db, COLLECTION_INVOICES)) <= 6


def test_prune_removes_oldest_first(fake_db):
    demo_stream.inject(fake_db, now=NOW, rng=_rng())
    demo_stream.inject(fake_db, now=NOW + timedelta(hours=1), rng=_rng())
    demo_stream.inject(fake_db, now=NOW + timedelta(hours=2), rng=_rng())

    newest_ts = (NOW + timedelta(hours=2)).isoformat()
    demo_stream.prune(fake_db, cap=1)

    remaining = [doc.to_dict() for doc in fake_db.collection(COLLECTION_LEADS).stream()]
    assert len(remaining) == 1
    assert remaining[0]["injected_ts"] == newest_ts


def test_prune_never_touches_curated_seed_records(fake_db):
    """prune runs unattended against production; deleting a hero record would
    silently break the scripted demo with nobody watching."""
    curated = {
        COLLECTION_LEADS: "lead-001",
        COLLECTION_DEALS: "deal-001",
        COLLECTION_INVOICES: "inv-followup-001",
    }
    for collection, doc_id in curated.items():
        fake_db.collection(collection).document(doc_id).set({"id": doc_id, "status": "issued"})

    for i in range(20):
        demo_stream.inject(fake_db, now=NOW + timedelta(minutes=30 * i), rng=_rng())
    demo_stream.prune(fake_db, cap=1)

    for collection, doc_id in curated.items():
        assert doc_id in _ids(fake_db, collection), f"{doc_id} was pruned — must never happen"


def test_prune_ignores_marker_lookalikes_without_the_injected_flag(fake_db):
    """Belt-and-braces: the id marker alone must not be enough to delete a
    record — the explicit `injected` flag is also required."""
    fake_db.collection(COLLECTION_LEADS).document(f"lead{demo_stream.STREAM_MARKER}fake").set(
        {"lead_id": "impostor", "status": "new"}
    )
    for i in range(5):
        demo_stream.inject(fake_db, now=NOW + timedelta(minutes=30 * i), rng=_rng())

    demo_stream.prune(fake_db, cap=1)

    assert f"lead{demo_stream.STREAM_MARKER}fake" in _ids(fake_db, COLLECTION_LEADS)


def test_pruning_a_deal_also_removes_its_auto_issued_invoice(fake_db):
    """Otherwise the Invoice Agent's output outlives its deal and the dashboard
    shows an invoice referencing a deal that no longer exists."""
    demo_stream.inject(fake_db, now=NOW, rng=_rng())
    oldest_deal_id = min(_ids(fake_db, COLLECTION_DEALS))
    # simulate the Invoice Agent having issued against it
    fake_db.collection(COLLECTION_INVOICES).document(f"inv-{oldest_deal_id}").set(
        {"invoice_id": f"inv-{oldest_deal_id}", "deal_id": oldest_deal_id, "status": "issued"}
    )

    for i in range(1, 6):
        demo_stream.inject(fake_db, now=NOW + timedelta(minutes=30 * i), rng=_rng())
    demo_stream.prune(fake_db, cap=1)

    assert f"inv-{oldest_deal_id}" not in _ids(fake_db, COLLECTION_INVOICES)


def test_tick_injects_then_prunes(fake_db):
    for i in range(12):
        demo_stream.tick(fake_db, now=NOW + timedelta(minutes=30 * i), rng=_rng(), cap=4)

    assert len(_ids(fake_db, COLLECTION_DEALS)) == 4
    assert len(_ids(fake_db, COLLECTION_LEADS)) == 4


def test_tick_reports_what_it_did(fake_db):
    result = demo_stream.tick(fake_db, now=NOW, rng=_rng(), cap=100)

    assert result["injected"]["leads"] == 1
    assert result["pruned"] == {"leads_removed": 0, "deals_removed": 0, "invoices_removed": 0}
