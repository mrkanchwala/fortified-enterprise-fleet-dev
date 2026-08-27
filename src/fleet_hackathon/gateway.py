"""Agent Gateway — the Fleet track's central Security & Governance dispatcher.

All 3 agents route every action through one Gateway instance instead of each
holding its own path to Firestore/side effects. Two things happen on every
call, in order:

1. Scope check — the action must be in the agent's declared CapabilityScope
   (see capability.py) or the call is refused before any side effect happens.
2. Drift check — even a scope-permitted call is compared against the agent's
   declared success_criterion; a call that technically fits the scope but
   violates the criterion (e.g. Payment-Followup sending a reminder past its
   own escalation threshold) is still executed for the demo's failure-catch
   scenario, but flagged drift_detected=True in the audit log, visible on the
   dashboard live.

An agent never imports actions.py directly — its only path to any effect is
`GatewayClient.call(action_name, **kwargs)`, obtained from `Gateway.client_for`.
"""

from collections.abc import Callable
from datetime import UTC, datetime

from fleet_hackathon import actions
from fleet_hackathon.config import (
    COLLECTION_INVOICES,
    OVERDUE_ESCALATION_DAYS,
    OVERDUE_ESCALATION_REMINDER_COUNT,
)
from fleet_hackathon.narration import NullNarrator
from fleet_hackathon.notifier import NullNotifier
from fleet_hackathon.registry import AgentRegistry
from fleet_hackathon.telemetry import AuditLogger


class CapabilityViolation(Exception):
    pass


class GatewayClient:
    """Per-agent handle. The agent object never holds a direct reference to any
    action function — only to this client, scoped at construction time."""

    def __init__(self, gateway: "Gateway", agent_name: str, allowed_actions: frozenset[str]):
        self._gateway = gateway
        self._agent_name = agent_name
        self._allowed_actions = allowed_actions

    def call(self, action: str, trace_id: str, **kwargs) -> dict:
        if action not in self._allowed_actions:
            self._gateway._log_blocked(self._agent_name, trace_id, action)
            raise CapabilityViolation(
                f"{self._agent_name} has no capability '{action}' "
                f"(declared scope: {sorted(self._allowed_actions)})"
            )
        return self._gateway._execute(self._agent_name, trace_id, action, **kwargs)


_ACTIONS: dict[str, Callable] = {
    "issue_invoice": actions.issue_invoice,
    "send_reminder": actions.send_reminder,
    "escalate_to_human": actions.escalate_to_human,
    "ping_human": actions.ping_human,
    "assign_account_manager": actions.assign_account_manager,
    "record_flag": actions.record_flag,
}

# Actions that genuinely dispatch outbound (2026-08-07 tightening pass) vs.
# ping_human, which stays Firestore-only on purpose — see actions.py.
_ACTIONS_WITH_NOTIFIER = frozenset(
    {"issue_invoice", "send_reminder", "escalate_to_human", "assign_account_manager"}
)


