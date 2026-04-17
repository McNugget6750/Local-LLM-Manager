"""
request_classifier.py — Three-tier complexity detection for routing user messages.

Returns one of:
  "direct"      — answer immediately, no orchestration
  "orchestrate" — run orchestrator pre-turn before Eli
  "ambiguous"   — heuristics inconclusive; Tier 2 (Eli decides via [ORCHESTRATE] signal)

Tier 3 (explicit user prefix) is checked first.
Tier 1 (pattern heuristics) is checked second.
Tier 2 (Eli's own routing signal) is handled in the adapter, not here.
"""

import re
from typing import Literal

# ── Tier 3: explicit user overrides ──────────────────────────────────────────

_FORCE_ORCHESTRATE = ("!plan ", "!o ", "!orchestrate ")
_FORCE_DIRECT      = ("!quick ", "!q ")

# ── Tier 1: auto-continuation messages (never reclassify these) ───────────────
# Messages that the harness itself generates — pass straight to Eli.
_AUTO_PREFIXES = (
    "[system:",
    "[System:",
    "[background:",
    "[Background:",
)

# ── Tier 1: conversational / lookup openers → direct ─────────────────────────
_DIRECT_OPENER = re.compile(
    r'^(what\s+(is|are|was|were|does|do|did|happened|changed|'
    r'version|s\b)|'
    r'how\s+(does|do|did|can|would|should|to\b)|'
    r'why\s+(does|do|did|is|are|was|would)|'
    r'when\s+(does|do|did|is|was)|'
    r'where\s+(does|do|did|is|are)|'
    r'who\s+(is|are|was|were)|'
    r'which\s+|'
    r'can\s+you\s+|'
    r'could\s+you\s+|'
    r'do\s+you\s+|'
    r'did\s+you\s+|'
    r'should\s+i\s+|'
    r'would\s+you\s+|'
    r'is\s+there\s+|'
    r'are\s+there\s+|'
    r'does\s+|'
    r'explain\s+|'
    r'tell\s+me\s+|'
    r'show\s+me\s+|'
    r'describe\s+|'
    r'define\s+|'
    r'summarize\s+)',
    re.IGNORECASE,
)

# ── Tier 1: implementation verbs (code changes) → orchestrate ────────────────
_IMPL_VERB = re.compile(
    r'\b(implement|build|create|add|write|make|develop|'
    r'refactor|rewrite|redesign|restructure|rework|clean\s*up|'
    r'fix|debug|diagnose|investigate|trace|root.?cause|'
    r'migrate|port|convert|upgrade|replace|swap|'
    r'optimize|improve|speed\s*up|reduce|eliminate|'
    r'integrate|connect|wire\s*up|set\s*up|configure)\b',
    re.IGNORECASE,
)

# ── Tier 1: review/analysis verbs (read/assess only) → ambiguous ─────────────
_REVIEW_VERB = re.compile(
    r'\b(analyze|analyse|audit|review|inspect|examine|profile|benchmark|'
    r'explore|survey|map\s+out|understand\s+how|walk\s+(me\s+)?through|'
    r'test|verify|validate|check|ensure)\b',
    re.IGNORECASE,
)

# ── Tier 1: codebase / project scope signals ─────────────────────────────────
_CODEBASE_REF = re.compile(
    r'(\.(py|ts|tsx|js|jsx|cpp|c|h|hpp|cs|go|rs|rb|java|kt|'
    r'yaml|yml|json|toml|ini|cfg|md|sql|sh|bat)\b'
    r'|/[a-zA-Z0-9_\-]+[./]'          # path-like fragment
    r'|\b(this\s+)?(project|codebase|repo|repository|'
    r'module|class|function|method|file|directory|folder|'
    r'the\s+code|source|src|lib|backend|frontend|'
    r'database|schema|api|endpoint|middleware|'
    r'test(s|ing)?|pipeline|workflow|ci|cd)\b)',
    re.IGNORECASE,
)


def classify(text: str) -> Literal["direct", "orchestrate", "ambiguous"]:
    """
    Classify a user message for routing.

    Returns:
        "direct"      — short-circuit to Eli, no orchestration
        "orchestrate" — run orchestrator pre-turn
        "ambiguous"   — let Eli decide via [ORCHESTRATE] signal (Tier 2)
    """
    stripped = text.strip()
    lower    = stripped.lower()

    # ── Tier 3: explicit overrides ───────────────────────────────────────────
    for prefix in _FORCE_ORCHESTRATE:
        if lower.startswith(prefix):
            return "orchestrate"
    for prefix in _FORCE_DIRECT:
        if lower.startswith(prefix):
            return "direct"

    # ── Auto-continuation / system messages — always direct ─────────────────
    for prefix in _AUTO_PREFIXES:
        if stripped.startswith(prefix):
            return "direct"

    # ── Very short messages with no task verb are almost always conversational ─
    words = stripped.split()
    if len(words) <= 10 and "\n" not in stripped and not _IMPL_VERB.search(stripped) and not _REVIEW_VERB.search(stripped):
        return "direct"

    # ── Tier 1: conversational opener ────────────────────────────────────────
    if _DIRECT_OPENER.match(stripped):
        # Still could be a task if it references the codebase with an impl verb
        # e.g. "explain how to refactor the auth module" — that's ambiguous.
        # "explain what git rebase does" — direct.
        if _IMPL_VERB.search(stripped) and _CODEBASE_REF.search(stripped):
            return "ambiguous"
        return "direct"

    # ── Tier 1: implementation verb + codebase ref → orchestrate ─────────────
    if _IMPL_VERB.search(stripped) and _CODEBASE_REF.search(stripped):
        return "orchestrate"

    # ── Review/analysis verb + codebase ref → ambiguous (Eli decides) ────────
    if _REVIEW_VERB.search(stripped) and _CODEBASE_REF.search(stripped):
        return "ambiguous"

    # ── Any task verb alone (no codebase ref) → ambiguous ────────────────────
    if _IMPL_VERB.search(stripped) or _REVIEW_VERB.search(stripped):
        return "ambiguous"

    return "direct"
