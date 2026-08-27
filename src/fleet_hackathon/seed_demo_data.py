"""Idempotent demo-data seed script — simulated deal/invoice/payment/bill
data, explicitly not real Quadriga transaction history. Every timestamp is
computed relative to run time (`now - N days`), never hardcoded, and safe to
re-run immediately before the final demo recording take — each run resets
every seeded record back to its known starting state so the same triggers
fire the same way on every take.

Audit-log reset (2026-08-10, Step 8 live re-check): a re-seed also clears
COLLECTION_AUDIT_LOG. Found via a repeated multi-take dry run: the drift
scenario's data resets correctly on re-seed, but the dashboard's READY/CAUGHT
indicator (dashboard.py's `_render_drift_status`) scans the FULL historical
audit log for any past drift-flagged entry matching the scenario's invoice
id — so after the first time the failure-catch moment fires (e.g. during
rehearsal), it would show CAUGHT immediately on every subsequent render, even
right after a fresh re-seed, defeating the live "watch it happen" demo
moment on the actual recording take. Clearing the audit log on every seed
keeps the "resets every seeded record back to its known starting state"
promise above actually true for the one piece of state that gates the
demo's core scripted moment.

Cash-events reset (2026-08-10, same fix, flagged alongside the audit-log
finding): a re-seed also clears COLLECTION_CASH_EVENTS. `actions.record_payment`
(the `/mark-paid` demo trigger) writes one incoming cash-event doc per call,
with a random `cash-{uuid}` id, and nothing else ever cleared that collection
on a re-seed. Across repeated rehearsal takes, each `/mark-paid` trigger would
add another incoming event on top of the last take's, inflating the Analytics
Agent's `cash_movements` dimension (a trailing-30-day net-inflow read) further
with every take — a number that could get narrated on camera in Step 11.
Same discipline as the audit-log clear: makes the "known starting state"
promise true for this collection too.

Narration reset (2026-08-27, /cso H1): a re-seed also clears
COLLECTION_NARRATION_CACHE. Cached Gemini narration is keyed by decision class,
so one bad sentence is served to every later entry of that class for the rest of
the judging window, and nothing else in the app deletes it. This clear is the
manual remedy; narration._NARRATION_VERSION is the deploy-time one. Same
discipline as the two resets above: makes the "known starting state" promise
true for model output too, which matters most right before a recording take —
a rehearsal's cached sentence would otherwise survive into the real one.

Client/amount pairs below are real samples pulled from the workspace's
existing `tools/demo-crm-mcp/crm_demo.db` (Quadriga's own public demo CRM
dataset, ~19K synthetic rows, fictional "Northbridge Solutions" universe) —
not invented from scratch. Dates are remapped relative to `now` (the source
DB's dates are static/historical) using the same discipline as the rest of
this file.

Two tiers, deliberately kept apart:
- The "hero" set (leads / deal-001 / inv-followup-00N / bills) is what the
  agents actually run against live in a demo take — small and curated so a
  triggered cycle stays a handful of clean, narratable actions.
- The "portfolio" set (COLLECTION_PORTFOLIO_INVOICES, ~13 invoices across 2
  account managers with deliberately different real collection profiles;
  COLLECTION_PORTFOLIO_ENGAGEMENTS, 20 engagements; COLLECTION_GROWTH_SNAPSHOT
  and COLLECTION_SALES_PIPELINE, static real snapshots) is analytics-only —
  the Executive Snapshot and the Analytics Agent (agents/analytics.py) read
  it, but no other agent's run_cycle ever touches it, so it can be as large
  as a real book of business without flooding a live demo run.
"""

from datetime import UTC, datetime, timedelta

from fleet_hackathon.config import (
    COLLECTION_AUDIT_LOG,
    COLLECTION_BILLS,
    COLLECTION_CASH_EVENTS,
    COLLECTION_DEALS,
    COLLECTION_DRIFT_SCENARIO,
    COLLECTION_GROWTH_SNAPSHOT,
    COLLECTION_INVOICES,
    COLLECTION_LEADS,
    COLLECTION_NARRATION_CACHE,
    COLLECTION_PORTFOLIO_ENGAGEMENTS,
    COLLECTION_PORTFOLIO_INVOICES,
    COLLECTION_SALES_PIPELINE,
)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _clear_collection(db, collection_name: str) -> int:
    count = 0
    for doc in db.collection(collection_name).stream():
        db.collection(collection_name).document(doc.id).delete()
        count += 1
    return count


