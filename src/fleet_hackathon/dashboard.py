"""Read-only public demo dashboard — served from the same Cloud Run/FastAPI
service the agents run in (app.py), per the build plan's cost/simplicity call
(no second GCP service, no frontend framework).

Two views, deliberately kept apart (per direct instruction, 2026-08-07):
- **Executive Snapshot** — cash position (receivable/payable/net), computed
  against real 2026 industry benchmarks (analytics.py), rule-based gap flags
  (e.g. an account manager with real volume but a poor collection rate), and
  the Analytics Agent's 6-dimension flag panel (cash movements, operational
  bottlenecks, growth, sales pipeline, supplier relationship, payables
  backlog — agents/analytics.py). For a CFO/COO/CEO glance, not a technical
  audience.
- **Agentic View** — one kanban column per agent (Outreach / Invoicing /
  Payment Follow-up / Account Management / Analytics), each showing its own
  capability scope and its own recent activity. For anyone verifying what's
  actually automated and what's guarded.
- **Activity Log** — the full combined audit trail, kept at the bottom as
  detail for anyone who wants to verify everything, judge included.

Renders live from Firestore on every request — never a static/cached mock.
No route in this module accepts input or performs a write; mutation only
happens via app.py's separately secret-guarded endpoints, never reachable
from this page.
"""

import html
from datetime import UTC, datetime

from fleet_hackathon import analytics
from fleet_hackathon.assets import JETBRAINS_MONO_WOFF2_BASE64
from fleet_hackathon.config import (
    COLLECTION_ANALYTICS_FLAGS,
    COLLECTION_BILLS,
    COLLECTION_DRIFT_SCENARIO,
    COLLECTION_PORTFOLIO_INVOICES,
)
from fleet_hackathon.registry import AgentRegistry
from fleet_hackathon.telemetry import AuditLogger

_PILL_TONE = {
    "ok": "ok",
    "issued": "action",
    "reminder_sent": "action",
    "escalated": "attention",
    "escalated_to_human": "attention",
    "blocked": "attention",
    "drift": "danger",
    "flagged": "action",
}

_SEVERITY_TONE = {"ok": "ok", "watch": "attention", "attention": "danger"}
_SEVERITY_LABELS = {"ok": "OK", "watch": "Watch", "attention": "Needs attention"}

_DIMENSION_LABELS = {
    "cash_movements": "Cash movements",
    "operational_bottlenecks": "Operational bottlenecks",
    "growth": "Growth",
    "sales_pipeline": "Sales pipeline",
    "supplier_relationship": "Supplier relationship",
    "payables_backlog": "Payables backlog",
}

_DIMENSION_ORDER = (
    "cash_movements",
    "operational_bottlenecks",
    "growth",
    "sales_pipeline",
    "supplier_relationship",
    "payables_backlog",
)

_NEEDS_ATTENTION_STATUSES = {"escalated", "escalated_to_human", "blocked", "drift"}

# Plain-English labels for a non-technical viewer (finance team, not just a
# hackathon judge) — the underlying snake_case identifiers are what the code
# and tests actually key off; these dicts only affect what gets displayed.
_AGENT_LABELS = {
    "outreach_check": "Outreach Check",
    "invoice": "Invoicing",
    "payment_followup": "Payment Follow-up",
    "account_management": "Account Management",
    "analytics": "Analytics",
}

_ACTION_LABELS = {
    "ping_human": "Notify a team member",
    "issue_invoice": "Send invoice",
    "send_reminder": "Send payment reminder",
    "escalate_to_human": "Hand off to a team member",
    "assign_account_manager": "Hand off to account manager",
    "record_flag": "Flag for review",
    "no_action": "No action needed",
}

_STATUS_LABELS = {
    "ok": "OK",
    "issued": "Invoice sent",
    "reminder_sent": "Reminder sent",
    "escalated": "Escalated",
    "escalated_to_human": "Escalated",
    "blocked": "Blocked",
    "drift": "Mistake caught",
    "flagged": "Flagged",
}

_KANBAN_ORDER = ("outreach_check", "invoice", "payment_followup", "account_management", "analytics")


def _agent_label(name: str) -> str:
    return _AGENT_LABELS.get(name, name)


def _action_label(name: str) -> str:
    return _ACTION_LABELS.get(name, name)


def _status_label(name: str) -> str:
    return _STATUS_LABELS.get(name, name)


def _money(amount: float) -> str:
    return f"${amount:,.0f}"


