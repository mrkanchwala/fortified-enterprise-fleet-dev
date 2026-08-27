"""Gemini narration layer, isolated from the decision logic on purpose.

Every agent's `decide()` function (agents/*.py) is pure Python — no LLM call,
fully unit-testable, and the actual enforcement path (Gateway/CapabilityScope)
never depends on this module. This is where the real Gemini call happens,
turning a structured decision into the plain-English narration the dashboard
shows next to the deterministic detail.

Reached ADK 2.6.2 via Vertex AI (`GOOGLE_GENAI_USE_VERTEXAI=TRUE`), model
`gemini-3.5-flash`, `GOOGLE_CLOUD_LOCATION=us` — the model 404s in
`us-central1`, where Firestore and Cloud Run independently live.

Two things this module does NOT own, deliberately: caching and rate limiting.
Both live in narration.py, so this stays a thin "one call in, one sentence out"
boundary that a test can replace wholesale.

Failures degrade to no narration and never raise — an LLM hiccup must never
block or mask the deterministic dispatch/audit path.
"""

import asyncio
import logging
import os

logger = logging.getLogger(__name__)

_APP_NAME = "fleet_narrator"
_USER_ID = "fleet-runtime"
_MODEL = "gemini-3.5-flash"

DEFAULT_TIMEOUT_SECONDS = 20.0

# The drift/blocked distinction is spelled out because a live smoke test on
# 2026-08-27 got it backwards: given a status=drift entry the model wrote that
# the action "was blocked and flagged for manual review", when drift means the
# opposite — the Gateway executed it and flagged it, which is the whole point of
# the failure-catch demo. Narration is judge-facing copy, so a confident wrong
# sentence is worse than no sentence.
_INSTRUCTION = (
    "You are given one autonomous agent's decision as structured facts. "
    "Write exactly one plain-English sentence explaining the decision and why, "
    "for a technical judge reading an audit log. No preamble, no markdown, no "
    "restating the input verbatim.\n"
    "The status field is the action's own outcome. Two values have a precise "
    "meaning you must not confuse:\n"
    "- status=drift: the action DID run, and was flagged afterwards for review "
    "because it violated the agent's own stated success criterion. It was NOT "
    "blocked or prevented. Never write that a drift action was blocked, "
    "stopped, or prevented.\n"
    "- status=blocked: the action was refused before it had any effect, because "
    "it is outside the agent's declared capability scope.\n"
    "Any other status value (for example issued, reminder_sent, "
    "escalated_to_human) describes an action that completed normally — say what "
    "it did, and never describe it as blocked or flagged."
)

# Built on first use rather than at import. Import-time construction was tested
# on 2026-08-27 against the pinned adk 2.6.2 and does NOT raise without
# credentials, so this is not load-bearing for correctness — it just keeps ADK
# off the container cold-start path.
_agent = None


def _get_agent():
    global _agent
    if _agent is None:
        from google.adk.agents.llm_agent import Agent

        _agent = Agent(
            model=_MODEL,
            name="narrator",
            description="Explains one autonomous agent decision in one plain-English sentence.",
            instruction=_INSTRUCTION,
        )
    return _agent


def _timeout_seconds() -> float:
    raw = os.environ.get("FLEET_NARRATION_TIMEOUT_SECONDS")
    if not raw:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        return float(raw)
    except ValueError:
        logger.warning("bad FLEET_NARRATION_TIMEOUT_SECONDS=%r, using default", raw)
        return DEFAULT_TIMEOUT_SECONDS


async def _narrate_async(decision_summary: str) -> str:
    from google.adk.runners import InMemoryRunner
    from google.genai import types

    runner = InMemoryRunner(agent=_get_agent(), app_name=_APP_NAME)
    session = await runner.session_service.create_session(app_name=_APP_NAME, user_id=_USER_ID)
    message = types.Content(role="user", parts=[types.Part(text=decision_summary)])

    final_text = ""
    for event in runner.run(user_id=_USER_ID, session_id=session.id, new_message=message):
        if event.is_final_response() and event.content and event.content.parts:
            final_text = "".join(part.text or "" for part in event.content.parts)
    return final_text.strip()


async def _with_timeout(decision_summary: str, timeout: float) -> str:
    return await asyncio.wait_for(_narrate_async(decision_summary), timeout=timeout)


def narrate(decision_summary: str, trace_id: str | None = None) -> str | None:
    """One live Gemini call. Returns None (never raises) on any failure —
    narration is a demo enhancement, not a dependency of the enforced path.

    `asyncio.run` is legal here only because every route in app.py is a sync
    `def`, so Starlette runs it in a threadpool worker with no running event
    loop. Converting any calling route to `async def` would make this raise
    RuntimeError at runtime — see test_narration.py, which pins this.
    """
    try:
        text = asyncio.run(_with_timeout(decision_summary, _timeout_seconds()))
    except TimeoutError:
        logger.warning("gemini_call timeout trace_id=%s model=%s", trace_id, _MODEL)
        return None
    except Exception:
        logger.exception("gemini_call failed trace_id=%s model=%s", trace_id, _MODEL)
        return None
    # Structured line so the Cloud Run log carries visible proof of real Vertex
    # AI traffic — this is the evidence the hackathon eligibility check needs.
    logger.info("gemini_call ok trace_id=%s model=%s chars=%d", trace_id, _MODEL, len(text or ""))
    return text or None