def seed(db, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    summary = {
        "leads": 0,
        "deals": 0,
        "invoices": 0,
        "drift_scenario": 0,
        "portfolio": 0,
        "bills": 0,
        "engagements": 0,
        "growth_snapshot": 0,
        "sales_pipeline": 0,
        "audit_log_cleared": 0,
        "cash_events_cleared": 0,
        "narration_cache_cleared": 0,
    }

    # --- Audit log reset — see module docstring ("Audit-log reset") ------
    summary["audit_log_cleared"] = _clear_collection(db, COLLECTION_AUDIT_LOG)

    # --- Cash-events reset — see module docstring ("Cash-events reset") --
    summary["cash_events_cleared"] = _clear_collection(db, COLLECTION_CASH_EVENTS)

    # --- Narration cache reset — see module docstring ("Narration reset") -
    summary["narration_cache_cleared"] = _clear_collection(db, COLLECTION_NARRATION_CACHE)

    # --- Outreach-Check: 2 leads ---------------------------------------
    leads = [
        {
            "lead_id": "lead-001",
            "contact_name": "Priya Nair",
            "company": "Northwind Logistics",
            "first_contact_ts": _iso(now - timedelta(hours=30)),
            "sla_window_hours": 48,
            "last_touch_ts": _iso(now - timedelta(hours=10)),
            "status": "in_progress",
        },
        {
            "lead_id": "lead-002",
            "contact_name": "Tomás Herrera",
            "company": "Bluefield Analytics",
            "first_contact_ts": _iso(now - timedelta(hours=72)),
            "sla_window_hours": 48,
            "last_touch_ts": None,
            "status": "new",
        },
    ]
    for lead in leads:
        db.collection(COLLECTION_LEADS).document(lead["lead_id"]).set(lead)
        summary["leads"] += 1

    # --- Invoice: 1 closed_won deal, no invoice yet ---------------------
    deal = {
        "deal_id": "deal-001",
        "account": "Nova Enterprises",
        "amount": 27295.52,
        "currency": "USD",
        "close_ts": _iso(now - timedelta(hours=2)),
        "status": "closed_won",
    }
    db.collection(COLLECTION_DEALS).document(deal["deal_id"]).set(deal)
    summary["deals"] += 1
    # Reset: delete any invoice already auto-issued for this deal on a prior
    # run, so the auto-issue trigger fires fresh on every take.
    db.collection(COLLECTION_INVOICES).document(f"inv-{deal['deal_id']}").delete()

    # --- Payment-Followup + Account-Management: 4 "hero" invoices -------
    # Graduated overdue stages (gentle/firm/escalate) plus a 4th, inv-followup-004,
    # left deliberately unpaid and un-escalated — the "loop-closer" target for the
    # live /mark-paid trigger (payment lands -> Payment-Followup no-ops "already
    # paid" -> Account-Management hands off to the account manager, on camera).
    followup_invoices = [
        {
            "invoice_id": "inv-followup-001",
            "deal_id": "deal-fu-001",
            "account": "Westfield Technologies Group",
            "account_manager": "Sara Ortiz",
            "amount": 15290.79,
            "currency": "USD",
            "due_ts": _iso(now - timedelta(days=5)),
            "reminders_sent": 0,
            "status": "issued",
        },
        {
            "invoice_id": "inv-followup-002",
            "deal_id": "deal-fu-002",
            "account": "Keystone Partners Group",
            "account_manager": "Nadia Larsson",
            "amount": 18006.29,
            "currency": "USD",
            "due_ts": _iso(now - timedelta(days=20)),
            "reminders_sent": 1,
            "status": "issued",
        },
        {
            "invoice_id": "inv-followup-003",
            "deal_id": "deal-fu-003",
            "account": "Pinnacle Logistics LLC",
            "account_manager": "Anya Petrov",
            "amount": 31820.28,
            "currency": "USD",
            "due_ts": _iso(now - timedelta(days=35)),
            "reminders_sent": 2,
            "status": "issued",
        },
        {
            "invoice_id": "inv-followup-004",
            "deal_id": "deal-fu-004",
            "account": "Beacon Enterprises",
            "account_manager": "Tom Nguyen",
            "amount": 15583.00,
            "currency": "USD",
            "due_ts": _iso(now - timedelta(days=8)),
            "reminders_sent": 1,
            "status": "issued",
        },
    ]
    for invoice in followup_invoices:
        db.collection(COLLECTION_INVOICES).document(invoice["invoice_id"]).set(invoice)
        db.collection(COLLECTION_DEALS).document(invoice["deal_id"]).set(
            {
                "deal_id": invoice["deal_id"],
                "account": invoice["account"],
                "amount": invoice["amount"],
                "close_ts": invoice["due_ts"],
                # Deliberately NOT "closed_won" (2026-08-10 fix): these deals
                # already have a hero invoice pre-seeded under a DIFFERENT id
                # (inv-followup-00N, not the Invoice agent's own inv-{deal_id}
                # convention), so a "closed_won" status made the Invoice agent's
                # idempotency check miss the existing invoice and re-issue a
                # duplicate on every cycle (confirmed live — 5 extra
                # "inv-deal-fu-00N: Invoice sent" entries cluttering the kanban).
                # "invoiced" correctly reads as already-past that stage.
                "status": "invoiced",
                # Account-Management's delivery gate (2026-08-10): all 4 hero deals
                # are delivered, so the existing inv-followup-004 loop-closer demo
                # (pay live -> handoff) is unaffected by the new gate.
                "delivery_status": "Delivered",
            }
        )
        summary["invoices"] += 1
        summary["deals"] += 1

    # --- Account-Management: 1 more hero invoice, already paid but NOT ------
    # delivered — demonstrates the delivery-status gate blocking a handoff live,
    # without needing a second /mark-paid trigger during the take. Deliberately
    # pre-seeded as "paid" (not run through Payment-Followup's reminder ladder,
    # which already ignores paid invoices — see agents/payment_followup.py's
    # decide()) so a single `/run account_management` cycle shows both a clean
    # handoff (inv-followup-004) and a correctly blocked one (this) together.
    gated_deal = {
        "deal_id": "deal-fu-005",
        "account": "Blue Group AG",
        "amount": 21430.15,
        "currency": "USD",
        "close_ts": _iso(now - timedelta(days=18)),
        "status": "invoiced",  # not "closed_won" — see the loop above, same duplicate-invoice fix
        "delivery_status": "Delayed",
    }
    db.collection(COLLECTION_DEALS).document(gated_deal["deal_id"]).set(gated_deal)
    summary["deals"] += 1

    gated_invoice = {
        "invoice_id": "inv-followup-005",
        "deal_id": gated_deal["deal_id"],
        "account": gated_deal["account"],
        "account_manager": "Anya Petrov",
        "amount": gated_deal["amount"],
        "currency": "USD",
        "due_ts": _iso(now - timedelta(days=3)),
        "reminders_sent": 0,
        "status": "paid",
        "paid_ts": _iso(now - timedelta(days=1)),
        "amount_paid": gated_deal["amount"],
    }
    db.collection(COLLECTION_INVOICES).document(gated_invoice["invoice_id"]).set(gated_invoice)
    summary["invoices"] += 1

    # --- Failure-catch scenario ------------------------------------------
    # inv-followup-003 already qualifies for escalation (35d overdue, 2
    # reminders sent). This registers it as a forced send_reminder instead —
    # the agent's real decide() would correctly choose escalate_to_human, but
    # the Gateway's independent drift check catches the forced wrong action
    # regardless of what the agent chose, and flags it live.
    drift_scenario = {
        "invoice_id": "inv-followup-003",
        "forced_action": "send_reminder",
        "forced_kwargs": {
            "tone": "firm",
            "message": "Reminder: invoice inv-followup-003 is overdue.",
        },
        "description": (
            "This is a built-in test: one invoice is deliberately set up to try "
            "sending a 3rd reminder after it should have gone to a team member "
            "instead. It shows the safety system catching a mistake automatically."
        ),
    }
    db.collection(COLLECTION_DRIFT_SCENARIO).document("active_scenario").set(drift_scenario)
    summary["drift_scenario"] = 1

    # --- Portfolio: the fuller book of business, analytics-only ---------
    # Manager A (Anya Petrov) — real volume, real collection problem: 4 of 8
    # invoices paid, and paid slowly. Manager B (Nadia Larsson) — smaller book,
    # collects faster and more completely. Both computed live by analytics.py,
    # not pre-baked to a round number.
    def _portfolio(
        invoice_id, account, manager, amount, issued_days_ago, due_offset_days, paid_after_issue_days=None
    ):
        issued = now - timedelta(days=issued_days_ago)
        due = issued + timedelta(days=due_offset_days)
        paid_ts = _iso(issued + timedelta(days=paid_after_issue_days)) if paid_after_issue_days is not None else None
        return {
            "invoice_id": invoice_id,
            "account": account,
            "account_manager": manager,
            "amount": amount,
            "amount_paid": amount if paid_ts else 0,
            "currency": "USD",
            "issued_ts": _iso(issued),
            "due_ts": _iso(due),
            "paid_ts": paid_ts,
            "status": "paid" if paid_ts else "issued",
        }

    portfolio_invoices = [
        _portfolio("pf-001", "Blue Group AG", "Anya Petrov", 67038.67, 95, 30, paid_after_issue_days=40),
        _portfolio("pf-002", "Catalyst Systems", "Anya Petrov", 17366.48, 80, 30, paid_after_issue_days=35),
        _portfolio("pf-003", "Catalyst Retail", "Anya Petrov", 83411.90, 70, 30),
        _portfolio("pf-004", "Catalyst Retail 7", "Anya Petrov", 26159.33, 60, 30),
        _portfolio("pf-005", "Pinnacle Logistics LLC", "Anya Petrov", 28766.94, 50, 30, paid_after_issue_days=45),
        _portfolio("pf-006", "Pinnacle Logistics LLC", "Anya Petrov", 31820.28, 45, 30),
        _portfolio("pf-007", "Catalyst Systems", "Anya Petrov", 17507.15, 40, 30, paid_after_issue_days=38),
        _portfolio("pf-008", "Westfield Technologies Group", "Anya Petrov", 15290.79, 20, 30),
        _portfolio("pf-009", "Keystone Partners Group", "Nadia Larsson", 18006.29, 40, 30, paid_after_issue_days=12),
        _portfolio("pf-010", "Keystone Partners Group", "Nadia Larsson", 16724.71, 35, 30, paid_after_issue_days=8),
        _portfolio("pf-011", "Pioneer Labs BV", "Nadia Larsson", 15920.33, 25, 30, paid_after_issue_days=15),
        _portfolio("pf-012", "Pioneer Labs BV", "Nadia Larsson", 25695.68, 20, 30, paid_after_issue_days=10),
        _portfolio("pf-013", "Westfield Capital", "Nadia Larsson", 30535.77, 15, 30),
    ]
    for invoice in portfolio_invoices:
        db.collection(COLLECTION_PORTFOLIO_INVOICES).document(invoice["invoice_id"]).set(invoice)
        summary["portfolio"] += 1

    # --- Bills: accounts payable, context only, no agent manages this ---
    def _bill(bill_id, supplier, category, amount, issued_days_ago, due_offset_days, paid_after_issue_days=None):
        issued = now - timedelta(days=issued_days_ago)
        due = issued + timedelta(days=due_offset_days)
        paid_ts = _iso(issued + timedelta(days=paid_after_issue_days)) if paid_after_issue_days is not None else None
        return {
            "bill_id": bill_id,
            "supplier": supplier,
            "category": category,
            "amount": amount,
            "currency": "USD",
            "issued_ts": _iso(issued),
            "due_ts": _iso(due),
            "paid_ts": paid_ts,
            "status": "paid" if paid_ts else "issued",
        }

    bills = [
        _bill("bill-001", "Fairmont Health Co Services", "Legal", 8724.11, 60, 30),
        _bill("bill-002", "Evergreen Health Partners", "Marketing", 3202.76, 40, 30, paid_after_issue_days=8),
        _bill("bill-003", "Silverline Systems Supply", "Legal", 11158.88, 35, 30, paid_after_issue_days=5),
        _bill("bill-004", "Summit Technologies Inc Services", "Travel", 17477.39, 20, 30),
        _bill("bill-005", "Orion Enterprises Corp Co", "Software Licenses", 10278.42, 15, 30),
        _bill("bill-006", "Skyline Capital Group Services", "Marketing", 37451.33, 10, 30),
    ]
    for bill in bills:
        db.collection(COLLECTION_BILLS).document(bill["bill_id"]).set(bill)
        summary["bills"] += 1

    # --- Analytics Agent: portfolio-scale, analytics-only -----------------
    # Client names + status mix below are real samples pulled from crm_demo.db's
    # `engagements` table (2,072 rows: 946 Delivered / 496 In Progress / 326
    # Delayed / 164 On Hold / 140 Cancelled — this 20-row sample keeps roughly
    # that same proportion). Dates remapped relative to `now`, same discipline
    # as the rest of this file — the source table's own dates are historical/
    # static and don't matter here, only the status distribution and windows do.
    def _engagement(client, status, start_days_ago, due_offset_days, delivered_days_after_start=None):
        start = now - timedelta(days=start_days_ago)
        due = start + timedelta(days=due_offset_days)
        delivered_ts = (
            _iso(start + timedelta(days=delivered_days_after_start))
            if delivered_days_after_start is not None
            else None
        )
        return {
            "client": client,
            "status": status,
            "start_date": _iso(start),
            "due_date": _iso(due),
            "delivered_date": delivered_ts,
        }

    portfolio_engagements = [
        # Delivered on time — 9 of 20 (~45%, close to the real ~46% share)
        _engagement("Riverstone Works", "Delivered", 70, 60, delivered_days_after_start=58),
        _engagement("Solstice Consulting Ltd", "Delivered", 68, 60, delivered_days_after_start=57),
        _engagement("Clearwater Holdings SA", "Delivered", 65, 60, delivered_days_after_start=54),
        _engagement("Goldcrest Networks Group", "Delivered", 60, 60, delivered_days_after_start=56),
        _engagement("Catalyst Works", "Delivered", 55, 60, delivered_days_after_start=53),
        _engagement("Pioneer Group", "Delivered", 50, 60, delivered_days_after_start=48),
        _engagement("Sterling Technologies", "Delivered", 45, 60, delivered_days_after_start=44),
        _engagement("Vertex Group", "Delivered", 40, 60, delivered_days_after_start=38),
        _engagement("Summit Technologies", "Delivered", 35, 60, delivered_days_after_start=33),
        # In Progress, still within window — 5 of 20 (~25%)
        _engagement("Catalyst Partners", "In Progress", 20, 60),
        _engagement("Stonebridge Industries GmbH", "In Progress", 15, 60),
        _engagement("Quantum Analytics Co", "In Progress", 10, 60),
        _engagement("Emerald Retail AG", "In Progress", 8, 60),
        _engagement("Westfield Systems LLC", "In Progress", 5, 60),
        # Delayed — past due, nothing delivered, the operational-bottleneck signal — 3 of 20
        _engagement("Meridian Industries LLC", "Delayed", 90, 60),
        _engagement("Orion Retail", "Delayed", 100, 45),
        _engagement("Lighthouse Holdings", "Delayed", 75, 30),
        # On Hold — 2 of 20
        _engagement("Catalyst Labs GmbH", "On Hold", 80, 60),
        _engagement("Onyx Logistics", "On Hold", 40, 60),
        # Cancelled — 1 of 20
        _engagement("Nova Enterprises", "Cancelled", 60, 45),
    ]
    for i, engagement in enumerate(portfolio_engagements, start=1):
        db.collection(COLLECTION_PORTFOLIO_ENGAGEMENTS).document(f"eng-{i:03d}").set(engagement)
        summary["engagements"] += 1

    # Growth snapshot — real Won-contract value from crm_demo.db (1,829 contracts):
    # trailing quarter (Mar-May 2026: $23,459,264 / 103 deals) vs. prior quarter
    # (Dec 2025-Feb 2026: $19,255,260 / 73 deals) = +21.8% value growth. A static
    # snapshot, not live-recomputed every agent cycle — same treatment as the
    # DSO/AR-turnover benchmarks already cited in analytics.py.
    db.collection(COLLECTION_GROWTH_SNAPSHOT).document("current").set(
        {
            "current_period_label": "Trailing quarter",
            "prior_period_label": "Prior quarter",
            "current_period_value": 23459264.0,
            "prior_period_value": 19255260.0,
            "current_period_deal_count": 103,
            "prior_period_deal_count": 73,
            "source_note": "Real Won-contract value, crm_demo.db, Mar-May 2026 vs. Dec 2025-Feb 2026.",
        }
    )
    summary["growth_snapshot"] = 1

    # Sales pipeline snapshot — real contract-stage distribution from crm_demo.db
    # (1,829 contracts total). Also a static snapshot, same reasoning as above.
    db.collection(COLLECTION_SALES_PIPELINE).document("current").set(
        {
            "won_count": 1061,
            "won_value": 268947599.0,
            "lost_count": 166,
            "lost_value": 30791408.0,
            "proposal_count": 340,
            "proposal_value": 88097250.0,
            "negotiation_count": 262,
            "negotiation_value": 70147634.0,
            "source_note": "Real contract-stage distribution, crm_demo.db, 1,829 contracts total.",
        }
    )
    summary["sales_pipeline"] = 1

    return summary


def main() -> None:
    from fleet_hackathon.firestore_client import get_db

    db = get_db()
    summary = seed(db)
    print(f"Seed complete: {summary}")


if __name__ == "__main__":
    main()
