"""Payment-Followup's graduated-judgment logic — pure, deterministic,
no Firestore/LLM call. This is the decision-maker behavior the build plan
calls out as the thing that makes it more than a cron job."""

from datetime import UTC, datetime, timedelta

from fleet_hackathon.agents.payment_followup import decide
from fleet_hackathon.config import (
    OVERDUE_ESCALATION_DAYS,
    OVERDUE_ESCALATION_REMINDER_COUNT,
)

NOW = datetime(2026, 8, 7, tzinfo=UTC)


def _invoice(days_overdue: int, reminders_sent: int = 0, status: str = "issued") -> dict:
    return {
        "invoice_id": "inv-test",
        "due_ts": (NOW - timedelta(days=days_overdue)).isoformat(),
        "reminders_sent": reminders_sent,
        "status": status,
    }


def test_not_yet_due_takes_no_action():
    decision = decide(_invoice(days_overdue=-3), NOW)
    assert decision.action is None


def test_early_overdue_sends_gentle_reminder():
    decision = decide(_invoice(days_overdue=5), NOW)
    assert decision.action == "send_reminder"
    assert decision.tone == "gentle"


def test_later_overdue_sends_firm_reminder():
    decision = decide(_invoice(days_overdue=20, reminders_sent=1), NOW)
    assert decision.action == "send_reminder"
    assert decision.tone == "firm"


def test_past_day_threshold_escalates_instead_of_reminding():
    decision = decide(_invoice(days_overdue=OVERDUE_ESCALATION_DAYS, reminders_sent=1), NOW)
    assert decision.action == "escalate_to_human"


def test_past_reminder_count_threshold_escalates_even_if_days_below_cutoff():
    decision = decide(_invoice(days_overdue=8, reminders_sent=OVERDUE_ESCALATION_REMINDER_COUNT), NOW)
    assert decision.action == "escalate_to_human"


def test_already_escalated_invoice_takes_no_further_action():
    decision = decide(_invoice(days_overdue=40, reminders_sent=3, status="escalated"), NOW)
    assert decision.action is None


def test_already_paid_invoice_takes_no_action():
    decision = decide(_invoice(days_overdue=2, status="paid"), NOW)
    assert decision.action is None


def test_tone_actually_varies_by_severity_not_just_frequency():
    """The 'graduated' claim: two reminders at different overdue-ages must not
    have the same tone."""
    gentle = decide(_invoice(days_overdue=2), NOW)
    firm = decide(_invoice(days_overdue=15), NOW)
    assert gentle.tone != firm.tone
