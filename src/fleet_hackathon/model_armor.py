"""Model Armor — the Fleet track's Security & Governance component covering
ingested-text risk. Applied to any text an agent reads from outside its own
declared scope (e.g. Outreach-Check's simulated lead replies) before that text
is included in a Gemini call.

Deliberately basic (regex-based), matching what the build plan calls for — the
managed GCP "Model Armor" product is a separate paid API and out of scope for a
demo-cost-capped build. Real, testable filtering; not a stub.
"""

import re
from dataclasses import dataclass, field

_PII_PATTERNS = {
    "email": re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
    "phone": re.compile(r"\b(?:\+?\d{1,2}[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b"),
}

_INJECTION_PATTERNS = [
    re.compile(r"ignore (all|any|previous|prior) instructions", re.IGNORECASE),
    re.compile(r"disregard (the )?(above|previous)", re.IGNORECASE),
    re.compile(r"^\s*(system|assistant)\s*:", re.IGNORECASE | re.MULTILINE),
    re.compile(r"\byou are now\b", re.IGNORECASE),
    re.compile(r"new instructions?\s*:", re.IGNORECASE),
]


@dataclass
class ArmorResult:
    clean_text: str
    pii_redacted: list[str] = field(default_factory=list)
    injection_flags: list[str] = field(default_factory=list)

    @property
    def flagged(self) -> bool:
        return bool(self.pii_redacted or self.injection_flags)


def scan(text: str) -> ArmorResult:
    clean = text
    pii_redacted: list[str] = []
    for label, pattern in _PII_PATTERNS.items():
        if pattern.search(clean):
            pii_redacted.append(label)
            clean = pattern.sub(f"[REDACTED_{label.upper()}]", clean)

    injection_flags: list[str] = []
    for pattern in _INJECTION_PATTERNS:
        if pattern.search(clean):
            injection_flags.append(pattern.pattern)
            clean = pattern.sub("[BLOCKED_INSTRUCTION]", clean)

    return ArmorResult(clean_text=clean, pii_redacted=pii_redacted, injection_flags=injection_flags)
