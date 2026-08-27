"""FastAPI entrypoint — single Cloud Run service hosting both the public
read-only dashboard and the Cloud-Scheduler-triggered agent run endpoints, per
the build plan's single-service architecture call.

Route security model (deliberate, since Cloud Run's `--allow-unauthenticated`
is service-wide, not per-route, and the plan commits to one service):
- `GET /` and `GET /health` — public, read-only, no secret required.
- `POST /run/{agent_name}` — requires the `X-Fleet-Runtime-Token` header to
  match `FLEET_RUNTIME_TOKEN` (sourced from Secret Manager at deploy time,
  never hardcoded — see Step 13's README credential-handling section). Cloud
  Scheduler's HTTP target config sets this header; the dashboard page never
  links to or exposes this route to a visitor.

Rate limiting (2026-08-10, CSO HIGH finding fix): once deployed, this URL is
public and judge-discoverable, and nothing previously throttled repeated
requests to any route — including the token-gated mutating ones, which
combined with a non-constant-time token comparison made a brute-force attempt
theoretically practical. Fixed with a minimal in-memory sliding-window
limiter (no new dependency — matches this codebase's existing preference for
small, testable, self-contained logic over pulling in a library for a
narrowly-scoped need, e.g. model_armor.py). Caveat, stated honestly: this is
per-instance, in-memory state — correct for a scale-to-zero demo service that
runs as one instance most of the time, but not a substitute for an
infra-layer control (Cloud Armor) if this were carrying real traffic. The
token comparison now uses `hmac.compare_digest` (constant-time) instead of
`!=`, and every failed token check is logged with the caller's IP so a
brute-force attempt would actually be visible.
"""

import hmac
import logging
import os
import time
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse

from fleet_hackathon import actions, dashboard, demo_stream, runtime, seed_demo_data
from fleet_hackathon.firestore_client import get_db

