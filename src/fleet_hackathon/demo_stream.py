"""Continuous demo-data stream — keeps the fleet doing real work between now
and the end of the judging window.

Why this exists
---------------
The curated seed set (`seed_demo_data.seed`) is finite. The scheduled fleet
runs every 30 minutes, so within a couple of days every seeded lead would be
escalated and every seeded invoice paid or escalated, leaving a static
dashboard for the remaining weeks of judging. A daily full reseed was the
alternative, but it shows judges the identical dataset every day and anyone
landing just after a reset sees a near-empty board.

So each tick injects a small amount of fresh work and prunes the oldest
injected records, keeping the working set bounded instead of growing to
thousands of documents over ~2,350 scheduled runs.

Boundaries
----------
* Only ever touches records it created itself, identified by the `-str-` id
  infix and an `injected: True` marker. The curated seed records (the "hero"
  invoices the demo narrative depends on, lead-001/002, deal-001) are never
  pruned, so the scripted demo moments stay intact.
* Pure data injection — it does not call agents, does not decide anything, and
  writes no audit entries. Agents observe the new records on their next cycle
  exactly as they observe seeded ones. This keeps the "system never modifies
  its own fleet" boundary intact.
* `now` and `rng` are both injectable, matching the codebase's existing
  time-bomb fix pattern, so tests are deterministic.
"""

import random
from datetime import UTC, datetime, timedelta

from fleet_hackathon.config import (
    AUDIT_COUNTER_DOC,
    COLLECTION_AUDIT_LOG,
    COLLECTION_DEALS,
    COLLECTION_INVOICES,
    COLLECTION_LEADS,
    COLLECTION_STATS,
)

# Marker embedded in every generated id. Prune refuses to delete anything
# without it, so a bug here can never eat the curated seed data.
STREAM_MARKER = "-str-"

# Max injected records kept per collection. Beyond this the oldest are pruned.
# 12 keeps the dashboard readable while still always showing recent movement;
# combined with ~2 injected records per collection per tick that is roughly a
# 3-hour rolling window of activity.
MAX_INJECTED_PER_COLLECTION = 12

# Real client names sampled from crm_demo.db's `clients` table (1,000 rows) —
# the same provenance discipline seed_demo_data.py already follows for the
# portfolio set, so injected records are indistinguishable in style from the
# curated ones. Deliberately excludes the accounts the curated "hero" invoices
# use (Westfield Technologies Group, Keystone Partners Group, Pinnacle
# Logistics LLC, Beacon Enterprises) so a judge never sees the scripted demo
# account appear twice with unrelated invoices.
_COMPANIES = [
    "North Networks AG", "Blue Group AG", "Catalyst Systems", "Horizon Manufacturing",
    "Sterling Health", "Atlas Capital BV", "Catalyst Retail", "Onyx Partners",
    "Pinnacle Dynamics AG", "Beacon Enterprises GmbH", "Granite Analytics GmbH",
    "Crest Consulting", "North Solutions Group", "Sterling Retail",
    "Stonebridge Consulting", "Highpoint Enterprises Inc", "Vertex Systems AG",
    "Emerald Dynamics", "Solstice Retail Ltd", "Westfield Capital", "Solstice Labs",
    "Lighthouse Works", "Westfield Ventures Co",
]

# Real people from crm_demo.db's `sales_managers` table. The first four are the
# same managers the curated invoices already name, so account ownership stays
# consistent across seeded and injected records.
_MANAGERS = [
    "Anya Petrov", "Sara Ortiz", "Nadia Larsson", "Tom Nguyen", "Raj Petrov",
    "Noah Ortiz", "Maya Reyes", "Aisha Dubois", "Diego Okafor", "Sara Rossi",
]

# No contact-name column exists in crm_demo.db, so lead contacts reuse the same
# real person-name pool rather than inventing a second naming style.
_CONTACTS = _MANAGERS


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _suffix(now: datetime, n: int) -> str:
    """Unique, sortable id suffix. Second-resolution timestamp plus an index,
    so two records created in the same tick never collide."""
    return f"{int(now.timestamp())}-{n}"


