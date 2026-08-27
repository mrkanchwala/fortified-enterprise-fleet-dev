"""Full 5-agent chain run end-to-end against the seeded synthetic dataset —
integration coverage per the build plan, no mocked agent internals."""

from datetime import UTC, datetime

from fleet_hackathon import actions, runtime, seed_demo_data
from fleet_hackathon.config import (
    COLLECTION_CASH_EVENTS,
    COLLECTION_INVOICES,
    COLLECTION_LEADS,
)

NOW = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)


def _seeded_db(fake_db):
    seed_demo_data.seed(fake_db, now=NOW)
    runtime.ensure_registered(fake_db)
    return fake_db


def test_outreach_check_pings_human_only_for_the_lead_past_sla(fake_db):
    """2026-08-10 fix: pins `now=NOW` (the same fixed anchor the fixture data
    is seeded relative to) instead of letting outreach_check.run_cycle fall
    back to real wall-clock time. Previously lead-001 (seeded 30h before NOW,
    inside the 48h SLA window) silently drifted past-SLA and flipped this
    test's assertion as real calendar days passed since NOW was written —
    lead-002 (seeded 72h before NOW) was never at risk since 72h is always
    past a 48h window regardless of drift, which is why only lead-001 broke."""
    db = _seeded_db(fake_db)
    results = runtime.run_agent_cycle(db, "outreach_check", now=NOW)

    by_lead = {r["lead_id"]: r for r in results}
    assert by_lead["lead-001"]["action"] is None, "inside-SLA lead must not trigger any action"
    assert by_lead["lead-002"]["action"] == "ping_human", "past-SLA lead must escalate to a human"

    lead_002 = db.collection(COLLECTION_LEADS).document("lead-002").get().to_dict()
    assert lead_002["status"] == "escalated_to_human"


def test_invoice_agent_issues_exactly_one_invoice_and_is_idempotent(fake_db):
    db = _seeded_db(fake_db)

    first = runtime.run_agent_cycle(db, "invoice")
    deal_001 = next(r for r in first if r["deal_id"] == "deal-001")
    assert deal_001["issued"] is True

    invoice_doc = db.collection(COLLECTION_INVOICES).document("inv-deal-001").get()
    assert invoice_doc.exists

    second = runtime.run_agent_cycle(db, "invoice")
    deal_001_again = next(r for r in second if r["deal_id"] == "deal-001")
    assert deal_001_again["issued"] is False, "re-running must not double-issue (idempotent)"


def test_invoice_agent_does_not_duplicate_the_pre_seeded_hero_followup_invoices(fake_db):
    """2026-08-10 bug fix: the 5 hero followup/gated deals (deal-fu-001..005)
    already have a hero invoice pre-seeded under a different id convention
    (inv-followup-00N, not the Invoice agent's own inv-{deal_id}) — before the
    fix, their "closed_won" status made the Invoice agent's idempotency check
    miss the existing invoice and re-issue a duplicate on every cycle."""
    db = _seeded_db(fake_db)
    results = runtime.run_agent_cycle(db, "invoice")

    followup_deal_ids = {"deal-fu-001", "deal-fu-002", "deal-fu-003", "deal-fu-004", "deal-fu-005"}
    by_deal = {r["deal_id"]: r for r in results}
    for deal_id in followup_deal_ids:
        assert by_deal[deal_id]["issued"] is False, f"{deal_id} must not get a duplicate Invoice-agent-issued invoice"
        duplicate_doc = db.collection(COLLECTION_INVOICES).document(f"inv-{deal_id}").get()
        assert not duplicate_doc.exists, f"inv-{deal_id} should never have been created"


