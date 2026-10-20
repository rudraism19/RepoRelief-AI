"""Triage engine for RepoRelief AI.

Provides transparent, deterministic heuristic classification, priority estimation,
missing reproduction detection, input sanitization, and actionable investigation checklists.
100% rule-based with zero external model or LLM dependencies.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from github_client import GitHubIssue

logger = logging.getLogger(__name__)


class TriageCategory(str, Enum):
    BUG = "bug"
    FEATURE = "feature_request"
    DOCUMENTATION = "documentation"
    QUESTION = "question"
    OTHER = "other"


class TriagePriority(str, Enum):
    P0 = "P0"  # Blocker / Critical
    P1 = "P1"  # High
    P2 = "P2"  # Medium
    P3 = "P3"  # Low


class TriageConfidence(str, Enum):
    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"


@dataclass
class ReproductionStatus:
    """Detailed evaluation of reproduction details provided in an issue."""

    has_repro_steps: bool
    has_expected_actual: bool
    has_environment_info: bool
    has_logs_or_code: bool
    missing_items: list[str] = field(default_factory=list)
    completeness: str = "Missing"  # "Complete", "Partial", "Missing"


@dataclass
class TriageResult:
    """Complete triage assessment for a GitHub issue."""

    issue_number: int
    title: str
    category: TriageCategory
    priority: TriagePriority
    priority_reasoning: str
    confidence: TriageConfidence
    confidence_explanation: str
    method: str  # Always "Deterministic Rule-Based Engine"
    summary: str
    reproduction: ReproductionStatus
    investigation_checklist: list[str] = field(default_factory=list)
    matched_signals: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert triage result to serializable dictionary for JSON and CSV exports."""
        return {
            "issue_number": self.issue_number,
            "title": self.title,
            "category": self.category.value,
            "priority": self.priority.value,
            "priority_reasoning": self.priority_reasoning,
            "confidence": self.confidence.value,
            "confidence_explanation": self.confidence_explanation,
            "method": self.method,
            "summary": self.summary,
            "reproduction_completeness": self.reproduction.completeness,
            "missing_reproduction_items": self.reproduction.missing_items,
            "investigation_checklist": self.investigation_checklist,
            "matched_signals": self.matched_signals,
        }


# --- Input Sanitization & Safety Defenses ---

_PROMPT_INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(all\s+)?(previous|prior)\s+instructions?", re.IGNORECASE),
    re.compile(r"disregard\s+(all\s+)?(previous|prior)\s+instructions?", re.IGNORECASE),
    re.compile(r"system\s*prompt", re.IGNORECASE),
    re.compile(r"\[INST\].*?\[/INST\]", re.IGNORECASE | re.DOTALL),
    re.compile(r"<\|im_start\|>.*?<\|im_end\|>", re.IGNORECASE | re.DOTALL),
    re.compile(r"###\s*System:", re.IGNORECASE),
]


def sanitize_untrusted_input(text: str, max_chars: int = 4000) -> str:
    """Sanitize untrusted user-supplied issue content.

    Defends against prompt injection attempts, strips dangerous control tokens,
    and truncates to prevent memory exhaustion on resource-constrained hardware.
    """
    if not text:
        return ""

    sanitized = text
    for pattern in _PROMPT_INJECTION_PATTERNS:
        sanitized = pattern.sub("[filtered-instruction-marker]", sanitized)

    # Strip dangerous non-printable control characters, keeping standard newlines and tabs
    sanitized = "".join(ch for ch in sanitized if ch in ("\n", "\r", "\t") or ch.isprintable())

    # Truncate if exceeds bounds
    if len(sanitized) > max_chars:
        sanitized = sanitized[:max_chars] + f"\n... [Truncated for safety: length exceeded {max_chars} characters]"

    return sanitized


# --- Deterministic Rule-Based Heuristics ---

_BUG_KEYWORDS = {
    "crash", "traceback", "exception", "segfault", "segmentation fault", "fail",
    "fails", "failure", "broken", "error", "bug", "hang", "freeze", "infinite loop",
    "unexpected", "panic", "not working", "regression", "nullpointer", "stacktrace",
    "corrupt", "fault", "500 internal", "timeout", "reproduce",
}

_FEATURE_KEYWORDS = {
    "add", "support", "feature", "request", "proposal", "allow", "implement",
    "suggestion", "would like", "enhancement", "rfc", "extend", "introduce",
    "capability", "provide an option", "ability to",
}

