"""Triage engine for RepoRelief AI.

Provides deterministic heuristic classification, priority estimation,
missing reproduction detection, input sanitization, and optional local Ollama LLM integration
with transparent fallback.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import requests
from pydantic import BaseModel, Field, ValidationError

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
    method: str  # "Rule-based Heuristic" or "Local LLM (Ollama: <model>)"
    summary: str
    reproduction: ReproductionStatus
    investigation_checklist: list[str] = field(default_factory=list)
    matched_signals: list[str] = field(default_factory=list)
    fallback_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert triage result to serializable dictionary."""
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
            "fallback_reason": self.fallback_reason,
        }


# Pydantic model for validating structured output from Ollama
class OllamaStructuredOutput(BaseModel):
    category: str = Field(description="One of: bug, feature_request, documentation, question, other")
    priority: str = Field(description="One of: P0, P1, P2, P3")
    priority_reasoning: str = Field(description="Reasoning for assigned priority")
    confidence: str = Field(default="Medium", description="High, Medium, or Low")
    confidence_explanation: str = Field(default="", description="Explanation of confidence rating")
    missing_repro_items: list[str] = Field(default_factory=list)
    summary: str = Field(description="1-2 sentence issue summary")
    investigation_checklist: list[str] = Field(default_factory=list)


# --- Input Sanitization & Anti-Prompt-Injection ---

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
    """Analyze issue text for the 4 core reproduction pillars."""
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
        checklist.append("Request reproduction steps from author using the issue template.")
    if not repro.has_environment_info:
        checklist.append("Ask author for runtime/OS details and package versions.")
    if not repro.has_logs_or_code:
        checklist.append("Request a minimal reproducible script or full traceback log.")

    # Category and Priority specific steps
    if category == TriageCategory.BUG:
        if priority in (TriagePriority.P0, TriagePriority.P1):
            checklist.append("Verify if this is a regression introduced in the latest release tag.")
            checklist.append("Assess user blast radius and decide whether a hotfix release is required.")
        checklist.append("Attempt local reproduction in a clean virtual environment.")
        checklist.append("Draft a failing automated test case matching the reported bug.")
    elif category == TriageCategory.FEATURE:
        checklist.append("Assess alignment with repository roadmap and maintenance capacity.")
        checklist.append("Check for existing related PRs or similar rejected feature requests.")
        checklist.append("Request API signature / usage example if not provided.")
    elif category == TriageCategory.DOCUMENTATION:
        checklist.append("Locate target documentation file or docstring.")
        checklist.append("Verify accuracy against current code implementation.")
        checklist.append("Review or create a concise documentation PR.")
    elif category == TriageCategory.QUESTION:
        checklist.append("Direct user to official documentation or community discussions/Discord.")
        checklist.append("Consider updating FAQ or README if this question is frequently asked.")
    else:
        checklist.append("Review issue scope with core maintainers.")
        checklist.append("Clarify expected outcome with the issue author.")

    return checklist[:5]


def triage_issue_heuristically(issue: GitHubIssue) -> TriageResult:
    """Perform deterministic, transparent rule-based triage on a GitHub issue."""
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
        method="Rule-based Heuristic",
        summary=summary,
        reproduction=repro,
        investigation_checklist=checklist,
        matched_signals=signals,
        fallback_reason=None,
    )


# --- Optional Local Ollama LLM Integration ---

