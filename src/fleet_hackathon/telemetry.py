"""Agent Observability — the Fleet track's Telemetry component.

Every Gateway-dispatched action (and every blocked/drifted one) is written here
as one audit-log entry. Field names follow OTel semantic-convention shape
(a `trace_id` correlating one triggered run, `attributes` carrying the
domain-specific fields) without pulling in the full OTel SDK for a demo-scale
system — see `runtime.py` for where a real OTel exporter would sit if this grew
past hackathon scale.
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from fleet_hackathon.config import AUDIT_COUNTER_DOC, COLLECTION_AUDIT_LOG, COLLECTION_STATS


@dataclass
class AuditEntry:
    trace_id: str
    agent_name: str
    action: str
    status: str  # "ok" | "blocked" | "escalated" | "drift"
    detail: str
    success_criterion: str
    drift_detected: bool = False
    attributes: dict = field(default_factory=dict)
    # Gemini-written, one sentence, optional. Top-level rather than inside
    # `attributes` because that field already carries two unrelated things (the
    # raw action result dict from Gateway._execute, and per-agent extras like
    # outreach_check's model_armor note) — model output is a third semantics.
    # Entries written before this shipped have no such key, and both shapes stay
    # live until the log turns over, so every reader must use .get("narration").
    narration: str | None = None
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "agent_name": self.agent_name,
            "action": self.action,
            "status": self.status,
            "detail": self.detail,
            "success_criterion": self.success_criterion,
            "drift_detected": self.drift_detected,
            "attributes": self.attributes,
            "narration": self.narration,
            "timestamp": self.timestamp,
        }


class AuditLogger:
    def __init__(self, db):
        self._db = db

    def new_trace_id(self) -> str:
        return uuid.uuid4().hex

    def log(
        self,
        trace_id: str,
        agent_name: str,
        action: str,
        status: str,
        detail: str,
        success_criterion: str,
        drift_detected: bool = False,
        attributes: dict | None = None,
        narration: str | None = None,
    ) -> AuditEntry:
        entry = AuditEntry(
            trace_id=trace_id,
            agent_name=agent_name,
            action=action,
            status=status,
            detail=detail,
            success_criterion=success_criterion,
            drift_detected=drift_detected,
            attributes=attributes or {},
            narration=narration,
        )
        self._db.collection(COLLECTION_AUDIT_LOG).add(entry.to_dict())
        return entry

    def list_recent(self, limit: int = 50) -> list[dict]:
        query = (
            self._db.collection(COLLECTION_AUDIT_LOG)
            .order_by("timestamp", direction="DESCENDING")
            .limit(limit)
        )
        return [doc.to_dict() for doc in query.stream()]

    def count_all(self) -> int | None:
        """Lifetime audit-entry count, or None if the backend can't aggregate.

        The dashboard renders only a capped slice, so it needs the true total
        separately. The log is also pruned now (demo_stream.prune_audit_log), so
        a plain count() of the live collection would report only what's retained
        — the pruner banks every deletion in a counter document, and lifetime is
        banked + currently stored.

        Both terms stay O(1): one document get, plus a count() aggregation that
        holds at ~1 read because pruning keeps the collection bounded. Before
        pruning existed this grew linearly — Firestore bills count() per 1,000
        index entries matched, so an unbounded log made every dashboard load
        progressively more expensive (projected ~120k-228k documents, i.e.
        120-228 reads per load, by the Oct 1 judging deadline).

        Returns None rather than raising when the backend has no aggregation
        support, so the caller can fall back — this renders a judge-facing page
        and a missing counter must never 500 it.
        """
        try:
            result = self._db.collection(COLLECTION_AUDIT_LOG).count().get()
            live = int(result[0][0].value)
        except Exception:  # noqa: BLE001 - deliberate fail-soft, see docstring
            return None
        return live + self._pruned_offset()

    def _pruned_offset(self) -> int:
        """How many entries the pruner has removed over this deployment's life.
        Missing counter (fresh project, or a backend without it) reads as 0,
        which degrades to "count what's stored" rather than failing."""
        try:
            snap = self._db.collection(COLLECTION_STATS).document(AUDIT_COUNTER_DOC).get()
        except Exception:  # noqa: BLE001 - see count_all
            return 0
        if not getattr(snap, "exists", False):
            return 0
        return int((snap.to_dict() or {}).get("pruned", 0))