_DOCS_KEYWORDS = {
    "doc", "docs", "documentation", "readme", "typo", "guide", "tutorial",
    "example", "quickstart", "api reference", "spelling", "grammar", "docs link",
    "broken link", "misleading",
}

_QUESTION_KEYWORDS = {
    "how to", "how do i", "how can i", "why does", "question", "is there a way",
    "can we", "best practice", "where is", "help needed", "trouble understanding",
}

_P0_SIGNALS = {
    "critical", "blocker", "cve", "vulnerability", "data loss", "data corruption",
    "rce", "remote code execution", "security hole", "segfault", "segmentation fault",
    "kernel panic", "system crash", "deadlock", "silent corruption",
}

_P1_SIGNALS = {
    "regression", "major", "broken workflow", "fails to build", "unhandled exception",
    "memory leak", "performance drop", "high cpu", "blocking release", "cannot log in",
    "crash on startup",
}

_P3_SIGNALS = {
    "minor", "typo", "cosmetic", "trivial", "spelling", "doc tweak", "nice to have",
    "nit", "cleanup", "polish", "low priority",
}


def evaluate_reproduction_status(title: str, body: str) -> ReproductionStatus:
    """Analyze issue text for the 4 core reproduction pillars:
    1. Steps to reproduce
    2. Expected vs. actual behavior
    3. Environment & version specifications
    4. Code sample, traceback, or console error logs
    """
    text_lower = f"{title}\n{body}".lower()

    # 1. Repro steps
    repro_indicators = [
        "steps to reproduce", "repro steps", "how to reproduce", "to reproduce",
        "steps:", "steps to replicate", "reproduction:",
    ]
    has_repro_steps = any(indicator in text_lower for indicator in repro_indicators)
    # Check for ordered steps like "1. ... 2. ..."
    if not has_repro_steps and re.search(r"\b1\.\s+.*\b2\.\s+", text_lower, re.DOTALL):
        has_repro_steps = True

    # 2. Expected vs actual
    expected_indicators = ["expected", "should be", "expected behavior", "supposed to"]
    actual_indicators = ["actual", "instead of", "actual behavior", "what happened", "got"]
    has_expected = any(i in text_lower for i in expected_indicators)
    has_actual = any(i in text_lower for i in actual_indicators)
    has_expected_actual = has_expected or has_actual

    # 3. Environment info
    env_indicators = [
        "python", "version", "os", "operating system", "windows", "linux", "ubuntu",
        "macos", "darwin", "browser", "chrome", "firefox", "edge", "node", "v1.",
        "v2.", "v3.", "arch", "x86", "arm64", "kernel",
    ]
    has_env = any(i in text_lower for i in env_indicators)

    # 4. Logs or code blocks
    has_code_blocks = "```" in body or "    " in body
    has_log_keywords = any(kw in text_lower for kw in ["traceback", "stack trace", "error log", "exception:"])
    has_logs_or_code = has_code_blocks or has_log_keywords

    missing: list[str] = []
    if not has_repro_steps:
        missing.append("Steps to reproduce (exact sequence of actions)")
    if not has_expected_actual:
        missing.append("Expected vs. actual behavior")
    if not has_env:
        missing.append("Environment details (OS, Python/runtime version, dependency versions)")
    if not has_logs_or_code:
        missing.append("Minimal code snippet, traceback, or console error logs")

    score = sum([has_repro_steps, has_expected_actual, has_env, has_logs_or_code])
    if score >= 3:
        completeness = "Complete"
    elif score in (1, 2):
        completeness = "Partial"
    else:
        completeness = "Missing"

    return ReproductionStatus(
        has_repro_steps=has_repro_steps,
        has_expected_actual=has_expected_actual,
        has_environment_info=has_env,
        has_logs_or_code=has_logs_or_code,
        missing_items=missing,
        completeness=completeness,
    )


