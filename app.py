"""RepoRelief AI — Streamlit Application

A lightweight, read-only GitHub issue triage assistant designed for open-source maintainers.
Provides transparent classification, priority estimation, missing reproduction audits,
and actionable investigation checklists using deterministic heuristics or optional local Ollama LLMs.
"""

from __future__ import annotations

import json
import os
from io import StringIO
from typing import Any

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from github_client import (
    GitHubAPIError,
    GitHubAuthError,
    GitHubClient,
    GitHubNotFoundError,
    GitHubRateLimitError,
    parse_github_url,
)
from triage_engine import (
    OllamaClient,
    TriageCategory,
    TriagePriority,
    triage_issue,
    triage_issue_heuristically,
)

# Load optional .env variables
load_dotenv()

# Streamlit Page Configuration
st.set_page_config(
    page_title="RepoRelief AI — GitHub Issue Triage",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Custom Styling for polished UI cards and badges
st.markdown(
    """
    <style>
    .metric-card {
        background-color: #f8f9fa;
        border: 1px solid #e9ecef;
        border-radius: 8px;
        padding: 14px 18px;
        text-align: center;
    }
    .badge {
        display: inline-block;
        padding: 3px 8px;
        font-size: 12px;
        font-weight: 600;
        border-radius: 12px;
        margin-right: 6px;
    }
    .badge-p0 { background-color: #ffebee; color: #c62828; border: 1px solid #ffcdd2; }
    .badge-p1 { background-color: #fff3e0; color: #ef6c00; border: 1px solid #ffe0b2; }
    .badge-p2 { background-color: #e3f2fd; color: #1565c0; border: 1px solid #bbdefb; }
    .badge-p3 { background-color: #f1f8e9; color: #33691e; border: 1px solid #dcedc8; }
    .badge-bug { background-color: #fce4ec; color: #ad1457; }
    .badge-feature { background-color: #ede7f6; color: #4527a0; }
    .badge-doc { background-color: #e0f2f1; color: #00695c; }
    .badge-question { background-color: #fff8e1; color: #f57f17; }
    .badge-other { background-color: #eceff1; color: #37474f; }
    .privacy-box {
        background-color: #f0f7ff;
        border-left: 4px solid #0066cc;
        padding: 10px 14px;
        border-radius: 4px;
        font-size: 13px;
        margin-top: 15px;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


def get_priority_badge(priority_val: str) -> str:
    css_class = f"badge-p{priority_val[1:]}" if len(priority_val) >= 2 else "badge-p2"
    return f'<span class="badge {css_class}">Priority: {priority_val}</span>'


def get_category_badge(cat_val: str) -> str:
    mapping = {
        "bug": "badge-bug",
        "feature_request": "badge-feature",
        "documentation": "badge-doc",
        "question": "badge-question",
        "other": "badge-other",
    }
    css = mapping.get(cat_val, "badge-other")
    label = cat_val.replace("_", " ").title()
    return f'<span class="badge {css}">{label}</span>'


def render_sidebar() -> dict[str, Any]:
    st.sidebar.title("🛡️ RepoRelief AI")
    st.sidebar.caption("Open-Source Issue Triage Assistant")

    st.sidebar.markdown("---")
    st.sidebar.subheader("Target Repository")

    # Sample Repositories helper
    sample_repos = [
        "Select a sample repository...",
        "pallets/flask",
        "psf/requests",
        "encode/starlette",
        "tiangolo/fastapi",
        "pydantic/pydantic",
    ]
    selected_sample = st.sidebar.selectbox("Quick-load sample repo:", sample_repos)
    default_repo = selected_sample if selected_sample != sample_repos[0] else "pallets/flask"

    repo_input = st.sidebar.text_input(
        "GitHub Repo URL or owner/repo:",
        value=default_repo,
        help="Accepts 'owner/repo', 'https://github.com/owner/repo', or GitHub issue URLs.",
    )

    max_issues = st.sidebar.slider(
        "Max issues to retrieve:",
        min_value=5,
        max_value=50,
        value=15,
        step=5,
        help="Higher values take more API requests. Public unauthenticated limit is 60 req/hr.",
    )

    st.sidebar.markdown("---")
    st.sidebar.subheader("Triage Engine")

    engine_mode = st.sidebar.radio(
        "Select Analysis Mode:",
        [
            "Rule-Based Engine (Deterministic)",
            "Local LLM via Ollama (Optional)",
        ],
        index=0,
        help=(
            "Rule-Based works instantly without dependencies and with 0 RAM overhead. "
            "Local LLM uses your installed Ollama instance (e.g. llama3.2:3b) and falls back safely to rules."
        ),
    )

    use_llm = engine_mode == "Local LLM via Ollama (Optional)"
    ollama_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    ollama_model = os.getenv("OLLAMA_MODEL", "llama3.2:3b")

    if use_llm:
        st.sidebar.markdown("##### 🦙 Ollama Configuration")
        ollama_url = st.sidebar.text_input("Ollama Host URL:", value=ollama_url)
        ollama_model = st.sidebar.text_input("Ollama Model Name:", value=ollama_model)

        if st.sidebar.button("Test Ollama Connection"):
            test_client = OllamaClient(base_url=ollama_url, model=ollama_model)
            online, msg = test_client.check_health()
            if online:
                st.sidebar.success(f"Connected! {msg}")
            else:
                st.sidebar.warning(f"Ollama not detected: {msg}. Will fall back to rule-based engine.")

    st.sidebar.markdown("---")
    st.sidebar.subheader("Authentication (Optional)")
    env_token = os.getenv("GITHUB_TOKEN", "")
    token_input = st.sidebar.text_input(
        "GitHub Personal Access Token:",
        value=env_token,
        type="password",
        help="Optional. Public repositories work without a token. A token raises rate limit from 60 to 5,000 requests/hour.",
    )

    fetch_button = st.sidebar.button(
        "🚀 Fetch & Triage Issues",
        type="primary",
        use_container_width=True,
    )

    st.sidebar.markdown("---")
    st.sidebar.markdown(
        """
        <div style='font-size: 11px; color: #666;'>
        <strong>Privacy & Security:</strong><br/>
        • 100% Read-Only (no write API calls)<br/>
        • Zero cloud LLM API keys required<br/>
        • Input sanitized against prompt injections<br/>
        • Local analysis only
        </div>
        """,
        unsafe_allow_html=True,
    )

    return {
        "repo_input": repo_input,
        "max_issues": max_issues,
        "use_llm": use_llm,
        "ollama_url": ollama_url,
        "ollama_model": ollama_model,
        "token": token_input,
        "fetch_button": fetch_button,
    }


def main() -> None:
    config = render_sidebar()

    # Main Page Title & Header
    st.title("🛡️ RepoRelief AI")
    st.markdown(
        "**Lightweight GitHub Issue Triage Assistant for Open-Source Maintainers.** "
        "Understand incoming issues faster, identify missing reproduction details, and prioritize work objectively."
    )

    # Privacy and Read-Only Banner
    st.markdown(
        """
        <div class="privacy-box">
            <strong>🔒 Read-Only & Privacy Assurance:</strong> RepoRelief AI never alters, labels, comments on, or closes issues.
            Data is fetched from public GitHub repositories and processed entirely in-memory on your machine.
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.write("")

    # Initialize session state for storing triage results
    if "triage_results" not in st.session_state:
        st.session_state.triage_results = None
    if "current_repo" not in st.session_state:
        st.session_state.current_repo = None
    if "rate_limit_info" not in st.session_state:
        st.session_state.rate_limit_info = None

    # Handle Fetch Action
    if config["fetch_button"]:
        repo_str = config["repo_input"].strip()
        if not repo_str:
            st.error("Please enter a valid GitHub repository URL or slug (e.g. 'pallets/flask').")
            return

        try:
            repo_ref = parse_github_url(repo_str)
        except ValueError as exc:
            st.error(f"Invalid Repository Input: {exc}")
            return

        client = GitHubClient(token=config["token"])
        ollama_client = (
            OllamaClient(base_url=config["ollama_url"], model=config["ollama_model"])
            if config["use_llm"]
            else None
        )

        with st.status(f"Fetching issues for {repo_ref.full_name}...", expanded=True) as status:
            st.write(f"Connecting to GitHub REST API (Target: `{repo_ref.full_name}`)...")

            try:
                issues, rate_info = client.fetch_issues(
                    owner=repo_ref.owner,
                    repo=repo_ref.name,
                    max_issues=config["max_issues"],
                )
                st.session_state.rate_limit_info = rate_info
            except GitHubNotFoundError as exc:
                status.update(label="Repository Not Found", state="error")
                st.error(str(exc))
                return
            except GitHubRateLimitError as exc:
                status.update(label="Rate Limit Exceeded", state="error")
                st.error(str(exc))
                return
            except GitHubAuthError as exc:
                status.update(label="Authentication Failed", state="error")
                st.error(str(exc))
                return
            except GitHubAPIError as exc:
                status.update(label="GitHub API Error", state="error")
                st.error(str(exc))
                return

            if not issues:
                status.update(label="No Open Issues Found", state="complete")
                st.info(f"Repository `{repo_ref.full_name}` currently has 0 open issues (excluding pull requests).")
                st.session_state.triage_results = []
                st.session_state.current_repo = repo_ref.full_name
                return

            st.write(f"Retrieved {len(issues)} open issues (pull requests filtered out). Triaging...")

            results = []
            progress_bar = st.progress(0.0)
            for idx, issue in enumerate(issues):
                res = triage_issue(
                    issue=issue,
                    use_llm=config["use_llm"],
                    ollama_client=ollama_client,
                )
                results.append((issue, res))
                progress_bar.progress((idx + 1) / len(issues))

            status.update(
                label=f"Triaged {len(results)} issues successfully!",
                state="complete",
                expanded=False,
            )
            st.session_state.triage_results = results
            st.session_state.current_repo = repo_ref.full_name

    # Display Results if available
    if st.session_state.triage_results is not None and len(st.session_state.triage_results) > 0:
        results: list[tuple[Any, Any]] = st.session_state.triage_results
        repo_name = st.session_state.current_repo

        # Top summary metrics
        total = len(results)
        bugs_count = sum(1 for _, r in results if r.category == TriageCategory.BUG)
        features_count = sum(1 for _, r in results if r.category == TriageCategory.FEATURE)
        urgent_count = sum(1 for _, r in results if r.priority in (TriagePriority.P0, TriagePriority.P1))
        missing_repro_count = sum(1 for _, r in results if r.reproduction.completeness == "Missing")

        st.subheader(f"📊 Triage Overview for `{repo_name}`")

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Total Issues", total)
        c2.metric("Bugs", bugs_count)
        c3.metric("Features", features_count)
        c4.metric("Urgent (P0/P1)", urgent_count, delta="Requires action" if urgent_count > 0 else "Clear", delta_color="inverse")
        c5.metric("Missing Repro", missing_repro_count, delta="Incomplete" if missing_repro_count > 0 else "Good", delta_color="inverse")

        # Rate Limit info banner
        if st.session_state.rate_limit_info:
            rl = st.session_state.rate_limit_info
            st.caption(
                f"GitHub API Quota: **{rl.remaining}/{rl.limit}** requests remaining. "
                f"Resets at {rl.reset_time_formatted}."
            )

        # Filters and search
        st.markdown("---")
        st.subheader("Filter and Search")
        f_col1, f_col2, f_col3, f_col4 = st.columns([2, 2, 2, 3])

        category_filter = f_col1.selectbox(
            "Category:",
            ["All", "Bug", "Feature Request", "Documentation", "Question", "Other"],
        )
        priority_filter = f_col2.selectbox(
            "Priority:",
            ["All", "P0 (Blocker)", "P1 (High)", "P2 (Medium)", "P3 (Low)"],
        )
        repro_filter = f_col3.selectbox(
            "Reproduction Status:",
            ["All", "Complete", "Partial", "Missing"],
        )
        search_query = f_col4.text_input("Search issues (title/summary):", "")

        # Apply Filters
        filtered_results = []
        for issue, res in results:
            if category_filter != "All":
                cat_key = category_filter.lower().replace(" ", "_")
                if res.category.value != cat_key:
                    continue

            if priority_filter != "All":
                prio_key = priority_filter.split()[0]
                if res.priority.value != prio_key:
                    continue

            if repro_filter != "All":
                if res.reproduction.completeness != repro_filter:
                    continue

            if search_query:
                q = search_query.lower()
                if q not in issue.title.lower() and q not in res.summary.lower():
                    continue

            filtered_results.append((issue, res))

        st.caption(f"Showing **{len(filtered_results)}** of **{len(results)}** issues")

        # Render each issue card
        for issue, res in filtered_results:
            with st.container():
                st.markdown(
                    f"#### [#{issue.number}: {issue.title}]({issue.html_url})",
                    help="Click to open this issue on GitHub (new tab)",
                )

                # Badges row
                cat_badge = get_category_badge(res.category.value)
                prio_badge = get_priority_badge(res.priority.value)

                repro_color = (
                    "green"
                    if res.reproduction.completeness == "Complete"
                    else "orange"
                    if res.reproduction.completeness == "Partial"
                    else "red"
                )
                repro_badge = f'<span class="badge" style="background-color: #f5f5f5; color: {repro_color}; border: 1px solid #ccc;">Repro: {res.reproduction.completeness}</span>'

                conf_badge = f'<span class="badge" style="background-color: #e8eaf6; color: #283593;">Confidence: {res.confidence.value}</span>'
                method_badge = f'<span class="badge" style="background-color: #eceff1; color: #455a64;">Engine: {res.method}</span>'

                st.markdown(
                    f"{cat_badge} {prio_badge} {repro_badge} {conf_badge} {method_badge}",
                    unsafe_allow_html=True,
                )

                # If LLM fallback occurred, display notice
                if res.fallback_reason:
                    st.warning(
                        f"⚠️ **LLM Fallback Activated:** {res.fallback_reason}. "
                        "Transparently analyzed using deterministic heuristics."
                    )

                # Summary & Reason
                st.markdown(f"**Summary:** {res.summary}")
                st.markdown(
                    f"**Priority Justification ({res.priority.value}):** {res.priority_reasoning}"
                )
                st.caption(f"Confidence Assessment: {res.confidence_explanation}")

                # Tabs for Details: Missing Repro, Checklist, and Original Issue
                tab_repro, tab_checklist, tab_original = st.tabs(
                    ["🔍 Reproduction Audit", "📋 Actionable Checklist", "📄 Original Issue Body"]
                )

                with tab_repro:
                    r = res.reproduction
                    col_p1, col_p2 = st.columns(2)
                    col_p1.markdown(
                        f"- {'✅' if r.has_repro_steps else '❌'} Steps to reproduce\n"
                        f"- {'✅' if r.has_expected_actual else '❌'} Expected vs. actual behavior"
                    )
                    col_p2.markdown(
                        f"- {'✅' if r.has_environment_info else '❌'} Environment / OS / versions\n"
                        f"- {'✅' if r.has_logs_or_code else '❌'} Traceback, logs, or code sample"
                    )

                    if r.missing_items:
                        st.markdown("**Missing Information to Request from Contributor:**")
                        for item in r.missing_items:
                            st.markdown(f"  • *{item}*")
                    else:
                        st.success("Great report! All essential reproduction elements are present.")

                with tab_checklist:
                    st.markdown("**Maintainer Next Steps:**")
                    for step in res.investigation_checklist:
                        st.checkbox(step, key=f"check_{issue.number}_{step[:25]}")

                with tab_original:
                    if issue.body.strip():
                        st.markdown(issue.body)
                    else:
                        st.info("No issue description provided by author.")

                st.markdown("---")

        # Export Section
        st.subheader("📥 Export Triage Results")
        exp_col1, exp_col2 = st.columns(2)

        # JSON Export
        export_data = [res.to_dict() for _, res in results]
        json_str = json.dumps(export_data, indent=2)
        exp_col1.download_button(
            label="Download Results as JSON",
            data=json_str,
            file_name=f"{repo_name.replace('/', '_')}_triage_results.json",
            mime="application/json",
            use_container_width=True,
        )

        # CSV Export
        flat_records = []
        for issue, res in results:
            flat_records.append(
                {
                    "issue_number": issue.number,
                    "title": issue.title,
                    "author": issue.author,
                    "category": res.category.value,
                    "priority": res.priority.value,
                    "priority_reasoning": res.priority_reasoning,
                    "confidence": res.confidence.value,
                    "confidence_explanation": res.confidence_explanation,
                    "method": res.method,
                    "summary": res.summary,
                    "reproduction_completeness": res.reproduction.completeness,
                    "missing_items": "; ".join(res.reproduction.missing_items),
                    "checklist": "; ".join(res.investigation_checklist),
                    "url": issue.html_url,
                }
            )
        df = pd.DataFrame(flat_records)
        csv_buffer = StringIO()
        df.to_csv(csv_buffer, index=False)
        exp_col2.download_button(
            label="Download Results as CSV",
            data=csv_buffer.getvalue(),
            file_name=f"{repo_name.replace('/', '_')}_triage_results.csv",
            mime="text/csv",
            use_container_width=True,
        )

    elif st.session_state.triage_results is not None and len(st.session_state.triage_results) == 0:
        st.info("No open issues found for this repository.")
    else:
        # Welcome & Empty State Guide
        st.info(
            "👋 **Welcome to RepoRelief AI!** Enter any public GitHub repository URL or slug in the sidebar "
            "(e.g., `pallets/flask` or `psf/requests`) and click **'🚀 Fetch & Triage Issues'** to begin."
        )

        st.markdown(
            """
            ### What RepoRelief AI Does
            - **🏷️ Automated Categorization:** Classifies issues into `bug`, `feature_request`, `documentation`, `question`, or `other`.
            - **⚡ Impact-Based Priority Heuristics:** Assigns `P0` (Critical Blocker), `P1` (High), `P2` (Normal), or `P3` (Low).
            - **🔍 Reproduction Audit:** Automatically checks for reproduction steps, expected/actual outcomes, environment specs, and stack traces.
            - **📋 Actionable Checklist:** Generates immediate next steps for maintainers.
            - **🛡️ 100% Read-Only:** Safely inspects issues without modifying, commenting on, or labeling your repository.
            - **⚙️ Deterministic & Local:** Runs completely on your machine with 0 external AI API costs.
            """
        )


if __name__ == "__main__":
    main()