def inject(db, now: datetime | None = None, rng: random.Random | None = None) -> dict:
    """Adds one tick's worth of new work.

    Deliberately injects an already-overdue invoice rather than relying on
    invoices issued from injected deals: `actions.issue_invoice` sets due_ts to
    now + INVOICE_DUE_WINDOW_DAYS, so those sit in 'issued' and generate no
    Payment-Followup or Account-Management activity for weeks. Injecting an
    overdue one directly is what keeps those two agents visibly working.
    """
    now = now or datetime.now(UTC)
    rng = rng or random.Random()
    summary = {"leads": 0, "deals": 0, "invoices": 0}

    company = rng.choice(_COMPANIES)
    contact = rng.choice(_CONTACTS)

    # --- A new lead. Age is randomised across the 48h SLA boundary so some
    # ticks produce a breach for Outreach-Check to escalate and some don't --
    lead_id = f"lead{STREAM_MARKER}{_suffix(now, 1)}"
    age_hours = rng.randint(10, 70)
    db.collection(COLLECTION_LEADS).document(lead_id).set(
        {
            "lead_id": lead_id,
            "contact_name": contact,
            "company": company,
            "first_contact_ts": _iso(now - timedelta(hours=age_hours)),
            "sla_window_hours": 48,
            "last_touch_ts": None if rng.random() < 0.5 else _iso(now - timedelta(hours=rng.randint(1, 8))),
            "status": "new",
            "injected": True,
            "injected_ts": _iso(now),
        }
    )
    summary["leads"] += 1

    # --- A closed-won deal with no invoice yet, so the Invoice Agent issues
    # one on its next cycle (its own autonomous path, unchanged) ------------
    deal_id = f"deal{STREAM_MARKER}{_suffix(now, 2)}"
    db.collection(COLLECTION_DEALS).document(deal_id).set(
        {
            "deal_id": deal_id,
            "account": rng.choice(_COMPANIES),
            "amount": round(rng.uniform(4_000, 45_000), 2),
            "currency": "USD",
            "close_ts": _iso(now - timedelta(hours=rng.randint(1, 6))),
            "status": "closed_won",
            "injected": True,
            "injected_ts": _iso(now),
        }
    )
    summary["deals"] += 1

    # --- An overdue invoice, so Payment-Followup has real work ------------
    fu_deal_id = f"deal{STREAM_MARKER}{_suffix(now, 3)}"
    invoice_id = f"inv{STREAM_MARKER}{_suffix(now, 3)}"
    account = rng.choice(_COMPANIES)
    amount = round(rng.uniform(6_000, 38_000), 2)
    days_overdue = rng.randint(3, 40)

    # ~1 tick in 3 lands a already-paid invoice on a delivered deal instead,
    # which is the Account-Management handoff path (it requires status 'paid'
    # AND the deal marked delivered — the fail-closed gate added 2026-08-10).
    is_paid = rng.random() < 0.33

    db.collection(COLLECTION_DEALS).document(fu_deal_id).set(
        {
            "deal_id": fu_deal_id,
            "account": account,
            "amount": amount,
            "currency": "USD",
            "close_ts": _iso(now - timedelta(days=days_overdue + 30)),
            "status": "closed_won",
            "delivery_status": "delivered" if is_paid else "in_progress",
            "injected": True,
            "injected_ts": _iso(now),
        }
    )
    summary["deals"] += 1

    db.collection(COLLECTION_INVOICES).document(invoice_id).set(
        {
            "invoice_id": invoice_id,
            "deal_id": fu_deal_id,
            "account": account,
            "account_manager": rng.choice(_MANAGERS),
            "amount": amount,
            "currency": "USD",
            "due_ts": _iso(now - timedelta(days=days_overdue)),
            "reminders_sent": rng.randint(0, 2),
            "status": "paid" if is_paid else "issued",
            "injected": True,
            "injected_ts": _iso(now),
        }
    )
    summary["invoices"] += 1

    return summary