def classify_issue_heuristically(
    title: str, body: str, labels: list[str]
) -> tuple[TriageCategory, TriageConfidence, list[str], str]:
    """Classify issue category using deterministic keyword and label matching."""
    matched_signals: list[str] = []
    labels_lower = [lbl.lower() for lbl in labels]

    # Label-based classification (strongest signal)
    for lbl in labels_lower:
        if any(b in lbl for b in ["bug", "defect", "crash", "error", "fault"]):
            matched_signals.append(f"Label: {lbl}")
            return (
                TriageCategory.BUG,
                TriageConfidence.HIGH,
                matched_signals,
                "Explicitly labeled with bug/defect marker in repository.",
            )
        if any(f in lbl for f in ["enhancement", "feature", "proposal", "rfc", "idea"]):
            matched_signals.append(f"Label: {lbl}")
            return (
                TriageCategory.FEATURE,
                TriageConfidence.HIGH,
                matched_signals,
                "Explicitly labeled with feature/enhancement marker in repository.",
            )
        if any(d in lbl for d in ["doc", "docs", "documentation", "typo"]):
            matched_signals.append(f"Label: {lbl}")
            return (
                TriageCategory.DOCUMENTATION,
                TriageConfidence.HIGH,
                matched_signals,
                "Explicitly labeled with documentation marker in repository.",
            )
        if any(q in lbl for q in ["question", "help wanted", "support", "discussion"]):
            matched_signals.append(f"Label: {lbl}")
            return (
                TriageCategory.QUESTION,
                TriageConfidence.HIGH,
                matched_signals,
                "Explicitly labeled with question/discussion marker in repository.",
            )

    # Text-based keyword scoring (Title is weighted 3x)
    title_lower = title.lower()
    body_lower = body.lower()

    def score_keywords(keywords: set[str]) -> tuple[int, list[str]]:
        hits = []
        score = 0
        for kw in keywords:
            if re.search(r"\b" + re.escape(kw) + r"\b", title_lower):
                score += 3
                hits.append(f"Title: '{kw}'")
            elif re.search(r"\b" + re.escape(kw) + r"\b", body_lower):
                score += 1
                hits.append(f"Body: '{kw}'")
        return score, hits

    bug_score, bug_hits = score_keywords(_BUG_KEYWORDS)
    feat_score, feat_hits = score_keywords(_FEATURE_KEYWORDS)
    doc_score, doc_hits = score_keywords(_DOCS_KEYWORDS)
    quest_score, quest_hits = score_keywords(_QUESTION_KEYWORDS)

    scores = [
        (bug_score, TriageCategory.BUG, bug_hits),
        (feat_score, TriageCategory.FEATURE, feat_hits),
        (doc_score, TriageCategory.DOCUMENTATION, doc_hits),
        (quest_score, TriageCategory.QUESTION, quest_hits),
    ]

    scores.sort(key=lambda x: x[0], reverse=True)
    top_score, top_category, top_hits = scores[0]

    if top_score >= 5:
        confidence = TriageConfidence.HIGH
        explanation = f"Strong keyword signals matched ({len(top_hits)} signals) across title and body."
    elif top_score >= 2:
        confidence = TriageConfidence.MEDIUM
        explanation = f"Moderate keyword signals matched in text ({len(top_hits)} signals)."
    else:
        top_category = TriageCategory.OTHER
        confidence = TriageConfidence.LOW
        explanation = "Insufficient keyword or label matches; categorized as 'other' by default."

    return top_category, confidence, top_hits[:5], explanation


def estimate_priority_heuristically(
    category: TriageCategory, title: str, body: str, labels: list[str]
) -> tuple[TriagePriority, str]:
    """Estimate priority using documented rule-based heuristics."""
    text_combined = f"{title}\n{body}\n{' '.join(labels)}".lower()

    # Check P0 signals
    p0_matches = [s for s in _P0_SIGNALS if re.search(r"\b" + re.escape(s) + r"\b", text_combined)]
    if p0_matches:
        return (
            TriagePriority.P0,
            f"Contains critical blocker signals: {', '.join(p0_matches[:3])}. Immediate triage required.",
        )

    # Check P1 signals
    p1_matches = [s for s in _P1_SIGNALS if re.search(r"\b" + re.escape(s) + r"\b", text_combined)]
    if p1_matches:
        return (
            TriagePriority.P1,
            f"Indicates major functional regression or core failure: {', '.join(p1_matches[:3])}.",
        )

    # Documentation and minor typos naturally map to P3
    if category == TriageCategory.DOCUMENTATION:
        return (
            TriagePriority.P3,
            "Documentation clarifications and typos are standard low-risk maintenance tasks.",
        )

    # Check P3 signals
    p3_matches = [s for s in _P3_SIGNALS if re.search(r"\b" + re.escape(s) + r"\b", text_combined)]
    if p3_matches and category != TriageCategory.BUG:
        return (
            TriagePriority.P3,
            f"Contains minor/cosmetic markers: {', '.join(p3_matches[:2])}.",
        )

    # Default for questions
    if category == TriageCategory.QUESTION:
        return (
            TriagePriority.P3,
            "Questions and usage inquiries do not block repository functionality.",
        )

    # General standard bug or feature request default to P2
    if category == TriageCategory.BUG:
        return (
            TriagePriority.P2,
            "Standard bug report with no observed critical crash or security markers.",
        )

    return (
        TriagePriority.P2,
        "Normal feature enhancement or maintenance proposal.",
    )