def _freshness_label(entries: list[dict]) -> str:
    """'As of X minutes ago', sourced from the most recent audit-log entry —
    not wall-clock render time, which would always read 'just now' and defeat
    the point of a freshness check. `entries` must already be ordered newest
    first (AuditLogger.list_recent's DESCENDING order)."""
    if not entries:
        return "No activity yet"
    latest = entries[0].get("timestamp")
    if not latest:
        return "No activity yet"
    try:
        latest_dt = datetime.fromisoformat(latest)
    except ValueError:
        return "No activity yet"
    minutes = max(0, int((datetime.now(UTC) - latest_dt).total_seconds() // 60))
    if minutes == 0:
        return "As of less than a minute ago"
    if minutes == 1:
        return "As of 1 minute ago"
    return f"As of {minutes} minutes ago"


def _entry_subject(entry: dict) -> str:
    """The record an audit entry is actually about — pulled from `attributes`
    (the action's own result dict, or the agent's no_action log fields).
    Without this, kanban mini-logs of the same action/status repeated across
    several records are visually indistinguishable."""
    attrs = entry.get("attributes") or {}
    subject = attrs.get("invoice_id") or attrs.get("lead_id") or attrs.get("deal_id") or attrs.get("dimension")
    return f"{subject}: " if subject else ""


_PAGE_SHELL = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fortified Enterprise Fleet — live demo dashboard</title>
<link rel="icon" type="image/png" sizes="32x32" href="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAIAAAD8GO2jAAAB4UlEQVR4nO2SS0hUURzGf+dcZwYcH0mKMwiWgYyLfJEroQx6YGIgZAtxZYs2bQwR3IpG0EZcRJtEcJW2d1XSAxHaOZOGIOXSRMECxbG598r/3lt3Ru9C3QX343A45zvf/30gRIgQ/z9UMK0VSoEtZ9PZAUP91ds+me9JqwA+KKg6d2KnECqFbdNRR1eDPH74zvyapBY1uFNPxxVyJu/WZbnKfya1Fxi4xuQiuwf+E+gC79p5eNTG5H255izG7/K8E8umOcnITf6YonnRxehtUWrlJ3mxmAeNFEcDqvBzB6pL+PKE7gaPTFWSGaS/Rc5lMY9sTPD1KX3Ncja0F6YpwcfHJEt9V8crcMnLFRyafPohI40arG0ztsDQdSrj/M4SKxIys8mzBYZvUBXHtIgY3ngNLbUWorBFzgeRPkQMMchZYvM2zeoWE91CZnMSvqacN2lWfgoZKxLSsrmXku7v7DuJ2kFD1kp07Zd41cOt12zvSaUK6XWilKle0SxukKqSa8+MtHu6l4jm84aEbK9laJ73656fgADu6OsqeNjEyyX2Dj1GOXtZjP5WrlZL4LkM37bESzwqk2hJ8ivL7DLpzfz/cxYodSoySHZSBFrL6E4a67yC3Ca44mNkiBAhQnBWHAGiEqHzzg2zZwAAAABJRU5ErkJggg==">
<style>
  @font-face {{
    font-family: 'JetBrains Mono';
    font-style: normal;
    font-weight: 400 700;
    font-display: swap;
    src: url(data:font/woff2;base64,{font_b64}) format('woff2-variations');
    unicode-range: U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA, U+02DC, U+0304, U+0308,
      U+0329, U+2000-206F, U+20AC, U+2122, U+2191, U+2193, U+2212, U+2215, U+FEFF, U+FFFD;
  }}
  :root {{
    --ink: #0a0f0a; --surface: #0c150c; --line: #192619;
    --text: #e2e8e2; --muted: #7a8f7a;
    --accent: #00ff88; --info: #00ddff; --warn: #ffcc00; --danger: #ff4455;
    --font: 'JetBrains Mono', 'Courier New', monospace;
  }}
  * {{ box-sizing: border-box; }}
  html {{ color-scheme: dark; }}
  body {{
    font-family: var(--font); background: var(--ink); color: var(--text);
    margin: 0; padding: 2.5rem 1.5rem 4rem; line-height: 1.5;
  }}
  main {{ max-width: 1180px; margin: 0 auto; display: flex; flex-direction: column; gap: 2.5rem; }}

  .page-header {{ display: flex; flex-direction: column; gap: .35rem; }}
  .eyebrow {{
    margin: 0; font-size: .75rem; letter-spacing: .12em; text-transform: uppercase;
    color: var(--accent); font-weight: 700;
  }}
  h1 {{ margin: 0; font-size: 1.6rem; font-weight: 700; letter-spacing: -.01em; text-wrap: balance; }}

  .boundary {{
    background: var(--surface); border: 1px solid var(--line); border-left: 3px solid var(--info);
    border-radius: 3px; padding: 1rem 1.25rem; font-size: .875rem; color: var(--muted);
  }}
  .boundary code {{ color: var(--info); }}

  h2.view-title {{
    margin: 0; font-size: 1rem; text-transform: uppercase; letter-spacing: .1em;
    color: var(--accent); font-weight: 700; border-bottom: 1px solid var(--line); padding-bottom: .5rem;
  }}
  .view {{ display: flex; flex-direction: column; gap: 1.25rem; }}

  section > h3 {{
    margin: 0 0 1rem; font-size: .8rem; text-transform: uppercase; letter-spacing: .08em;
    color: var(--muted); font-weight: 600;
  }}
  section > h4 {{
    margin: 1.25rem 0 .6rem; font-size: .74rem; text-transform: uppercase; letter-spacing: .06em;
    color: var(--info); font-weight: 700;
  }}
  section > h4:first-of-type {{ margin-top: 0; }}

  .snapshot-meta {{
    display: flex; flex-wrap: wrap; align-items: center; gap: .5rem; font-size: .78rem; color: var(--muted);
  }}
  .snapshot-meta .freshness {{ color: var(--text); font-weight: 600; }}
  .meta-sep {{ color: var(--line); }}
  .meta-chip {{ padding: .1rem .55rem; border: 1px solid var(--line); border-radius: 3px; }}
  .meta-chip--armed {{ color: var(--warn); border-color: var(--warn); }}
  .meta-chip--caught {{ color: var(--danger); border-color: var(--danger); }}

  details.drill-down {{
    margin-top: .5rem; font-size: .72rem; color: var(--muted); border-top: 1px solid var(--line); padding-top: .5rem;
  }}
  details.drill-down summary {{
    cursor: pointer; color: var(--info); font-weight: 600; list-style: none;
  }}
  details.drill-down summary::-webkit-details-marker {{ display: none; }}
  details.drill-down summary::before {{ content: "▸ "; }}
  details.drill-down[open] summary::before {{ content: "▾ "; }}
  details.drill-down .drill-entry {{ padding: .35rem 0 0 .9rem; }}
  .narration {{
    margin-top: .3rem;
    font-style: italic;
    opacity: .78;
    line-height: 1.35;
  }}

  .help {{
    display: inline-flex; align-items: center; justify-content: center;
    width: 1rem; height: 1rem; margin-left: .3rem; vertical-align: middle;
    border: 1px solid var(--muted); border-radius: 50%; color: var(--muted);
    font-size: .62rem; font-weight: 700; cursor: help; position: relative;
  }}
  .help:hover, .help:focus-visible {{ color: var(--info); border-color: var(--info); }}
  .help:hover::after, .help:focus-visible::after {{
    content: attr(data-tip);
    position: absolute; bottom: 135%; left: 50%; transform: translateX(-50%);
    background: var(--surface); border: 1px solid var(--line); border-radius: 3px;
    padding: .5rem .65rem; font-size: .68rem; font-weight: 400; color: var(--text);
    white-space: normal; width: max-content; max-width: 220px; text-align: left;
    line-height: 1.4; box-shadow: 0 4px 14px rgba(0, 0, 0, .45); z-index: 20;
  }}

  .stats {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 1px;
    background: var(--line); border: 1px solid var(--line); border-radius: 3px; overflow: hidden;
  }}
  .stat-tile {{ background: var(--surface); padding: 1.1rem 1.25rem; display: flex; flex-direction: column; gap: .3rem; }}
  .stat-value {{ font-size: 1.5rem; font-weight: 700; font-variant-numeric: tabular-nums; }}
  .stat-label {{ font-size: .68rem; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); }}
  .stat-tile--armed .stat-value {{ color: var(--warn); }}
  .stat-tile--caught .stat-value {{ color: var(--danger); }}
  .stat-tile--bad .stat-value {{ color: var(--danger); }}
  .stat-tile--good .stat-value {{ color: var(--accent); }}

  .benchmark-table {{ width: 100%; border-collapse: collapse; font-size: .82rem; background: var(--surface);
    border: 1px solid var(--line); border-radius: 3px; overflow: hidden; }}
  .benchmark-table th, .benchmark-table td {{ text-align: left; padding: .55rem .75rem;
    border-bottom: 1px solid var(--line); font-variant-numeric: tabular-nums; }}
  .benchmark-table th {{ color: var(--muted); font-weight: 600; text-transform: uppercase; font-size: .68rem; }}
  .benchmark-table tr:last-child td {{ border-bottom: none; }}
  .metric-ok {{ color: var(--accent); font-weight: 700; }}
  .metric-bad {{ color: var(--danger); font-weight: 700; }}
  .source-note {{ margin: .5rem 0 0; font-size: .72rem; color: var(--muted); }}

  .flag-list {{ display: flex; flex-direction: column; gap: .6rem; }}
  .flag-item {{
    background: var(--surface); border: 1px solid var(--line); border-left: 3px solid var(--warn);
    border-radius: 3px; padding: .75rem 1rem; font-size: .85rem;
  }}
  .flag-item strong {{ color: var(--warn); }}
  .flag-empty {{ font-size: .85rem; color: var(--muted); }}

  .kanban {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 1rem; }}
  .flag-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 1rem; }}
  .flag-card {{
    background: var(--surface); border: 1px solid var(--line); border-radius: 3px;
    padding: 1rem 1.1rem; display: flex; flex-direction: column; gap: .4rem;
  }}
  .flag-card--attention {{ border-color: var(--danger); }}
  .flag-card--watch {{ border-color: var(--warn); }}
  .flag-card-header {{ display: flex; align-items: center; justify-content: space-between; gap: .5rem; }}
  .flag-card-header h4 {{ margin: 0; font-size: .82rem; }}
  .kanban-column {{
    background: var(--surface); border: 1px solid var(--line); border-radius: 3px;
    padding: 1rem; display: flex; flex-direction: column; gap: .75rem; min-width: 0;
  }}
  .kanban-column-header {{ display: flex; flex-direction: column; gap: .3rem; }}
  .kanban-column-header h4 {{ margin: 0; font-size: .9rem; font-weight: 700; color: var(--accent); }}
  .kanban-column-header .id-tag {{ margin: 0; font-size: .62rem; color: var(--muted); }}
  .kanban-column--disabled h4 {{ color: var(--muted); }}
  .badge {{
    align-self: flex-start; font-size: .62rem; text-transform: uppercase; letter-spacing: .04em;
    padding: .1rem .4rem; border-radius: 3px; font-weight: 700;
  }}
  .badge--enabled {{ color: var(--accent); background: #0d2e1c; }}
  .badge--disabled {{ color: var(--muted); background: #1a1f1a; }}
  .chip-row {{ display: flex; flex-wrap: wrap; gap: .35rem; }}
  .chip {{
    font-size: .65rem; padding: .15rem .5rem; border: 1px solid var(--line); border-radius: 3px;
    color: var(--info); background: rgba(0, 221, 255, .06);
  }}
  .criterion {{ margin: 0; font-size: .74rem; color: var(--muted); line-height: 1.5; }}
  .mini-log {{ display: flex; flex-direction: column; gap: .4rem; font-size: .72rem; }}
  .mini-log-entry {{ border-top: 1px solid var(--line); padding-top: .4rem; }}
  .mini-log-entry:first-child {{ border-top: none; padding-top: 0; }}
  .mini-log-entry--drift {{ color: var(--danger); }}
  .mini-log-empty {{ font-size: .72rem; color: var(--muted); }}
  .mini-log-subject {{ color: var(--info); font-weight: 600; }}

  .scenario-card {{
    background: var(--surface); border: 1px solid var(--line); border-left: 4px solid var(--warn);
    border-radius: 3px; padding: 1rem 1.25rem; display: flex; flex-direction: column; gap: .5rem;
    font-size: .85rem;
  }}
  .scenario-card--caught {{ border-left-color: var(--danger); }}
  .scenario-card p {{ margin: 0; }}
  .scenario-status {{ font-weight: 700; text-transform: uppercase; letter-spacing: .04em; font-size: .78rem; }}
  .scenario-status--armed {{ color: var(--warn); }}
  .scenario-status--caught {{ color: var(--danger); }}

  .table-scroll {{ overflow-x: auto; border: 1px solid var(--line); border-radius: 3px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: .8rem; background: var(--surface); }}
  th, td {{ text-align: left; padding: .55rem .75rem; border-bottom: 1px solid var(--line); vertical-align: top; }}
  tbody tr:last-child td {{ border-bottom: none; }}
  th {{ color: var(--muted); font-weight: 600; text-transform: uppercase; font-size: .68rem; letter-spacing: .06em; }}
  td.timestamp {{ font-variant-numeric: tabular-nums; color: var(--muted); white-space: nowrap; }}
  tr.drift-row {{ background: rgba(255, 68, 85, .08); }}

  .pill {{
    display: inline-block; padding: .15rem .5rem; border-radius: 3px;
    font-size: .7rem; font-weight: 700; text-transform: uppercase; letter-spacing: .03em;
  }}
  .pill--ok {{ color: var(--accent); background: #0d2e1c; }}
  .pill--action {{ color: var(--info); background: #0d2a2e; }}
  .pill--attention {{ color: var(--warn); background: #332a05; }}
  .pill--danger {{ color: var(--danger); background: #2a0e12; }}

  code {{ font-size: .8rem; color: var(--muted); }}
  a:focus-visible, button:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; }}

  @media (max-width: 900px) {{
    .kanban {{ grid-template-columns: repeat(2, 1fr); }}
  }}
  @media (max-width: 480px) {{
    body {{ padding: 1.5rem 1rem 3rem; }}
    .kanban {{ grid-template-columns: 1fr; }}
    h1 {{ font-size: 1.3rem; }}
  }}
</style>
</head>
<body>
<main>
  <header class="page-header">
    <p class="eyebrow">Fortified Enterprise Fleet</p>
    <h1>Live demo dashboard</h1>
  </header>

  <div class="boundary">
    This runs on simulated pipeline data (see <code>seed_demo_data.py</code>) — we're a small
    automation studio, not a company with a large deal pipeline yet. What's real: the agent
    reasoning, the Gemini calls, the Firestore writes, the Cloud Run execution, all live.
    What's seeded: the underlying business records.
  </div>

{body}
</main>
</body>
</html>"""


def _esc(value) -> str:
    return html.escape(str(value)) if value is not None else ""


def _narration_html(entry: dict) -> str:
    """Gemini's one-sentence explanation, rendered beside the deterministic
    detail rather than in place of it.

    Two things this must never skip. It reads via .get() because entries
    written before narration shipped have no such key, and both shapes stay
    live until the audit log turns over. And it routes through _esc() because
    this is the only field on the page whose content a model generates —
    unescaped model output on a judge-facing page is stored XSS."""
    text = entry.get("narration")
    if not text:
        return ""
    return f'<div class="narration">{_esc(text)}</div>'


def _help_icon(tip: str) -> str:
    """A small '?' badge next to a technical term — hover or focus (keyboard-
    accessible, WCAG 1.4.13) shows a brief plain-English definition via a
    CSS-only tooltip. No JS needed. aria-label carries the same text so a
    screen reader announces the actual definition, not just a bare '?' —
    the CSS ::after tooltip content isn't reliably exposed to the
    accessibility tree."""
    return f'<span class="help" tabindex="0" data-tip="{_esc(tip)}" aria-label="{_esc(tip)}">?</span>'


# ---------------------------------------------------------------- Executive Snapshot


def _render_financial_health(portfolio_invoices: list[dict], bills: list[dict]) -> str:
    """Cash position + industry benchmarks, tiered under one heading (2026-08-10
    simplification pass) — both are "how healthy is our cash cycle" questions,
    previously two separate top-level sections."""
    receivable = analytics.total_outstanding(portfolio_invoices)
    payable = sum(b.get("amount", 0) for b in bills if b.get("status") != "paid")
    net = receivable - payable
    net_tone = "bad" if net < 0 else "good"

    cash_stats = (
        '<div class="stats" aria-label="Cash position">'
        f'<div class="stat-tile"><span class="stat-value">{_money(receivable)}</span>'
        "<span class=\"stat-label\">Owed to Quadriga (receivable)</span></div>"
        f'<div class="stat-tile"><span class="stat-value">{_money(payable)}</span>'
        "<span class=\"stat-label\">Quadriga owes (payable)</span></div>"
        f'<div class="stat-tile stat-tile--{net_tone}"><span class="stat-value">{_money(net)}</span>'
        "<span class=\"stat-label\">Net cash position</span></div>"
        "</div>"
    )

    return (
        "<section><h3>Financial health</h3>"
        + cash_stats
        + "<h4>Are we collecting fast enough?</h4>"
        + _render_benchmark_table(portfolio_invoices)
        + "</section>"
    )


def _render_benchmark_table(portfolio_invoices: list[dict]) -> str:
    dso_value = analytics.dso(portfolio_invoices)
    turnover_value = analytics.ar_turnover(portfolio_invoices)
    collection = analytics.collection_rate(portfolio_invoices)
    outstanding = analytics.total_outstanding(portfolio_invoices)
    cost = analytics.opportunity_cost(outstanding)

    def _row(label, tip, value, benchmark, ok):
        cls = "metric-ok" if ok else "metric-bad"
        return (
            f"<tr><td>{label}{_help_icon(tip)}</td>"
            f'<td class="{cls}">{value}</td><td>{benchmark}</td></tr>'
        )

    rows = []
    if dso_value is not None:
        rows.append(
            _row(
                "Days Sales Outstanding",
                "Average days between issuing an invoice and getting paid. Lower is better.",
                f"{dso_value:.0f} days",
                f"{analytics.DSO_BENCHMARK_DAYS:.0f} days (mid-market)",
                dso_value <= analytics.DSO_BENCHMARK_DAYS,
            )
        )
    if turnover_value is not None:
        rows.append(
            _row(
                "AR turnover ratio",
                "How many times per year receivables get collected in full. Higher means faster collection.",
                f"{turnover_value:.1f}x",
                f"{analytics.AR_TURNOVER_BENCHMARK_LOW:.0f}-{analytics.AR_TURNOVER_BENCHMARK_HIGH:.0f}x (B2B)",
                turnover_value >= analytics.AR_TURNOVER_BENCHMARK_LOW,
            )
        )
    if collection is not None:
        rows.append(
            _row(
                "Collection rate",
                "Share of invoiced amounts actually collected, vs. still outstanding.",
                f"{collection * 100:.0f}%",
                f"under {analytics.BAD_DEBT_BENCHMARK_PCT:.1f}% uncollected is the target",
                collection >= (1 - analytics.BAD_DEBT_BENCHMARK_PCT / 100),
            )
        )

    body_rows = "".join(rows) if rows else '<tr><td colspan="3">Not enough data yet</td></tr>'
    table = (
        '<table class="benchmark-table"><thead><tr><th>Metric</th><th>Quadriga</th><th>Benchmark</th></tr></thead>'
        f"<tbody>{body_rows}</tbody></table>"
    )
    cost_note = (
        f'<p class="source-note">${outstanding:,.0f} uncollected — parking it risk-free '
        f"(T-bill, {analytics.RISK_FREE_ANNUAL_RATE * 100:.2f}%) would earn ~{_money(cost)}/yr.</p>"
    )
    sources = (
        '<details class="drill-down"><summary>Benchmark sources</summary>'
        '<div class="drill-entry">DSO — Eagle Rock CFO / Billtrust 2026 AR Benchmark Report. '
        "AR turnover, collection rate — ARDEM / Serrala 2026 AR KPI reports. "
        "3-month T-bill yield — Trading Economics, Aug 6 2026.</div></details>"
    )
    return f"{table}{cost_note}{sources}"


def _render_analytics_flags(flags: dict[str, dict], analytics_entries: list[dict] | None = None) -> str:
    """The Analytics Agent's 6-dimension read of the book of business — one
    card per dimension, in a fixed order so the layout doesn't jump around
    as severities change from one cycle to the next.

    Simplified 2026-08-10 (top-management-glance feedback): the card face
    now shows the headline only — a scannable claim, not a paragraph. The
    reasoning sentence (previously always shown) moves into a "Why" drill-down,
    alongside the 1 most recent prior audit-log entry for that dimension if
    one exists and actually differs (so re-opening the same reason twice
    doesn't just repeat itself)."""
    if not flags:
        return (
            "<section><h3>What the Analytics Agent is watching</h3>"
            '<p class="flag-empty">No analytics run yet — trigger the analytics agent to populate this.</p>'
            "</section>"
        )

    analytics_entries = analytics_entries or []
    cards = []
    for dimension in _DIMENSION_ORDER:
        flag = flags.get(dimension)
        if not flag:
            continue
        severity = flag.get("severity", "ok")
        tone = _SEVERITY_TONE.get(severity, "ok")
        card_class = "flag-card" if severity == "ok" else f"flag-card flag-card--{severity}"

        detail = flag.get("detail")
        why_items = [f'<div class="drill-entry">{_esc(detail)}</div>']
        backing = [e for e in analytics_entries if e.get("attributes", {}).get("dimension") == dimension]
        prior = next((e for e in backing if e.get("detail") != detail), None)
        if prior:
            why_items.append(
                f'<div class="drill-entry"><code>{_esc(prior.get("timestamp", ""))[:19]}</code> — {_esc(prior.get("detail"))}</div>'
            )
        drill_down = f'<details class="drill-down"><summary>Why</summary>{"".join(why_items)}</details>'

        cards.append(
            f'<article class="{card_class}">'
            '<div class="flag-card-header">'
            f"<h4>{_esc(_DIMENSION_LABELS.get(dimension, dimension))}</h4>"
            f'<span class="pill pill--{tone}">{_esc(_SEVERITY_LABELS.get(severity, severity))}</span>'
            "</div>"
            f"<p><strong>{_esc(flag.get('headline'))}</strong></p>"
            f"{drill_down}"
            "</article>"
        )
    return f'<section><h3>What the Analytics Agent is watching</h3><div class="flag-grid">{"".join(cards)}</div></section>'


def _render_needs_your_attention(manager_rows: list[dict], flagged_managers: list[dict], entries: list[dict]) -> str:
    """Merged 2026-08-10 (was two separate sections: 'Gaps for management to
    review' + 'Needs a human right now') — both are "where does a human need
    to look" questions, now one section with two subgroups. The always-shown
    full manager breakdown table is tucked behind a drill-down disclosure
    instead of rendered inline, to keep the section itself to a glance."""
    if not flagged_managers:
        gap_body = '<p class="flag-empty">No account manager currently below the collection-rate floor.</p>'
    else:
        items = []
        for row in flagged_managers:
            why = (
                f"{row['account_manager']} closed {row['deal_count']} deals worth "
                f"{_money(row['total_invoiced'])}, but only collected {_money(row['total_collected'])} "
                f"({row['collection_rate'] * 100:.0f}%) — worse collection than colleagues with fewer deals. "
                "Revenue on paper, not in the bank."
            )
            items.append(
                "<div class=\"flag-item\">"
                f"<strong>{_esc(row['account_manager'])}</strong> — {row['collection_rate'] * 100:.0f}% collected "
                f"on {row['deal_count']} deals"
                f'<details class="drill-down"><summary>Why</summary><div class="drill-entry">{_esc(why)}</div></details>'
                "</div>"
            )
        gap_body = f'<div class="flag-list">{"".join(items)}</div>'

    rows = "".join(
        f"<tr><td>{_esc(r['account_manager'])}</td><td>{r['deal_count']}</td>"
        f"<td>{_money(r['total_invoiced'])}</td><td>{_money(r['total_collected'])}</td>"
        f"<td>{(r['collection_rate'] * 100):.0f}%</td></tr>"
        for r in manager_rows
    )
    manager_table = (
        '<table class="benchmark-table"><thead><tr><th>Account manager</th><th>Deals</th>'
        "<th>Invoiced</th><th>Collected</th><th>Collection rate</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )
    gap_drill_down = f'<details class="drill-down"><summary>All account managers</summary>{manager_table}</details>'

    flagged_entries = [e for e in entries if e.get("status") in _NEEDS_ATTENTION_STATUSES]
    if not flagged_entries:
        attention_body = '<p class="flag-empty">Nothing pending.</p>'
    else:
        attention_body = '<div class="flag-list">' + "".join(
            f'<div class="flag-item"><strong>{_esc(_agent_label(e.get("agent_name")))}</strong> — '
            f"{_esc(_entry_subject(e))}{_esc(_status_label(e.get('status')))}"
            f'<details class="drill-down"><summary>Why</summary><div class="drill-entry">{_esc(e.get("detail"))}{_narration_html(e)}</div></details>'
            "</div>"
            for e in flagged_entries[:8]
        ) + "</div>"

    return (
        "<section><h3>Where do we need to act?</h3>"
        "<h4>Account managers falling behind</h4>" + gap_body + gap_drill_down
        + f"<h4>Escalations needing a human ({len(flagged_entries)})</h4>" + attention_body
        + "</section>"
    )


# ---------------------------------------------------------------- Agentic View (kanban)


_KANBAN_VISIBLE_LOG_CAP = 3
# 2026-08-12 page-weight pass. Measured live: 135,939 B, of which 74 KB was the
# *same* 200-entry audit slice rendered twice — once as kanban mini-log entries
# (every overflow entry sat inside the collapsed <details>, hidden but fully in
# the DOM) and again as full activity-log rows below it. Entity rows were never
# the driver: the whole page carried 7 <tr> outside the activity log, so the
# demo_stream prune cap is not the lever here. The overflow drill-down now stops
# at 13 entries per agent (the complete log sits directly below, so no
# information is lost) and the activity log renders 75 rows instead of 200.
_KANBAN_OVERFLOW_LOG_CAP = 13
_ACTIVITY_LOG_RENDER_CAP = 75


def _mini_log_entry_html(e: dict) -> str:
    return (
        f'<div class="mini-log-entry{" mini-log-entry--drift" if e.get("drift_detected") else ""}">'
        f'<span class="mini-log-subject">{_esc(_entry_subject(e))}</span>'
        f"{_esc(_action_label(e.get('action')))} — {_esc(_status_label(e.get('status')))}"
        "</div>"
    )


def _render_kanban(registry: AgentRegistry, entries_by_agent: dict[str, list[dict]]) -> str:
    """Simplified 2026-08-10 (top-management-glance feedback): the declared
    success criterion (a full sentence, always shown before) is now a
    'Declared scope' drill-down — still on the page and satisfies the Fleet
    track's Agent Identity visibility requirement, just not forced into every
    glance. The mini-log shows at most 3 entries inline; the rest (often
    several identical 'No action needed — OK' lines) collapse behind a
    '+N more' drill-down instead of repeating on the card face."""
    declarations = {d["agent_name"]: d for d in registry.list_all()}
    columns = []
    for agent_name in _KANBAN_ORDER:
        decl = declarations.get(agent_name)
        if not decl:
            continue
        enabled = decl.get("enabled", True)
        column_class = "kanban-column" if enabled else "kanban-column kanban-column--disabled"
        badge = (
            '<span class="badge badge--enabled">Active</span>'
            if enabled
            else '<span class="badge badge--disabled">Disabled</span>'
        )
        chips = "".join(
            f'<span class="chip">{_esc(_action_label(a))}</span>' for a in sorted(decl["allowed_actions"])
        )
        criterion = (
            '<details class="drill-down"><summary>Declared scope</summary>'
            f'<div class="drill-entry">{_esc(decl["success_criterion"])}</div></details>'
        )

        agent_entries = entries_by_agent.get(agent_name, [])
        visible = agent_entries[:_KANBAN_VISIBLE_LOG_CAP]
        overflow = agent_entries[_KANBAN_VISIBLE_LOG_CAP:_KANBAN_OVERFLOW_LOG_CAP]
        if visible:
            log_items = "".join(_mini_log_entry_html(e) for e in visible)
            if overflow:
                overflow_items = "".join(_mini_log_entry_html(e) for e in overflow)
                log_items += (
                    f'<details class="drill-down"><summary>+{len(overflow)} more</summary>{overflow_items}</details>'
                )
        else:
            log_items = '<p class="mini-log-empty">No activity yet.</p>'

        columns.append(
            f'<article class="{column_class}">'
            '<div class="kanban-column-header">'
            f"{badge}<h4>{_esc(_agent_label(agent_name))}</h4>"
            f'<p class="id-tag"><code>{_esc(agent_name)}</code></p>'
            "</div>"
            f'<div class="chip-row">{chips}</div>'
            f"{criterion}"
            f'<div class="mini-log">{log_items}</div>'
            "</article>"
        )
    return f'<section><h3>What each assistant is doing right now</h3><div class="kanban">{"".join(columns)}</div></section>'


def _render_drift_status(db, entries: list[dict]) -> tuple[str, bool, bool]:
    scenario_snap = db.collection(COLLECTION_DRIFT_SCENARIO).document("active_scenario").get()
    if not scenario_snap.exists:
        return "<section><h3>Built-in safety check</h3><p>No test set up.</p></section>", False, False
    scenario = scenario_snap.to_dict()

    caught = any(
        entry.get("drift_detected") and entry.get("attributes", {}).get("invoice_id") == scenario["invoice_id"]
        for entry in entries
    )
    tone = "caught" if caught else "armed"
    state_label = (
        "Caught! Flagged automatically for a team member to review."
        if caught
        else "Ready — hasn't been tested yet this run."
    )
    html_out = (
        "<section><h3>Built-in safety check (demo)</h3>"
        f'<div class="scenario-card scenario-card--{tone}">'
        f"<p>{_esc(scenario['description'])}</p>"
        f"<p>Invoice being tested: <code>{_esc(scenario['invoice_id'])}</code> — action attempted: "
        f"{_esc(_action_label(scenario['forced_action']))}</p>"
        f'<p class="scenario-status scenario-status--{tone}">{state_label}</p>'
        "</div></section>"
    )
    return html_out, caught, True


# ---------------------------------------------------------------- Activity Log (detail)


def _render_activity_log(entries: list[dict]) -> str:
    rows = []
    for entry in entries:
        row_class = "drift-row" if entry.get("drift_detected") else ""
        status = entry.get("status", "")
        tone = _PILL_TONE.get(status, "action")
        rows.append(
            f'<tr class="{row_class}">'
            f'<td class="timestamp"><code>{_esc(entry.get("timestamp", ""))[:19]}</code></td>'
            f"<td>{_esc(_agent_label(entry.get('agent_name')))}</td>"
            f"<td>{_esc(_action_label(entry.get('action')))}</td>"
            f'<td><span class="pill pill--{tone}">{_esc(_status_label(status))}</span></td>'
            f"<td>{_esc(entry.get('detail'))}{_narration_html(entry)}</td>"
            f"</tr>"
        )
    if not rows:
        rows.append('<tr><td colspan="5">No activity yet — trigger a run to see it here.</td></tr>')
    return (
        "<section><h3>Full activity log</h3>"
        '<div class="table-scroll"><table><thead><tr>'
        "<th>Time (UTC)</th><th>Assistant</th><th>Action</th><th>Result</th><th>Details</th>"
        f"</tr></thead><tbody>{''.join(rows)}</tbody></table></div></section>"
    )


# ---------------------------------------------------------------- Top-level assembly


def render(db) -> str:
    registry = AgentRegistry(db)
    audit_logger = AuditLogger(db)

    entries = audit_logger.list_recent(limit=_ACTIVITY_LOG_RENDER_CAP)
    # The rendered slice is capped, so report the real lifetime total separately
    # rather than letting the cap masquerade as the fleet's whole history.
    total_actions = audit_logger.count_all()
    if total_actions is None:
        total_actions = len(entries)
    entries_by_agent: dict[str, list[dict]] = {}
    for entry in entries:
        entries_by_agent.setdefault(entry.get("agent_name"), []).append(entry)

    portfolio_invoices = [doc.to_dict() for doc in db.collection(COLLECTION_PORTFOLIO_INVOICES).stream()]
    bills = [doc.to_dict() for doc in db.collection(COLLECTION_BILLS).stream()]
    manager_rows = analytics.manager_breakdown(portfolio_invoices)
    flagged_managers = analytics.underperforming_managers(manager_rows)
    analytics_flags = {doc.id: doc.to_dict() for doc in db.collection(COLLECTION_ANALYTICS_FLAGS).stream()}

    scenario_html, caught, scenario_exists = _render_drift_status(db, entries)
    agent_count = len(registry.list_all())
    scenario_value = "N/A" if not scenario_exists else ("CAUGHT" if caught else "READY")
    scenario_tone = "" if not scenario_exists else ("caught" if caught else "armed")

    # 2026-08-10 simplification pass: the 3-tile overview block collapsed into
    # a slim meta strip (freshness + compact chips), not a full stats section —
    # frees a whole row of vertical space for the sections that actually
    # change per-cycle.
    snapshot_meta = (
        '<div class="snapshot-meta">'
        f'<span class="freshness">{_esc(_freshness_label(entries))}</span>'
        '<span class="meta-sep">·</span>'
        f'<span class="meta-chip">{agent_count} automated assistants</span>'
        f'<span class="meta-chip">{total_actions:,} actions logged</span>'
        f'<span class="meta-chip meta-chip--{scenario_tone}">Safety check: {scenario_value}</span>'
        "</div>"
    )

    executive_snapshot = (
        '<div class="view">'
        '<h2 class="view-title">Executive Snapshot</h2>'
        + snapshot_meta
        + _render_analytics_flags(analytics_flags, entries_by_agent.get("analytics"))
        + _render_financial_health(portfolio_invoices, bills)
        + _render_needs_your_attention(manager_rows, flagged_managers, entries)
        + "</div>"
    )

    agentic_view = (
        '<div class="view">'
        '<h2 class="view-title">Agentic View</h2>'
        + _render_kanban(registry, entries_by_agent)
        + scenario_html
        + "</div>"
    )

    activity_view = '<div class="view">' + _render_activity_log(entries) + "</div>"

    body = executive_snapshot + agentic_view + activity_view
    return _PAGE_SHELL.format(body=body, font_b64=JETBRAINS_MONO_WOFF2_BASE64)