def _prune_collection(db, collection_name: str, cap: int) -> list[str]:
    """Deletes the oldest injected docs beyond `cap`, oldest first.

    Prunes by age rather than by terminal status on purpose: if an agent were
    ever disabled, status-based pruning would stop reclaiming anything and the
    collection would grow without bound — exactly the failure this module
    exists to prevent.
    """
    injected = [
        doc
        for doc in db.collection(collection_name).stream()
        if STREAM_MARKER in doc.id and (doc.to_dict() or {}).get("injected") is True
    ]
    if len(injected) <= cap:
        return []

    injected.sort(key=lambda d: (d.to_dict() or {}).get("injected_ts") or "")
    doomed = injected[: len(injected) - cap]

    removed = []
    for doc in doomed:
        db.collection(collection_name).document(doc.id).delete()
        removed.append(doc.id)
        # A pruned deal's auto-issued invoice would otherwise be orphaned,
        # left referencing a deal that no longer exists.
        if collection_name == COLLECTION_DEALS:
            db.collection(COLLECTION_INVOICES).document(f"inv-{doc.id}").delete()
    return removed


def prune(db, cap: int = MAX_INJECTED_PER_COLLECTION) -> dict:
    """Bounds the working set. Only ever removes injected records."""
    return {
        "leads_removed": len(_prune_collection(db, COLLECTION_LEADS, cap)),
        "deals_removed": len(_prune_collection(db, COLLECTION_DEALS, cap)),
        "invoices_removed": len(_prune_collection(db, COLLECTION_INVOICES, cap)),
    }


MAX_AUDIT_LOG_ENTRIES = 1_000
# Drain gradually. The first run after this shipped had ~1,650 entries to clear,
# and deleting them all inside one scheduled request risks the 300s deadline.
AUDIT_PRUNE_PER_RUN = 500


def prune_audit_log(db, cap: int = MAX_AUDIT_LOG_ENTRIES, max_per_run: int = AUDIT_PRUNE_PER_RUN) -> int:
    """Bounds the audit log, banking what it removes.

    Nothing pruned this collection before, so it grew without limit — roughly
    50-95 entries per tick, 48 ticks a day, projecting to 120k-228k documents by
    the Oct 1 judging deadline. That matters beyond storage: count() is billed
    per 1,000 index entries matched, so an unbounded log turned every dashboard
    load into a linearly-growing read cost, and the free-tier read quota is what
    would have run out first (taking the page down for the rest of that day).

    Deletions are banked in a counter document so count_all() still reports the
    true lifetime total rather than merely what survived.

    Uses count() + order_by/limit rather than streaming the collection — a full
    stream would itself cost one read per stored document, every tick, which is
    the very cost this is here to remove.
    """
    try:
        total = int(db.collection(COLLECTION_AUDIT_LOG).count().get()[0][0].value)
    except Exception:  # noqa: BLE001 - a backend without count() simply skips pruning
        return 0
    excess = min(total - cap, max_per_run)
    if excess <= 0:
        return 0

    stale = db.collection(COLLECTION_AUDIT_LOG).order_by("timestamp").limit(excess).stream()
    removed = 0
    for doc in stale:
        db.collection(COLLECTION_AUDIT_LOG).document(doc.id).delete()
        removed += 1
    if not removed:
        return 0

    counter = db.collection(COLLECTION_STATS).document(AUDIT_COUNTER_DOC)
    snap = counter.get()
    banked = int((snap.to_dict() or {}).get("pruned", 0)) if getattr(snap, "exists", False) else 0
    counter.set({"pruned": banked + removed}, merge=True)
    return removed


def tick(db, now: datetime | None = None, rng: random.Random | None = None, cap: int = MAX_INJECTED_PER_COLLECTION) -> dict:
    """Inject, then prune. Agents are run separately by the caller so this
    module never triggers agent execution itself."""
    return {"injected": inject(db, now=now, rng=rng), "pruned": prune(db, cap=cap)}