class Gateway:
    def __init__(self, db, registry: AgentRegistry, audit_logger: AuditLogger, notifier=None, narrator=None):
        self._db = db
        self._registry = registry
        self._audit = audit_logger
        self._notifier = notifier or NullNotifier()
        # Same dependency-injection contract as `notifier`: defaults to a no-op
        # so the whole test suite runs with zero live Gemini calls, per the
        # standing cost-discipline rule.
        self._narrator = narrator or NullNarrator()

    def client_for(self, agent_name: str) -> GatewayClient:
        scope = self._registry.get(agent_name)
        if scope is None:
            raise CapabilityViolation(f"agent '{agent_name}' has no declared capability scope in the registry")
        if not scope.enabled:
            raise CapabilityViolation(
                f"agent '{agent_name}' is disabled — a human turned it off via the Registry, "
                "the system never does this itself"
            )
        return GatewayClient(self, agent_name, scope.allowed_actions)

    def _log_blocked(self, agent_name: str, trace_id: str, action: str) -> None:
        scope = self._registry.get(agent_name)
        criterion = scope.success_criterion if scope else "unknown"
        self._audit.log(
            trace_id=trace_id,
            agent_name=agent_name,
            action=action,
            status="blocked",
            detail=f"action '{action}' outside declared capability scope",
            success_criterion=criterion,
            narration=self._narrator.narrate(
                agent_name=agent_name,
                action=action,
                status="blocked",
                success_criterion=criterion,
                drift_detected=False,
                trace_id=trace_id,
            ),
        )

    def _execute(self, agent_name: str, trace_id: str, action: str, **kwargs) -> dict:
        scope = self._registry.get(agent_name)
        criterion = scope.success_criterion if scope else "unknown"

        drift, drift_detail = self._check_drift(action, kwargs)

        if action in _ACTIONS_WITH_NOTIFIER:
            result = _ACTIONS[action](self._db, notifier=self._notifier, **kwargs)
        else:
            result = _ACTIONS[action](self._db, **kwargs)

        status = "drift" if drift else result.get("status", "ok")
        # Narration happens after the action has run and after the deterministic
        # detail exists, so an LLM failure can never affect whether the action
        # took place or what the audit log records about it.
        self._audit.log(
            trace_id=trace_id,
            agent_name=agent_name,
            action=action,
            status=status,
            detail=drift_detail if drift else self._format_detail(action, result),
            success_criterion=criterion,
            drift_detected=drift,
            attributes=result,
            narration=self._narrator.narrate(
                agent_name=agent_name,
                action=action,
                status=status,
                success_criterion=criterion,
                drift_detected=drift,
                trace_id=trace_id,
            ),
        )
        return result

    def _format_detail(self, action: str, result: dict) -> str:
        """One plain-English sentence per action — never the raw result dict.
        Written for a non-technical reader (finance team, not just a judge)."""
        dispatch = result.get("dispatch")
        dispatch_note = ""
        if dispatch is not None:
            if dispatch.get("test_mode"):
                dispatch_note = " — demo mode, no real email sent"
            elif dispatch.get("dispatched"):
                dispatch_note = " — email sent"
            else:
                dispatch_note = " — email failed to send"

        if action == "issue_invoice":
            note = " (already sent, skipped duplicate)" if result.get("idempotent") else ""
            return f"Sent invoice {result['invoice_id']}{note}{dispatch_note}"
        if action == "send_reminder":
            return f"Sent reminder #{result['reminders_sent']} for invoice {result['invoice_id']}{dispatch_note}"
        if action == "escalate_to_human":
            return f"Handed invoice {result['invoice_id']} off to a team member{dispatch_note}"
        if action == "ping_human":
            return f"Handed lead {result['lead_id']} off to a team member"
        if action == "assign_account_manager":
            note = " (already handed off, no-op)" if result.get("idempotent") else ""
            return f"Invoice {result['invoice_id']} handed to {result.get('handed_off_to', 'an account manager')}{note}{dispatch_note}"
        if action == "record_flag":
            return f"{result['headline']} ({result['severity']})"
        return f"{action} executed: {result}"

    def _check_drift(self, action: str, kwargs: dict) -> tuple[bool, str]:
        """Deterministic drift checks, keyed by action name. Only send_reminder
        has a stated threshold to violate for now (Payment-Followup's graduated
        escalation) — extend this dict if other agents grow a drift-checkable
        criterion."""
        if action == "send_reminder":
            return self._check_reminder_drift(kwargs.get("invoice_id"))
        return False, ""

    def _check_reminder_drift(self, invoice_id: str | None) -> tuple[bool, str]:
        if not invoice_id:
            return False, ""
        snap = self._db.collection(COLLECTION_INVOICES).document(invoice_id).get()
        if not snap.exists:
            return False, ""
        invoice = snap.to_dict()

        reminders_sent = invoice.get("reminders_sent", 0)
        if reminders_sent >= OVERDUE_ESCALATION_REMINDER_COUNT:
            return True, (
                f"Invoice {invoice_id} already had {reminders_sent} reminders sent — this should have "
                "gone to a team member instead of another reminder. Caught and flagged for review."
            )

        due_ts = invoice.get("due_ts")
        if due_ts:
            days_overdue = (datetime.now(UTC) - datetime.fromisoformat(due_ts)).days
            if days_overdue >= OVERDUE_ESCALATION_DAYS:
                return True, (
                    f"Invoice {invoice_id} is {days_overdue} days overdue — this should have gone to a "
                    "team member instead of another reminder. Caught and flagged for review."
                )
        return False, ""
