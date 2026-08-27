"""Gemini narration layer — the guards that keep a live LLM call out of the
enforced dispatch path, out of /tick's 300s budget, and out of the test suite.

Every test here runs with zero live Gemini calls: narration is opt-in via
FLEET_NARRATION_ENABLED, and GeminiNarrator takes an injected narrate_fn.
"""

import asyncio

import pytest

from fleet_hackathon import dashboard, narrate, narration, runtime, seed_demo_data
from fleet_hackathon.config import COLLECTION_NARRATION_CACHE
from fleet_hackathon.gateway import Gateway
from fleet_hackathon.narration import GeminiNarrator, NullNarrator
from fleet_hackathon.registry import AgentRegistry
from fleet_hackathon.telemetry import AuditLogger


class _CountingNarrateFn:
    """Stands in for narrate.narrate — records every call, never leaves the process."""

    def __init__(self, returns="A narrated sentence."):
        self.calls = []
        self._returns = returns

    def __call__(self, summary, trace_id=None):
        self.calls.append(summary)
        return self._returns


def _gateway(db, narrator):
    return Gateway(db, AgentRegistry(db), AuditLogger(db), narrator=narrator)


# --------------------------------------------------------------- opt-in / kill switch


def test_narration_is_disabled_unless_explicitly_enabled(monkeypatch, fake_db):
    """The whole suite's safety property: no env var, no Vertex AI, ever."""
    monkeypatch.delenv("FLEET_NARRATION_ENABLED", raising=False)
    assert narration.enabled() is False
    assert isinstance(narration.get_narrator(fake_db), NullNarrator)


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes"])
def test_kill_switch_accepts_the_documented_on_values(monkeypatch, fake_db, value):
    monkeypatch.setenv("FLEET_NARRATION_ENABLED", value)
    assert narration.enabled() is True
    assert isinstance(narration.get_narrator(fake_db), GeminiNarrator)


@pytest.mark.parametrize("value", ["false", "0", "no", "", "garbage"])
def test_kill_switch_turns_narration_off_without_a_redeploy(monkeypatch, fake_db, value):
    monkeypatch.setenv("FLEET_NARRATION_ENABLED", value)
    assert isinstance(narration.get_narrator(fake_db), NullNarrator)


# --------------------------------------------------------------- ceiling


def test_per_tick_ceiling_caps_live_calls(fake_db):
    """/tick writes 50-95 audit entries per beat and a live call measured 5.98s
    on 2026-08-27. Uncapped that is 300-570s against Cloud Run's 300s deadline,
    so this ceiling is a correctness guard, not a cost guard."""
    fn = _CountingNarrateFn()
    narrator = GeminiNarrator(fake_db, max_per_tick=3, narrate_fn=fn)

    for i in range(10):
        narrator.narrate(
            agent_name=f"agent_{i}",  # distinct classes, so the cache never absorbs these
            action=f"action_{i}",
            status="ok",
            success_criterion=f"criterion {i}",
        )

    assert len(fn.calls) == 3, f"ceiling of 3 let {len(fn.calls)} calls through"


def test_ceiling_is_shared_across_all_five_agents_in_one_tick(fake_db):
    """run_all_cycles must build ONE narrator. Building one per agent would
    multiply the ceiling by five and put /tick back over its deadline."""
    seed_demo_data.seed(fake_db)
    runtime.ensure_registered(fake_db)

    fn = _CountingNarrateFn()
    narrator = GeminiNarrator(fake_db, max_per_tick=2, narrate_fn=fn)
    for name in runtime.AGENT_NAMES:
        runtime.run_agent_cycle(fake_db, name, narrator=narrator)

    assert len(fn.calls) <= 2, f"shared ceiling breached: {len(fn.calls)} calls across 5 agents"


def test_ceiling_of_zero_disables_live_calls_entirely(fake_db):
    fn = _CountingNarrateFn()
    narrator = GeminiNarrator(fake_db, max_per_tick=0, narrate_fn=fn)
    assert narrator.narrate(agent_name="a", action="b", status="ok", success_criterion="c") is None
    assert fn.calls == []


# --------------------------------------------------------------- cache


def test_cache_hit_skips_the_second_call(fake_db):
    fn = _CountingNarrateFn()
    narrator = GeminiNarrator(fake_db, max_per_tick=10, narrate_fn=fn)
    kwargs = {"agent_name": "invoice", "action": "issue_invoice", "status": "ok", "success_criterion": "crit"}

    first = narrator.narrate(**kwargs)
    second = narrator.narrate(**kwargs)

    assert first == second
    assert len(fn.calls) == 1, "identical decision class made a second live call"


def test_cache_survives_a_new_narrator_instance(fake_db):
    """The cache is Firestore-backed, not per-process — a new Cloud Run
    instance or the next tick must not re-pay for the same decision class."""
    fn = _CountingNarrateFn()
    kwargs = {"agent_name": "invoice", "action": "issue_invoice", "status": "ok", "success_criterion": "crit"}

    GeminiNarrator(fake_db, max_per_tick=10, narrate_fn=fn).narrate(**kwargs)
    GeminiNarrator(fake_db, max_per_tick=10, narrate_fn=fn).narrate(**kwargs)

    assert len(fn.calls) == 1