class OllamaClient:
    """Client for local Ollama inference with health checks and structured JSON output."""

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "llama3.2:3b",
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def check_health(self) -> tuple[bool, str]:
        """Check if local Ollama server is running and accessible."""
        url = f"{self.base_url}/api/tags"
        try:
            resp = requests.get(url, timeout=3.0)
            if resp.status_code == 200:
                models = resp.json().get("models", [])
                model_names = [m.get("name") for m in models if isinstance(m, dict)]
                return True, f"Ollama online. Available models: {', '.join(model_names) or 'None'}"
            return False, f"Ollama returned HTTP {resp.status_code}"
        except requests.exceptions.RequestException as exc:
            return False, f"Ollama unavailable at {self.base_url}: {exc}"

    def triage_with_llm(
        self,
        issue: GitHubIssue,
        prompt_template: str,
    ) -> tuple[OllamaStructuredOutput | None, str | None]:
        """Call Ollama with structured JSON format.

        Returns:
            Tuple of (OllamaStructuredOutput or None, failure_reason or None)
        """
        sanitized_title = sanitize_untrusted_input(issue.title, max_chars=300)
        sanitized_body = sanitize_untrusted_input(issue.body, max_chars=3500)
        labels_str = ", ".join(issue.labels) if issue.labels else "None"
        author_str = sanitize_untrusted_input(issue.author, max_chars=50)

        # Substitute into prompt template
        filled_prompt = prompt_template.replace("{{ISSUE_TITLE}}", sanitized_title)
        filled_prompt = filled_prompt.replace("{{ISSUE_LABELS}}", labels_str)
        filled_prompt = filled_prompt.replace("{{ISSUE_AUTHOR}}", author_str)
        filled_prompt = filled_prompt.replace("{{ISSUE_BODY}}", sanitized_body)

        endpoint = f"{self.base_url}/api/generate"
        payload = {
            "model": self.model,
            "prompt": filled_prompt,
            "format": "json",
            "stream": False,
            "options": {
                "temperature": 0.1,  # Low temperature for deterministic, factual parsing
                "num_predict": 512,
            },
        }

        try:
            resp = requests.post(endpoint, json=payload, timeout=self.timeout)
            if resp.status_code != 200:
                return None, f"Ollama returned HTTP {resp.status_code}: {resp.text[:120]}"

            data = resp.json()
            raw_response = data.get("response", "").strip()
            if not raw_response:
                return None, "Ollama returned empty response."

            parsed_json = json.loads(raw_response)
            validated = OllamaStructuredOutput.model_validate(parsed_json)
            return validated, None

        except requests.exceptions.Timeout:
            return None, f"Ollama request timed out after {self.timeout}s."
        except requests.exceptions.RequestException as exc:
            return None, f"Ollama connection error: {exc}"
        except (json.JSONDecodeError, ValidationError) as exc:
            return None, f"Failed to validate Ollama JSON response: {exc}"


def load_prompt_template(custom_path: Path | None = None) -> str:
    """Load the triage prompt template from disk."""
    default_path = Path(__file__).resolve().parent / "prompts" / "issue_triage_prompt.md"
    target_path = custom_path or default_path
    if not target_path.exists():
        raise FileNotFoundError(f"Prompt template file not found at {target_path}")
    return target_path.read_text(encoding="utf-8")


def triage_issue(
    issue: GitHubIssue,
    use_llm: bool = False,
    ollama_client: OllamaClient | None = None,
    prompt_template: str | None = None,
) -> TriageResult:
    """Triage a GitHub issue using LLM when available, falling back safely to deterministic rules.

    Guarantees:
    - Never pretends rule-based results came from an LLM.
    - If LLM fails or is disabled, transparently documents the fallback.
    """
    if not use_llm or ollama_client is None:
        return triage_issue_heuristically(issue)

    template = prompt_template or load_prompt_template()
    llm_output, failure_reason = ollama_client.triage_with_llm(issue, template)

    if llm_output is None or failure_reason is not None:
        # Transparent fallback to rule-based engine
        fallback_result = triage_issue_heuristically(issue)
        fallback_result.method = f"Rule-based Heuristic (Ollama Fallback)"
        fallback_result.fallback_reason = failure_reason
        return fallback_result

    # Map LLM structured output to typed enums safely
    cat_str = llm_output.category.lower().strip()
    category = TriageCategory.OTHER
    for enum_cat in TriageCategory:
        if enum_cat.value in cat_str or cat_str in enum_cat.value:
            category = enum_cat
            break

    prio_str = llm_output.priority.upper().strip()
    priority = TriagePriority.P2
    for enum_prio in TriagePriority:
        if enum_prio.value == prio_str:
            priority = enum_prio
            break

    conf_str = llm_output.confidence.capitalize().strip()
    confidence = TriageConfidence.MEDIUM
    for enum_conf in TriageConfidence:
        if enum_conf.value == conf_str:
            confidence = enum_conf
            break

    # Also compute deterministic reproduction status as a baseline
    repro = evaluate_reproduction_status(issue.title, issue.body)
    if llm_output.missing_repro_items:
        # Merge LLM observations with deterministic checklist
        combined_missing = list(dict.fromkeys(llm_output.missing_repro_items + repro.missing_items))
        repro.missing_items = combined_missing
        if len(combined_missing) >= 3:
            repro.completeness = "Missing"
        elif len(combined_missing) >= 1:
            repro.completeness = "Partial"
        else:
            repro.completeness = "Complete"

    checklist = llm_output.investigation_checklist or generate_investigation_checklist(
        category, priority, repro
    )

    return TriageResult(
        issue_number=issue.number,
        title=issue.title,
        category=category,
        priority=priority,
        priority_reasoning=llm_output.priority_reasoning or "Assessed via local LLM reasoning.",
        confidence=confidence,
        confidence_explanation=llm_output.confidence_explanation or "Generated by local LLM analysis.",
        method=f"Local LLM (Ollama: {ollama_client.model})",
        summary=llm_output.summary or generate_heuristic_summary(issue.title, issue.body, category),
        reproduction=repro,
        investigation_checklist=checklist,
        matched_signals=[f"LLM Model: {ollama_client.model}"],
        fallback_reason=None,
    )
