"""Runtime configuration, sourced from environment. No hardcoded project IDs outside this module."""

import os

GCP_PROJECT = os.environ["GOOGLE_CLOUD_PROJECT"]
GCP_LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "us")

# Firestore collections — single source of truth for names used across modules.
COLLECTION_REGISTRY = "fleet_registry"
COLLECTION_AUDIT_LOG = "fleet_audit_log"
COLLECTION_LEADS = "demo_leads"
COLLECTION_DEALS = "demo_deals"
COLLECTION_INVOICES = "demo_invoices"
COLLECTION_DRIFT_SCENARIO = "demo_drift_scenario"
COLLECTION_BILLS = "demo_bills"  # accounts payable — context data only, no agent manages this
COLLECTION_CASH_EVENTS = "demo_cash_events"  # unified in/out ledger, mirrors the real crm_demo.db payments shape
# The larger realistic book of business (real client/amount samples from tools/demo-crm-mcp/crm_demo.db,
# dates remapped relative to now) — analytics/Executive Snapshot only. Deliberately NOT in
# COLLECTION_INVOICES: the agents only ever act on the small curated "hero" set there, so a live
# demo run stays a handful of clean, narratable actions instead of 20+ reminders firing at once.
COLLECTION_PORTFOLIO_INVOICES = "demo_portfolio_invoices"

# Analytics Agent's data sources/output — all portfolio-scale (real crm_demo.db-derived
# samples), analytics-only, never touched by any of the other 4 agents.
COLLECTION_ANALYTICS_FLAGS = "demo_analytics_flags"  # one doc per dimension, upserted each cycle
COLLECTION_PORTFOLIO_ENGAGEMENTS = "demo_portfolio_engagements"  # real client/status mix, operational-bottleneck signal
COLLECTION_GROWTH_SNAPSHOT = "demo_growth_snapshot"  # static real Won-value snapshot, trailing vs prior quarter
COLLECTION_SALES_PIPELINE = "demo_sales_pipeline"  # static real contract-stage distribution

# Bookkeeping the demo stream owns. Currently one document, holding the running
# tally of audit entries the pruner has removed, so the lifetime action count
# survives pruning (see telemetry.AuditLogger.count_all).
COLLECTION_STATS = "fleet_stats"
AUDIT_COUNTER_DOC = "audit_counter"

# Gemini narration cache. Keyed by a hash of the exact prompt string, which
# carries only class-level facts (agent/action/status/drift/success_criterion),
# never record specifics — see narration._summarize for why that is a
# correctness requirement rather than a style choice. The key space is therefore
# bounded by the number of decision classes (~5 agents x 6 actions x 4 statuses),
# so this collection self-bounds well under 100 documents and needs no pruner.
COLLECTION_NARRATION_CACHE = "fleet_narration_cache"

# Payment-Followup graduated escalation threshold (days overdue, or reminder count — whichever first).
OVERDUE_ESCALATION_DAYS = 30
OVERDUE_ESCALATION_REMINDER_COUNT = 2

# Outreach-Check SLA window default (hours), used when a lead record omits its own.
DEFAULT_SLA_WINDOW_HOURS = 48
