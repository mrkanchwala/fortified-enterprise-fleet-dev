"""2026-08-07 tightening pass: Invoice and Payment-Followup's actions must
genuinely dispatch through a notifier, not just write a Firestore status flag
labeled "sent". Outreach-Check's ping_human stays Firestore-only — verified
here too, so a future change can't silently widen its scope.
"""

from datetime import UTC, datetime

from fleet_hackathon.capability import ALL_SCOPES
from fleet_hackathon.config import (
    COLLECTION_DEALS,
    COLLECTION_INVOICES,
    COLLECTION_LEADS,
)
from fleet_hackathon.gateway import Gateway
from fleet_hackathon.registry import AgentRegistry
from fleet_hackathon.telemetry import AuditLogger

NOW = datetime(2026, 8, 7, tzinfo=UTC)


def _gateway(fake_db, fake_notifier):
    registry = AgentRegistry(fake_db)
    for scope in ALL_SCOPES:
        registry.declare(scope)
    return Gateway(fake_db, registry, AuditLogger(fake_db), notifier=fake_notifier)


def test_issue_invoice_dispatches_a_real_message(fake_db, fake_notifier):
    fake_db.collection(COLLECTION_DEALS).document("deal-1").set(
        {"deal_id": "deal-1", "account": "Acme Co", "amount": 500, "close_ts": NOW.isoformat(), "status": "closed_won"}
    )
    gateway = _gateway(fake_db, fake_notifier)
    client = gateway.client_for("invoice")

    result = client.call("issue_invoice", trace_id="t1", deal_id="deal-1")

    assert result["dispatch"]["dispatched"] is True
    assert len(fake_notifier.sent) == 1
    subject, body = fake_notifier.sent[0]
    assert "inv-deal-1" in subject
    assert "deal-1" in body


def test_send_reminder_dispatches_a_real_message(fake_db, fake_notifier):
    fake_db.collection(COLLECTION_INVOICES).document("inv-1").set(
        {"invoice_id": "inv-1", "due_ts": NOW.isoformat(), "reminders_sent": 0, "status": "issued"}
    )
    gateway = _gateway(fake_db, fake_notifier)
    client = gateway.client_for("payment_followup")

    result = client.call("send_reminder", trace_id="t1", invoice_id="inv-1", tone="gentle", message="Reminder: pay up")

    assert result["dispatch"]["dispatched"] is True
    assert len(fake_notifier.sent) == 1
    subject, body = fake_notifier.sent[0]
    assert "inv-1" in subject
    assert body == "Reminder: pay up"


def test_escalate_to_human_dispatches_a_real_message(fake_db, fake_notifier):
    fake_db.collection(COLLECTION_INVOICES).document("inv-1").set(
        {"invoice_id": "inv-1", "due_ts": NOW.isoformat(), "reminders_sent": 2, "status": "issued"}
    )
    gateway = _gateway(fake_db, fake_notifier)
    client = gateway.client_for("payment_followup")

    result = client.call("escalate_to_human", trace_id="t1", invoice_id="inv-1", reason="past threshold")

    assert result["dispatch"]["dispatched"] is True
    assert len(fake_notifier.sent) == 1
    subject, body = fake_notifier.sent[0]
    assert "inv-1" in subject
    assert "inv-1" in body


def test_outreach_check_ping_human_never_dispatches_anything(fake_db, fake_notifier):
    """Unchanged by design: Outreach-Check's only action stays a pure
    Firestore write — first contact is a human noticing and acting
    deliberately, not even an automated nudge."""
    fake_db.collection(COLLECTION_LEADS).document("lead-1").set({"lead_id": "lead-1", "status": "new"})
    gateway = _gateway(fake_db, fake_notifier)
    client = gateway.client_for("outreach_check")

    client.call("ping_human", trace_id="t1", lead_id="lead-1", reason="past SLA")

    assert fake_notifier.sent == []
