"""Page-weight caps (added 2026-08-12).

The live dashboard measured 135,939 B, of which ~74 KB was the *same* 200-entry
audit slice rendered twice: as kanban mini-log entries (all overflow entries in
the collapsed <details>, hidden but fully in the DOM) and again as activity-log
rows. These tests pin the two caps that fixed it, plus the counter that keeps
the capped render from understating the fleet's real history.
"""

from datetime import UTC, datetime

import pytest

from fleet_hackathon import dashboard, runtime, seed_demo_data
from fleet_hackathon.telemetry import AuditLogger

NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)

_ACTIVITY_LOG_HEADING = "<h3>Full activity log</h3>"


def _seeded_db(fake_db):
    seed_demo_data.seed(fake_db, now=NOW)
    runtime.ensure_registered(fake_db)
    return fake_db


def _write_entries(db, n, agent_name="invoice"):
    logger = AuditLogger(db)
    for i in range(n):
        logger.log(
            trace_id=logger.new_trace_id(),
            agent_name=agent_name,
            action="issue_invoice",
            status="ok",
            detail=f"synthetic entry {i}",
            success_criterion="every closed-won deal has an invoice",
        )
    return logger


def _activity_log_rows(html):
    """<tr> count inside the activity-log section only — the page has other
    tables (benchmarks, account managers) whose rows are not what's capped."""
    section = html[html.index(_ACTIVITY_LOG_HEADING) :]
    return section.count("<tr") - 1  # minus the header row


def test_activity_log_render_is_capped_below_the_stored_count(fake_db):
    db = _seeded_db(fake_db)
    stored = dashboard._ACTIVITY_LOG_RENDER_CAP * 3
    _write_entries(db, stored)

    rows = _activity_log_rows(dashboard.render(db))

    assert rows == dashboard._ACTIVITY_LOG_RENDER_CAP, (
        f"activity log rendered {rows} rows of {stored} stored — cap not applied"
    )


def test_kanban_overflow_drill_down_is_bounded(fake_db):
    db = _seeded_db(fake_db)
    _write_entries(db, dashboard._ACTIVITY_LOG_RENDER_CAP, agent_name="invoice")

    html = dashboard.render(db)

    overflow_max = dashboard._KANBAN_OVERFLOW_LOG_CAP - dashboard._KANBAN_VISIBLE_LOG_CAP
    assert f"+{overflow_max} more" in html
    for n in range(overflow_max + 1, dashboard._ACTIVITY_LOG_RENDER_CAP + 1):
        assert f"+{n} more" not in html, f"kanban rendered {n} overflow entries, above the {overflow_max} cap"


def test_actions_logged_chip_reports_lifetime_total_not_the_render_cap(fake_db):
    db = _seeded_db(fake_db)
    stored = dashboard._ACTIVITY_LOG_RENDER_CAP + 40
    _write_entries(db, stored)

    html = dashboard.render(db)

    assert f"{stored:,} actions logged" in html, "chip reported the render cap instead of the true total"
    assert f"{dashboard._ACTIVITY_LOG_RENDER_CAP} actions logged" not in html


def test_count_all_returns_true_total(fake_db):
    db = _seeded_db(fake_db)
    _write_entries(db, 30)
    assert AuditLogger(db).count_all() == 30


def test_count_all_returns_none_when_backend_cannot_aggregate():
    """Fail-soft contract: a backend without count() yields None, never raises —
    this renders a judge-facing page."""

    class _NoCountCollection:
        def count(self):
            raise NotImplementedError("backend has no aggregation support")

    class _NoAggregateDb:
        def collection(self, name):
            return _NoCountCollection()

    assert AuditLogger(_NoAggregateDb()).count_all() is None


def test_dashboard_still_renders_when_count_is_unavailable(fake_db, monkeypatch):
    db = _seeded_db(fake_db)
    _write_entries(db, 12)
    monkeypatch.setattr(AuditLogger, "count_all", lambda self: None)

    html = dashboard.render(db)

    assert "12 actions logged" in html, "fallback did not use the rendered slice length"


@pytest.mark.parametrize("cap_name", ["_ACTIVITY_LOG_RENDER_CAP", "_KANBAN_OVERFLOW_LOG_CAP"])
def test_caps_are_named_constants_not_literals(cap_name):
    """Guards against a future edit re-inlining the number — the 200 that caused
    this was a bare literal in render()."""
    assert isinstance(getattr(dashboard, cap_name), int)


# --- Byte budget (added 2026-08-27, narration) --------------------------------

PAGE_BYTE_BUDGET = 120_000
"""The row caps above bound the number of rendered entries, not their size, so
they cannot detect a field growing. Narration adds a Gemini sentence to every
narrated entry — one measured live call on 2026-08-27 returned 224 characters,
roughly double the 120 assumed when the hook was designed. At 224 chars across
the 75-row activity cap plus the kanban overflow that is ~20 KB on top of the
86,852 B the page measured after the 2026-08-12 optimisation, landing near
107 KB. This budget is the headroom above that, and it is the assertion that
would actually fail if narration length drifted."""


def test_page_stays_within_its_byte_budget_with_every_entry_narrated(fake_db):
    db = _seeded_db(fake_db)
    logger = AuditLogger(db)
    # Worst realistic case: the full rendered slice, every entry narrated at the
    # measured live length.
    narration = "N" * 224
    for i in range(dashboard._ACTIVITY_LOG_RENDER_CAP * 2):
        logger.log(
            trace_id=logger.new_trace_id(),
            agent_name="payment_followup",
            action="send_reminder",
            status="drift",
            detail=f"synthetic entry {i} with a realistic length of deterministic detail text",
            success_criterion="no invoice receives a third reminder without human handoff",
            drift_detected=True,
            narration=narration,
        )

    size = len(dashboard.render(db).encode("utf-8"))

    assert size <= PAGE_BYTE_BUDGET, (
        f"page grew to {size:,} B, above the {PAGE_BYTE_BUDGET:,} B budget"
    )