# Cloud Run captures stdout/stderr, but uvicorn only configures its OWN loggers
# (uvicorn.access and friends). This app's module loggers inherit the root
# logger, which defaults to WARNING — so every logger.info() in this codebase
# was silently dropped in production. Found 2026-08-27 when narrate.py's
# "gemini_call" line, written specifically as deploy-time evidence that real
# Vertex AI traffic is happening, produced nothing in Cloud Logging while the
# narration itself was demonstrably working on the live page.
logging.basicConfig(
    level=os.environ.get("FLEET_LOG_LEVEL", "INFO").upper(),
    format="%(levelname)s %(name)s %(message)s",
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    runtime.ensure_registered(get_db())
    yield


app = FastAPI(title="Fortified Enterprise Fleet", lifespan=_lifespan)

# --- Rate limiting ---------------------------------------------------------

_RATE_LIMIT_WINDOW_SECONDS = 60
_RATE_LIMITS = {
    "public": 30,  # GET / and /health — dashboard viewing
    "mutating": 10,  # the 6 token-gated POST routes
}
_request_log: dict[tuple[str, str], list[float]] = defaultdict(list)


# Proxies we operate, and therefore trust to have appended honestly. Anything
# else in the chain may have been forged by the caller. Comma-separated env var.
_TRUSTED_PROXY_IPS = frozenset(
    part.strip() for part in (os.environ.get("TRUSTED_PROXY_IPS") or "").split(",") if part.strip()
)


def _client_ip(request: Request) -> str:
    """Rightmost X-Forwarded-For entry that isn't one of our own proxies.

    This previously returned the FIRST entry, which is simply whatever the
    caller chose to send, so a rotating X-Forwarded-For defeated the rate
    limiter outright — measured 2026-08-12: 40/40 requests sailed through a
    30/min bucket, while the same 40 with a fixed header correctly produced 10
    429s. Every proxy in the chain *appends*, so a value the client forged can
    only ever sit to the LEFT of one a real proxy added. Walking right-to-left
    and skipping our own proxies lands on the first entry the caller could not
    have controlled.

    Correct for both ingress paths: straight to the run.app URL (Google appends
    the true client IP last), and via the nginx reverse proxy on the Quadriga
    VPS (nginx appends the true client, then Google appends the VPS IP, which
    TRUSTED_PROXY_IPS skips — without that every visitor arriving through
    quadrigasolutions.com would share one bucket and throttle each other).
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        for candidate in reversed([p.strip() for p in forwarded.split(",") if p.strip()]):
            if candidate not in _TRUSTED_PROXY_IPS:
                return candidate
    return request.client.host if request.client else "unknown"


def _check_rate_limit(request: Request, bucket: str) -> None:
    limit = _RATE_LIMITS[bucket]
    ip = _client_ip(request)
    key = (bucket, ip)
    now = time.monotonic()
    window_start = now - _RATE_LIMIT_WINDOW_SECONDS

    recent = [t for t in _request_log[key] if t > window_start]
    if len(recent) >= limit:
        logger.warning(f"rate limit exceeded: bucket={bucket} ip={ip}")
        raise HTTPException(status_code=429, detail="rate limit exceeded, try again shortly")
    recent.append(now)
    _request_log[key] = recent


# --- Auth --------------------------------------------------------------


def _require_runtime_token(request: Request, x_fleet_runtime_token: str | None) -> None:
    # Secret Manager payloads are routinely created with `echo`, which appends a
    # trailing newline, and Cloud Run injects the payload verbatim. An HTTP header
    # value can never contain a bare newline, so an unstripped expected token makes
    # every token-gated route permanently unreachable -- which is exactly what
    # happened in production on 2026-08-11. Strip both sides defensively.
    expected = (os.environ.get("FLEET_RUNTIME_TOKEN") or "").strip()
    provided = (x_fleet_runtime_token or "").strip()
    if not expected or not provided or not hmac.compare_digest(provided, expected):
        logger.warning(f"rejected request: missing or invalid runtime token, ip={_client_ip(request)}")
        raise HTTPException(status_code=403, detail="missing or invalid runtime token")


@app.get("/health")
def health(request: Request) -> dict:
    _check_rate_limit(request, "public")
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> str:
    _check_rate_limit(request, "public")
    return dashboard.render(get_db())


@app.post("/run/{agent_name}")
def run_agent(agent_name: str, request: Request, x_fleet_runtime_token: str | None = Header(default=None)) -> dict:
    _check_rate_limit(request, "mutating")
    _require_runtime_token(request, x_fleet_runtime_token)
    try:
        results = runtime.run_agent_cycle(get_db(), agent_name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"agent": agent_name, "results": results}


@app.post("/run-all")
def run_all(request: Request, x_fleet_runtime_token: str | None = Header(default=None)) -> dict:
    _check_rate_limit(request, "mutating")
    _require_runtime_token(request, x_fleet_runtime_token)
    return runtime.run_all_cycles(get_db())
@app.post("/tick")
def tick(request: Request, x_fleet_runtime_token: str | None = Header(default=None)) -> dict:
    """One scheduled beat of the live demo: inject work, run the fleet, prune.

    Called by the fleet-tick Cloud Scheduler job every 30 minutes. The three
    steps are deliberately one endpoint rather than three scheduled jobs, so
    they can never interleave or run out of order (pruning before the agents
    have acted would delete work nobody ever saw happen).

    Injection and pruning are pure data operations in demo_stream — they never
    trigger or modify agents, preserving the "system never modifies its own
    fleet" boundary the entry is built around.
    """
    _check_rate_limit(request, "mutating")
    _require_runtime_token(request, x_fleet_runtime_token)
    db = get_db()
    stream = demo_stream.tick(db)
    agents = runtime.run_all_cycles(db)
    # After the agents have written this beat's entries, not before — pruning
    # first would trim a window the fleet is about to add to.
    audit_pruned = demo_stream.prune_audit_log(db)
    return {"stream": stream, "agents": agents, "audit_pruned": audit_pruned}
@app.post("/reseed")
def reseed(request: Request, x_fleet_runtime_token: str | None = Header(default=None)) -> dict:
    """Resets the demo dataset to its initial state.

    Exists because Cloud Scheduler can only call HTTP endpoints, and seeding
    was previously CLI-only (seed_demo_data.main). The scheduled fleet-reseed
    job calls this daily: the 5 agents run every 30 minutes and would
    otherwise work through the finite demo dataset within days, leaving a
    static dashboard for the rest of the judging window.

    Clears COLLECTION_AUDIT_LOG and COLLECTION_CASH_EVENTS as well as the
    entity collections (see seed_demo_data's own notes) so a reseed is a true
    reset, not an overlay on stale history.
    """
    _check_rate_limit(request, "mutating")
    _require_runtime_token(request, x_fleet_runtime_token)
    return seed_demo_data.seed(get_db())


@app.post("/mark-paid/{invoice_id}")
def mark_paid(invoice_id: str, request: Request, x_fleet_runtime_token: str | None = Header(default=None)) -> dict:
    """Simulates a payment landing (a real deployment would wire this to a
    payment-processor webhook) — not an agent action, so it bypasses the
    Gateway entirely, same as seed_demo_data.py injecting initial state.
    Demo use: trigger this live, then /run payment_followup (shows
    'already paid') and /run account_management (hands off) to close the
    loop on camera."""
    _check_rate_limit(request, "mutating")
    _require_runtime_token(request, x_fleet_runtime_token)
    try:
        result = actions.record_payment(get_db(), invoice_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return result


@app.post("/toggle-agent/{agent_name}")
def toggle_agent(
    agent_name: str, enabled: bool, request: Request, x_fleet_runtime_token: str | None = Header(default=None)
) -> dict:
    """The one human-flipped governance switch (query param `enabled=true|false`)
    — the system never calls this on itself, only a human via this endpoint."""
    _check_rate_limit(request, "mutating")
    _require_runtime_token(request, x_fleet_runtime_token)
    try:
        runtime.set_agent_enabled(get_db(), agent_name, enabled)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"agent": agent_name, "enabled": enabled}