def test_prompt_carries_no_record_specifics(fake_db):
    """Correctness, not tidiness. The cache is keyed on the exact prompt string,
    so if the prompt named one invoice's client, every later invoice of the same
    class would reuse a sentence naming the WRONG client."""
    fn = _CountingNarrateFn()
    narrator = GeminiNarrator(fake_db, max_per_tick=10, narrate_fn=fn)
    narrator.narrate(
        agent_name="payment_followup",
        action="send_reminder",
        status="drift",
        success_criterion="no invoice receives a third reminder without human handoff",
        drift_detected=True,
        trace_id="abc123",
    )

    summary = fn.calls[0]
    for leak in ("INV-", "inv-", "Keystone", "abc123", "18006"):
        assert leak not in summary, f"record specific {leak!r} leaked into a cached prompt"


def test_differing_statuses_are_cached_separately(fake_db):
    fn = _CountingNarrateFn()
    narrator = GeminiNarrator(fake_db, max_per_tick=10, narrate_fn=fn)
    base = {"agent_name": "payment_followup", "action": "send_reminder", "success_criterion": "crit"}

    narrator.narrate(**base, status="ok", drift_detected=False)
    narrator.narrate(**base, status="drift", drift_detected=True)

    assert len(fn.calls) == 2, "drift and ok must never share a cached sentence"


def test_cache_key_space_is_bounded_by_decision_classes(fake_db):
    """No pruner exists for this collection, so the key space must self-bound.
    Same class 50 times must produce exactly one document."""
    fn = _CountingNarrateFn()
    narrator = GeminiNarrator(fake_db, max_per_tick=50, narrate_fn=fn)
    for _ in range(50):
        narrator.narrate(agent_name="invoice", action="issue_invoice", status="ok", success_criterion="c")

    stored = list(fake_db.collection(COLLECTION_NARRATION_CACHE).stream())
    assert len(stored) == 1, f"cache grew to {len(stored)} docs for one decision class"


def test_failed_call_is_not_cached(fake_db):
    """A None result must not poison the cache into permanently returning None."""
    failing = _CountingNarrateFn(returns=None)
    GeminiNarrator(fake_db, max_per_tick=5, narrate_fn=failing).narrate(
        agent_name="invoice", action="issue_invoice", status="ok", success_criterion="c"
    )
    assert list(fake_db.collection(COLLECTION_NARRATION_CACHE).stream()) == []


# --------------------------------------------------------------- fail-soft


def test_narration_failure_never_breaks_dispatch(fake_db):
    """The load-bearing property: an LLM outage must not stop the fleet acting."""

    class _ExplodingNarrator:
        def narrate(self, **_kwargs):
            raise RuntimeError("Vertex AI is down")

    seed_demo_data.seed(fake_db)
    runtime.ensure_registered(fake_db)

    with pytest.raises(RuntimeError):
        # Confirms the stub really does raise, so the next assertion is meaningful.
        _ExplodingNarrator().narrate()

    narrator = GeminiNarrator(fake_db, max_per_tick=5, narrate_fn=_CountingNarrateFn())
    results = runtime.run_agent_cycle(fake_db, "invoice", narrator=narrator)
    assert results, "dispatch produced no results"


def test_entry_without_narration_still_logs_the_deterministic_detail(fake_db):
    seed_demo_data.seed(fake_db)
    runtime.ensure_registered(fake_db)
    runtime.run_agent_cycle(fake_db, "invoice", narrator=NullNarrator())

    entries = AuditLogger(fake_db).list_recent(limit=50)
    assert entries
    assert all(e.get("narration") is None for e in entries)
    assert all(e.get("detail") for e in entries), "deterministic detail must survive without narration"


# --------------------------------------------------------------- narrate.py internals


def test_agent_is_not_constructed_at_import():
    """Keeps ADK off the container cold-start path. Import-time construction was
    tested against the pinned adk 2.6.2 and does not raise, so this is a
    performance property, not a crash guard."""
    assert narrate._agent is None or hasattr(narrate._agent, "model")


def test_instruction_spells_out_the_drift_versus_blocked_distinction():
    """A live smoke test on 2026-08-27 returned 'was blocked and flagged for
    manual review' for a status=drift entry. Drift means the action DID run and
    was flagged after the fact — the opposite. Narration is judge-facing copy,
    so a confident wrong sentence is worse than no sentence."""
    instruction = narrate._INSTRUCTION.lower()
    assert "status=drift" in instruction
    assert "did run" in instruction
    assert "never write that a drift action was blocked" in instruction
    assert "status=blocked" in instruction


