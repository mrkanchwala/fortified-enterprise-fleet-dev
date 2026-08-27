"""Agent Runtime — the Fleet track's Core Execution & State (background) component.

Cloud Scheduler hits `POST /run/{agent_name}` (wired in app.py) on a fixed
interval — explicit background/async execution, not an on-demand chatbot
turn. This module is the per-agent cycle dispatcher only; decision logic
lives in agents/*.py, enforcement lives in gateway.py.
"""

from datetime import datetime

from fleet_hackathon.agents import (
    account_management,
    analytics,
    invoice,
    outreach_check,
    payment_followup,
)
from fleet_hackathon.capability import ALL_SCOPES
from fleet_hackathon.config import COLLECTION_DRIFT_SCENARIO
from fleet_hackathon.gateway import Gateway
from fleet_hackathon.narration import get_narrator
from fleet_hackathon.notifier import get_notifier
from fleet_hackathon.registry import AgentRegistry
from fleet_hackathon.telemetry import AuditLogger

AGENT_NAMES = ("outreach_check", "invoice", "payment_followup", "account_management", "analytics")


def ensure_registered(db) -> None:
    """Idempotent — (re)declares all 5 agents' capability scopes. Safe to call
    on every cold start; Firestore `.set()` overwrites, it doesn't duplicate.
    A human's enable/disable toggle (registry.set_enabled) survives this —
    see AgentRegistry.declare's own docstring."""
    registry = AgentRegistry(db)
    for scope in ALL_SCOPES:
        registry.declare(scope)


def set_agent_enabled(db, agent_name: str, enabled: bool) -> None:
    """The one human-flipped governance switch — see capability.py's
    CapabilityScope.enabled docstring. Never called by the agents themselves."""
    if agent_name not in AGENT_NAMES:
        raise ValueError(f"unknown agent '{agent_name}', expected one of {AGENT_NAMES}")
    AgentRegistry(db).set_enabled(agent_name, enabled)


def _drift_scenario(db) -> dict | None:
    snap = db.collection(COLLECTION_DRIFT_SCENARIO).document("active_scenario").get()
    return snap.to_dict() if snap.exists else None


def run_agent_cycle(db, agent_name: str, now: datetime | None = None, narrator=None) -> list[dict]:
    """`now` (2026-08-10): outreach_check, payment_followup, and analytics
    run_cycles all accept an injectable reference time — a test-only override
    so a deterministic seed fixture (an "within SLA" lead, a "35 days
    overdue" invoice, a "trailing 30 days" window) doesn't silently drift out
    of sync as real calendar days pass since the fixture was written.
    Production callers (Cloud Scheduler) never pass this; each agent defaults
    to real wall-clock time internally when `now` is None. invoice and
    account_management have no time-window-dependent logic, so they don't
    take this param at all."""
    if agent_name not in AGENT_NAMES:
        raise ValueError(f"unknown agent '{agent_name}', expected one of {AGENT_NAMES}")

    registry = AgentRegistry(db)
    audit_logger = AuditLogger(db)
    # `narrator` is threaded in by run_all_cycles so all five agents share one
    # per-tick budget. Constructing one here per agent would multiply the
    # ceiling by five and put /tick back over its 300s deadline.
    gateway = Gateway(
        db, registry, audit_logger, notifier=get_notifier(), narrator=narrator or get_narrator(db)
    )

    if agent_name == "outreach_check":
        return outreach_check.run_cycle(db, gateway, audit_logger, now=now)
    if agent_name == "invoice":
        return invoice.run_cycle(db, gateway, audit_logger)
    if agent_name == "account_management":
        return account_management.run_cycle(db, gateway, audit_logger)
    if agent_name == "analytics":
        return analytics.run_cycle(db, gateway, audit_logger, now=now)

    # payment_followup — checks for a seeded demo drift scenario each cycle
    scenario = _drift_scenario(db)
    force_action = None
    if scenario:
        force_action = {
            scenario["invoice_id"]: {
                "action": scenario["forced_action"],
                "kwargs": scenario.get("forced_kwargs", {}),
            }
        }
    return payment_followup.run_cycle(db, gateway, audit_logger, force_action=force_action, now=now)


def run_all_cycles(db) -> dict[str, list[dict]]:
    """One narrator for the whole beat — the narration ceiling is per tick, not
    per agent (see narration.GeminiNarrator)."""
    narrator = get_narrator(db)
    return {name: run_agent_cycle(db, name, narrator=narrator) for name in AGENT_NAMES}
