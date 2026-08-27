"""Narrator — caching, rate limiting, and the kill switch around narrate.py.

Dependency-injected exactly like notifier.py: Gateway takes a `narrator=`, every
existing test runs against `NullNarrator` with zero live Gemini calls, and
gateway.py never imports narrate.py directly.

Three guards, each closing a specific measured risk:

1. **Per-tick ceiling.** A live call measured 5.98s on 2026-08-27. `/tick` writes
   roughly 50-95 audit entries per beat (see demo_stream.prune_audit_log) inside
   a request already bounded by Cloud Run's 300s deadline — the same deadline the
   audit pruner drains 500-at-a-time to respect. Uncapped narration would add
   300-570s and time the tick out every single time. The ceiling is a correctness
   guard, not a cost guard.

2. **Cache.** Keyed on the exact prompt string, so a cache hit is by construction
   the same question. See _summarize for why the prompt carries no record
   specifics.

3. **Kill switch.** FLEET_NARRATION_ENABLED=false disables narration without a
   redeploy, so a runaway can be stopped from the console.
"""

import hashlib
import logging
import os

from fleet_hackathon.config import COLLECTION_NARRATION_CACHE

logger = logging.getLogger(__name__)

DEFAULT_MAX_PER_TICK = 3

# Bump to invalidate every cached narration on the next deploy.
#
# Exists because a cached sentence is otherwise permanent: it is served to every
# future entry of its decision class, on the judge-facing page, for the whole
# judging window, and nothing in the app deletes it. That matters concretely —
# the single live call spent on 2026-08-27 returned "the action was blocked and
# flagged for manual review" for a status=drift entry, the exact inverse of what
# drift means here. Correcting the instruction does nothing about text already
# cached, so without a version in the key the only remedy would have been manual
# Firestore surgery against a live project. With it, shipping the prompt fix and
# clearing the bad output are the same action.
#
# Deliberately part of the KEY, not the prompt: the prompt stays the pure
# question being asked, so it remains readable as the thing the cache is keyed on.
_NARRATION_VERSION = 1


def _summarize(agent_name: str, action: str, status: str, drift_detected: bool, success_criterion: str) -> str:
    """The exact string sent to Gemini, and the exact thing cached.

    Deliberately carries NO record specifics — no invoice id, amount, client
    name, or elapsed-hours figure. That is a correctness requirement, not a
    style choice: the cache is keyed on this string, so if it named one
    invoice's client, every later invoice of the same decision class would
    reuse a sentence naming the wrong client. Class-level in, class-level out.

    The specifics are not lost to the reader — the deterministic
    Gateway._format_detail() sentence renders directly beside the narration on
    the dashboard, and it carries the exact ids and figures.
    """
    return (
        f"agent={agent_name} action={action} status={status} "
        f"drift_detected={str(drift_detected).lower()} "
        f"success_criterion='{success_criterion}'"
    )


def _cache_key(summary: str) -> str:
    return hashlib.sha256(f"v{_NARRATION_VERSION}|{summary}".encode()).hexdigest()


class NullNarrator:
    """Default everywhere except production. Mirrors NullNotifier."""

    def narrate(self, **_kwargs) -> None:
        return None


class GeminiNarrator:
    def __init__(self, db, max_per_tick: int | None = None, narrate_fn=None):
        self._db = db
        self._max = DEFAULT_MAX_PER_TICK if max_per_tick is None else max_per_tick
        self._calls_made = 0
        # Injected for tests so the suite never reaches Vertex AI.
        if narrate_fn is None:
            from fleet_hackathon import narrate as _narrate_module

            narrate_fn = _narrate_module.narrate
        self._narrate_fn = narrate_fn

    def narrate(
        self,
        agent_name: str,
        action: str,
        status: str,
        success_criterion: str,
        drift_detected: bool = False,
        trace_id: str | None = None,
    ) -> str | None:
        summary = _summarize(agent_name, action, status, drift_detected, success_criterion)
        key = _cache_key(summary)

        cached = self._read_cache(key)
        if cached is not None:
            return cached

        if self._calls_made >= self._max:
            logger.info("narration ceiling %d reached this tick, skipping trace_id=%s", self._max, trace_id)
            return None

        self._calls_made += 1
        text = self._narrate_fn(summary, trace_id=trace_id)
        if text:
            self._write_cache(key, text, summary)
        return text

    # Cache reads/writes fail soft for the same reason count_all() does: this
    # feeds a judge-facing page, and a Firestore hiccup must degrade to "no
    # narration", never to a 500.
    def _read_cache(self, key: str) -> str | None:
        try:
            snap = self._db.collection(COLLECTION_NARRATION_CACHE).document(key).get()
        except Exception:  # noqa: BLE001 - see comment above
            return None
        if not getattr(snap, "exists", False):
            return None
        return (snap.to_dict() or {}).get("narration")

    def _write_cache(self, key: str, text: str, summary: str) -> None:
        try:
            self._db.collection(COLLECTION_NARRATION_CACHE).document(key).set(
                {"narration": text, "summary": summary}
            )
        except Exception:  # noqa: BLE001 - see comment above
            logger.warning("narration cache write failed for key=%s", key[:12])


def enabled() -> bool:
    """Opt-IN, not opt-out. Defaults to off so that any caller without the env
    var — every test, every local run, every `pytest` in CI — cannot reach
    Vertex AI even by accident. Production turns it on explicitly via the Cloud
    Run env var, alongside GOOGLE_GENAI_USE_VERTEXAI. This is the standing
    cost-discipline rule enforced structurally rather than by convention."""
    return (os.environ.get("FLEET_NARRATION_ENABLED") or "false").strip().lower() in {
        "true",
        "1",
        "yes",
    }


def _max_per_tick() -> int:
    raw = os.environ.get("FLEET_NARRATION_MAX_PER_TICK")
    if not raw:
        return DEFAULT_MAX_PER_TICK
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning("bad FLEET_NARRATION_MAX_PER_TICK=%r, using default", raw)
        return DEFAULT_MAX_PER_TICK


def get_narrator(db):
    """One narrator per tick — see runtime.run_all_cycles. Constructing one per
    agent would multiply the ceiling by 5."""
    if not enabled():
        return NullNarrator()
    return GeminiNarrator(db, max_per_tick=_max_per_tick())