def generate_heuristic_summary(title: str, body: str, category: TriageCategory) -> str:
    """Produce a concise 1-2 sentence plain-language summary."""
    clean_title = title.strip()
    first_paragraph = ""
    for paragraph in body.split("\n\n"):
        p = paragraph.strip()
        if p and not p.startswith("```") and not p.startswith("#"):
            first_paragraph = " ".join(p.split())
            break

    if first_paragraph and len(first_paragraph) > 20:
        truncated_first_p = first_paragraph[:160] + "..." if len(first_paragraph) > 160 else first_paragraph
        return f"{clean_title}. Context: {truncated_first_p}"
    return f"{clean_title}. (Category: {category.value})"


def generate_investigation_checklist(
    category: TriageCategory,
    priority: TriagePriority,
    repro: ReproductionStatus,
) -> list[str]:
    """Generate practical, actionable next steps for a maintainer."""
    checklist: list[str] = []

    # Repro-specific steps
    if not repro.has_repro_steps:
        checklist.append("Request reproduction steps from author using the repository issue template.")
    if not repro.has_environment_info:
        checklist.append("Ask author for runtime/OS details and exact package versions.")
    if not repro.has_logs_or_code:
        checklist.append("Request a minimal reproducible script or full traceback log.")

    # Category and Priority specific steps
    if category == TriageCategory.BUG:
        if priority in (TriagePriority.P0, TriagePriority.P1):
            checklist.append("Verify if this is a regression introduced in the latest release tag.")
            checklist.append("Assess user blast radius and determine if a hotfix patch is warranted.")
        checklist.append("Attempt local reproduction in an isolated virtual environment.")
        checklist.append("Draft a failing automated test case reproducing the reported failure.")
    elif category == TriageCategory.FEATURE:
        checklist.append("Assess alignment with repository roadmap and maintenance capacity.")
        checklist.append("Check for existing related PRs or similar previously closed feature requests.")
        checklist.append("Request API signature / usage example if not provided in the issue.")
    elif category == TriageCategory.DOCUMENTATION:
        checklist.append("Locate target documentation file, guide, or docstring.")
        checklist.append("Verify technical accuracy against current codebase implementation.")
        checklist.append("Review or create a concise documentation PR.")
    elif category == TriageCategory.QUESTION:
        checklist.append("Direct user to official documentation or GitHub Discussions / community forum.")
        checklist.append("Consider updating FAQ or README if this question is frequently asked.")
    else:
        checklist.append("Review issue scope with core maintainers.")
        checklist.append("Clarify expected outcome with the issue author.")

    return checklist[:5]


def triage_issue(issue: GitHubIssue) -> TriageResult:
    """Triage a GitHub issue using transparent, deterministic heuristics."""
    title_sanitized = sanitize_untrusted_input(issue.title, max_chars=300)
    body_sanitized = sanitize_untrusted_input(issue.body, max_chars=4000)

    category, confidence, signals, conf_expl = classify_issue_heuristically(
        title_sanitized, body_sanitized, issue.labels
    )
    priority, priority_reason = estimate_priority_heuristically(
        category, title_sanitized, body_sanitized, issue.labels
    )
    repro = evaluate_reproduction_status(title_sanitized, body_sanitized)
    summary = generate_heuristic_summary(title_sanitized, body_sanitized, category)
    checklist = generate_investigation_checklist(category, priority, repro)

    return TriageResult(
        issue_number=issue.number,
        title=issue.title,
        category=category,
        priority=priority,
        priority_reasoning=priority_reason,
        confidence=confidence,
        confidence_explanation=conf_expl,
        method="Deterministic Rule-Based Engine",
        summary=summary,
        reproduction=repro,
        investigation_checklist=checklist,
        matched_signals=signals,
    )
