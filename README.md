# Fortified Enterprise Fleet

Built for the All Things Agentic hackathon, Fortified Enterprise Fleet track.

## Live demo

Dashboard (public, read-only): https://quadrigasolutions.com/agent-fleet/

Direct Cloud Run URL: https://agent-fleet-758180534444.us-central1.run.app

## What it does

Five agents run a small business's back office: Outreach-Check, Invoice, Payment-Followup, Account-Management, and Analytics. Each declares its own capability scope and success criterion before it runs. A shared dispatcher enforces that scope centrally, so an agent has no function or credential outside what it declared. Every action and every human escalation is logged and replayable in the dashboard's audit trail.

Outreach-Check has no email-send action in its declared scope. A bad first contact with a lead is hard to undo in a relationship sense, so that step stays gated to a human, on purpose. Invoice and Payment-Followup act autonomously within their declared scope, and their sends run through a real, tested Gmail SMTP notifier (dependency-injected, same pattern as the Firestore client). Payment-Followup varies its tone and frequency by days overdue, and escalates to a human once a stated threshold is crossed.

The live deployment runs that notifier on its no-op fallback: every send is still logged and audited, but no message leaves the service, because no Gmail credential was provisioned into the deployed project. A one-off local test using an already-authorized tool confirmed the same dispatch content lands in a real inbox, without adding a new credential to the deployed attack surface for a demo.

## Fleet track component mapping

Judged against the track's four named categories:

| Category | Component | Where it lives |
|---|---|---|
| Discovery & Lifecycle | Agent Registry | Firestore capability-declaration store |
| Core Execution & State | Memory Bank | Firestore per-agent state (e.g. Payment-Followup's `reminders_sent`) |
| Core Execution & State | Agent Runtime | Cloud Scheduler-driven periodic runs, background and async |
| Security & Governance | Agent Identity | Capability scope declared and enforced per agent |
| Security & Governance | Agent Gateway | Single dispatcher every agent routes actions through |
| Security & Governance | Model Armor | Prompt-injection and PII filter on any ingested text (e.g. simulated lead replies) |
| Telemetry | Agent Observability | Audit log and drift telemetry, fields aligned to OTel semantic conventions |

## Architecture

One FastAPI service on Cloud Run.

- `GET /` and `GET /health`: public, read-only dashboard.
- `POST /run/{agent_name}` and `POST /tick`: gated by an `X-Fleet-Runtime-Token` header, checked with a constant-time comparison, rate-limited per client IP.
- Cloud Scheduler calls `/tick` every 30 minutes. Each call injects a small batch of fresh work, runs all five agents over it, and prunes the oldest injected records.
- Firestore holds fleet state, the audit log, and demo CRM records. The audit log prunes itself on a cap so it can't grow unbounded.

## Data

The CRM data behind this demo is simulated, seeded by `seed_demo_data.py` and refreshed by the `/tick` cycle above. This is not real transaction history.

## What's real vs. roadmap

Built and live: all five agents, the capability, gateway, armor, and observability layers, the public dashboard, scheduled autonomous runs, and human escalation on the SLA and threshold cases described above.

Out of scope for this entry: account-management handoff to a human closer, project-status follow-through, a marketing case-study trigger, a CEO digest, HR performance tracking.

## Credential handling

`FLEET_RUNTIME_TOKEN` is provisioned through Secret Manager and injected at deploy time. It is never hardcoded or committed.

The Gmail notifier reads its credential from environment variables the deployed service never sets, by decision, not by omission: adding live email meant a new SMTP credential inside the deployed project, and that cost was judged not worth paying for a demo. See "What it does" above.

## Technologies used

Google ADK 2.6.2 and Gemini 3.5 Flash, reached through Vertex AI. Cloud Run hosts the service,
Firestore holds state, and Cloud Scheduler drives a 30 minute beat. FastAPI serves both the
dashboard and the agent run endpoints, with OpenTelemetry-shaped audit records and uv for
dependency management.

Each of the five agents reaches its decision in pure Python, which keeps the enforced dispatch
path deterministic and unit-testable. Gemini then writes the one sentence explanation that renders
beside that decision in the audit log, so the reasoning a judge reads was generated live by a real
model call. That call sits behind a per-tick ceiling and a Firestore cache, because a live call
takes around six seconds and the scheduled tick runs against a fixed request deadline.

## Run locally

```bash
uv sync
export GOOGLE_CLOUD_PROJECT=<your-gcp-project>
uv run uvicorn fleet_hackathon.app:app --reload
```

## Run the tests

```bash
uv run pytest
```

## Deploy

The production image builds from `Dockerfile` (`uv sync --frozen --no-dev`, non-root user, `uvicorn` on port 8080). Deployed to Cloud Run with `--min-instances=1` so the first visitor doesn't pay a cold start.

## Findings and learnings

- The rate limiter's first version checked `X-Forwarded-For`'s first entry, a value the caller controls directly. A rotating header put 40 of 40 requests through a 30-per-minute bucket meant to block exactly that. Fixed by walking the proxy chain from the right and skipping trusted hops.
- That fix, verified on the direct Cloud Run URL, still had a gap on the nginx-proxied vanity URL: traffic egressed over two IPs and split across two separate buckets. A fix verified on one ingress path is not a verified fix.
- A Secret Manager value created with `echo` carried a trailing newline. Every token-gated route rejected every request, silently, until the mismatch was traced to a 65-byte secret behind a 64-character token.
- The audit log had no pruning. Storage cost and page weight both grew unbounded until a cap and a server-side count were added.

## Built by

Quadriga Automations (https://quadrigasolutions.com), AI automation infrastructure for marketing, sales, operations, and engineering teams.

## License

MIT. See [LICENSE](LICENSE).