def test_payment_followup_graduated_actions_across_the_seeded_invoices(fake_db):
    """2026-08-27 fix: pins `now=NOW`, the same anchor the fixture is seeded
    against. The 2026-08-10 now-injection fix was applied to the outreach test
    above but missed this one. inv-followup-002 is seeded due_ts = NOW - 20 days;
    left on real wall-clock it crossed OVERDUE_ESCALATION_DAYS (30) once ~10 real
    calendar days had passed since NOW, flipping send_reminder -> escalate_to_human.
    Failed on a clean checkout before any narration work."""
    db = _seeded_db(fake_db)
    results = runtime.run_agent_cycle(db, "payment_followup", now=NOW)

    by_invoice = {r["invoice_id"]: r for r in results}
    # inv-followup-003 is the drift scenario's target — forced, not the agent's real decision.
    assert by_invoice["inv-followup-001"]["action"] == "send_reminder"
    assert by_invoice["inv-followup-002"]["action"] == "send_reminder"
    assert by_invoice["inv-followup-003"]["action"] == "send_reminder"
    assert by_invoice["inv-followup-003"]["reason"] == "forced (demo drift scenario)"


def test_failure_catch_scenario_is_flagged_live_by_the_gateway(fake_db):
    """The scripted demo moment: inv-followup-003 already qualifies for
    escalation, but the seeded scenario forces a 3rd reminder instead. The
    Gateway's drift check must catch this independent of what the agent
    itself would have chosen — this is the layer the demo shows on camera."""
    db = _seeded_db(fake_db)
    runtime.run_agent_cycle(db, "payment_followup")

    from fleet_hackathon.telemetry import AuditLogger

    entries = AuditLogger(db).list_recent(limit=200)
    drift_entries = [e for e in entries if e.get("drift_detected")]

    assert drift_entries, "the seeded drift scenario must produce at least one drift-flagged audit entry"
    assert any(e["attributes"].get("invoice_id") == "inv-followup-003" for e in drift_entries)
    assert all(e["status"] == "drift" for e in drift_entries)


def test_run_all_cycles_covers_all_five_agents(fake_db):
    db = _seeded_db(fake_db)
    results = runtime.run_all_cycles(db)
    assert set(results.keys()) == {
        "outreach_check",
        "invoice",
        "payment_followup",
        "account_management",
        "analytics",
    }
    assert all(results.values()), "every agent must have found at least one record to act on"


def test_account_management_hands_off_only_after_a_real_payment_lands(fake_db):
    """The loop-closer: inv-followup-004 starts unpaid, so account_management
    must not act on it until /mark-paid (actions.record_payment) fires."""
    db = _seeded_db(fake_db)
    before = runtime.run_agent_cycle(db, "account_management")
    by_invoice_before = {r["invoice_id"]: r for r in before}
    assert by_invoice_before["inv-followup-004"]["handed_off"] is False

    actions.record_payment(db, "inv-followup-004")

    after = runtime.run_agent_cycle(db, "account_management")
    by_invoice_after = {r["invoice_id"]: r for r in after}
    assert by_invoice_after["inv-followup-004"]["handed_off"] is True

    invoice_doc = db.collection(COLLECTION_INVOICES).document("inv-followup-004").get().to_dict()
    assert invoice_doc["handed_off_to"] == "Tom Nguyen"


def test_account_management_blocks_handoff_when_delivery_is_not_complete(fake_db):
    """inv-followup-005 is seeded already paid but with delivery_status
    'Delayed' on its linked deal — the delivery gate must block the handoff
    even though the payment condition alone is satisfied."""
    db = _seeded_db(fake_db)
    results = runtime.run_agent_cycle(db, "account_management")

    by_invoice = {r["invoice_id"]: r for r in results}
    assert by_invoice["inv-followup-005"]["handed_off"] is False
    assert "Delayed" in by_invoice["inv-followup-005"]["reason"]

    invoice_doc = db.collection(COLLECTION_INVOICES).document("inv-followup-005").get().to_dict()
    assert invoice_doc.get("handed_off") is not True


def test_cash_events_reset_on_reseed(fake_db):
    """2026-08-10: a re-seed (the documented "run immediately before the
    final take" step) must clear COLLECTION_CASH_EVENTS, not accumulate one
    more incoming event per rehearsal take's /mark-paid trigger — otherwise
    the Analytics Agent's cash_movements dimension inflates across takes."""
    db = _seeded_db(fake_db)
    actions.record_payment(db, "inv-followup-004")
    assert len(list(db.collection(COLLECTION_CASH_EVENTS).stream())) == 1

    seed_demo_data.seed(db, now=NOW)  # simulates "re-run the seed script before the final take"
    assert list(db.collection(COLLECTION_CASH_EVENTS).stream()) == []