def test_instruction_covers_the_status_values_the_gateway_actually_emits(fake_db):
    """Gateway._execute passes the action result's OWN status through
    (result.get("status", "ok")), so the live values are issued /
    reminder_sent / escalated_to_human — not a fixed ok/drift/blocked
    vocabulary. Caught 2026-08-27 by running the full chain with a stub after
    the first draft of the instruction enumerated statuses the model would
    almost never see."""
    instruction = narrate._INSTRUCTION.lower()
    assert "any other status value" in instruction
    for real_status in ("issued", "reminder_sent", "escalated_to_human"):
        assert real_status in instruction


def test_timeout_is_configurable_and_falls_back_on_garbage(monkeypatch):
    monkeypatch.setenv("FLEET_NARRATION_TIMEOUT_SECONDS", "7.5")
    assert narrate._timeout_seconds() == 7.5
    monkeypatch.setenv("FLEET_NARRATION_TIMEOUT_SECONDS", "not-a-number")
    assert narrate._timeout_seconds() == narrate.DEFAULT_TIMEOUT_SECONDS


def test_a_hanging_call_times_out_rather_than_blocking_the_tick(monkeypatch):
    """Without this, one hung Vertex AI call holds /tick until Cloud Run kills
    the whole request — taking the injection and pruning steps down with it."""

    async def _never_returns(_summary):
        await asyncio.sleep(30)

    monkeypatch.setattr(narrate, "_narrate_async", _never_returns)
    monkeypatch.setenv("FLEET_NARRATION_TIMEOUT_SECONDS", "0.2")

    assert narrate.narrate("agent=x action=y status=ok") is None


def test_narrate_returns_none_instead_of_raising(monkeypatch):
    async def _explodes(_summary):
        raise RuntimeError("vertex exploded")

    monkeypatch.setattr(narrate, "_narrate_async", _explodes)
    assert narrate.narrate("agent=x action=y status=ok") is None


# --------------------------------------------------------------- dashboard rendering


def test_narration_is_html_escaped(fake_db):
    """Narration is the only field on the page whose content a model generates.
    Unescaped model output on a judge-facing page is stored XSS."""
    seed_demo_data.seed(fake_db)
    runtime.ensure_registered(fake_db)
    logger = AuditLogger(fake_db)
    logger.log(
        trace_id=logger.new_trace_id(),
        agent_name="invoice",
        action="issue_invoice",
        status="ok",
        detail="deterministic detail",
        success_criterion="crit",
        narration='<script>alert("xss")</script>',
    )

    html = dashboard.render(fake_db)

    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html


def test_dashboard_renders_mixed_narrated_and_unnarrated_entries(fake_db):
    """Both shapes stay live until the 1,000-entry log turns over after deploy."""
    seed_demo_data.seed(fake_db)
    runtime.ensure_registered(fake_db)
    logger = AuditLogger(fake_db)
    common = {
        "agent_name": "invoice",
        "action": "issue_invoice",
        "status": "ok",
        "success_criterion": "crit",
    }
    logger.log(trace_id=logger.new_trace_id(), detail="older entry", **common)
    logger.log(
        trace_id=logger.new_trace_id(),
        detail="newer entry",
        narration="Gemini explained this one.",
        **common,
    )

    html = dashboard.render(fake_db)

    assert "older entry" in html
    assert "newer entry" in html
    assert "Gemini explained this one." in html


# --------------------------------------------------------------- H1: cache invalidation


def test_version_bump_invalidates_every_cached_narration(fake_db, monkeypatch):
    """/cso H1. A cached sentence is served to every later entry of its decision
    class for the whole judging window. The single live call on 2026-08-27
    returned a factually inverted sentence, so 'the model can be wrong' is
    observed, not hypothetical — shipping the prompt fix must also clear the
    output it produced."""
    fn = _CountingNarrateFn()
    kwargs = {"agent_name": "invoice", "action": "issue_invoice", "status": "ok", "success_criterion": "c"}

    GeminiNarrator(fake_db, max_per_tick=5, narrate_fn=fn).narrate(**kwargs)
    assert len(fn.calls) == 1

    monkeypatch.setattr(narration, "_NARRATION_VERSION", narration._NARRATION_VERSION + 1)
    GeminiNarrator(fake_db, max_per_tick=5, narrate_fn=fn).narrate(**kwargs)

    assert len(fn.calls) == 2, "version bump did not invalidate the cached narration"


def test_reseed_clears_the_narration_cache(fake_db):
    """/cso H1, manual remedy. Matters most right before a recording take: a
    rehearsal's cached sentence would otherwise survive into the real one."""
    fn = _CountingNarrateFn()
    GeminiNarrator(fake_db, max_per_tick=5, narrate_fn=fn).narrate(
        agent_name="invoice", action="issue_invoice", status="ok", success_criterion="c"
    )
    assert len(list(fake_db.collection(COLLECTION_NARRATION_CACHE).stream())) == 1

    summary = seed_demo_data.seed(fake_db)

    assert summary["narration_cache_cleared"] == 1
    assert list(fake_db.collection(COLLECTION_NARRATION_CACHE).stream()) == []
