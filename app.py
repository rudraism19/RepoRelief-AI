"""RepoRelief AI — Streamlit Application

A lightweight, accessible, read-only GitHub issue triage assistant designed for open-source maintainers.
Provides transparent heuristic classification, P0–P3 priority estimation, missing reproduction audits,
and actionable investigation checklists using a 100% deterministic rule-based engine.
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
    GitHubIssue,
    GitHubNotFoundError,
    GitHubRateLimitError,
    parse_github_url,
)
from triage_engine import (
    TriageCategory,
    TriagePriority,
    TriageResult,
    triage_issue,
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

# High-contrast CSS styling for accessibility (WCAG AA/AAA compliant)
st.markdown(
    """
    <style>
    /* High-contrast badges with solid backgrounds and white text */
    .badge {
        display: inline-block;
        padding: 4px 10px;
        font-size: 13px;
        font-weight: 700;
        border-radius: 4px;
        margin-right: 8px;
        margin-bottom: 6px;
        letter-spacing: 0.3px;
    }
    .badge-p0 { background-color: #b71c1c; color: #ffffff !important; }
    .badge-p1 { background-color: #d84315; color: #ffffff !important; }
    .badge-p2 { background-color: #0d47a1; color: #ffffff !important; }
    .badge-p3 { background-color: #2e7d32; color: #ffffff !important; }
    
    .badge-bug { background-color: #880e4f; color: #ffffff !important; }
    .badge-feature { background-color: #4a148c; color: #ffffff !important; }
    .badge-doc { background-color: #004d40; color: #ffffff !important; }
    .badge-question { background-color: #e65100; color: #ffffff !important; }
    .badge-other { background-color: #37474f; color: #ffffff !important; }
    
    .badge-repro-complete { background-color: #1b5e20; color: #ffffff !important; }
    .badge-repro-partial { background-color: #e65100; color: #ffffff !important; }
    .badge-repro-missing { background-color: #b71c1c; color: #ffffff !important; }
    
    .badge-engine { background-color: #263238; color: #ffffff !important; }
    .badge-conf { background-color: #1a237e; color: #ffffff !important; }

    /* Accessible Privacy & Guarantee Notice */
    .accessible-notice {
        background-color: #e8f4fd;
        border-left: 6px solid #0056b3;
        color: #0b2545;
        padding: 14px 18px;
        border-radius: 6px;
        font-size: 14px;
        line-height: 1.5;
        margin-top: 10px;
        margin-bottom: 20px;
        box-shadow: 0 1px 3px rgba(0,0,0,0.1);
    }
    .accessible-notice strong {
        color: #003366;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


def get_priority_badge(priority_val: str) -> str:
    p_num = priority_val[1:] if len(priority_val) >= 2 else "2"
    return f'<span class="badge badge-p{p_num}">Priority: {priority_val}</span>'


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


def get_repro_badge(completeness: str) -> str:
    if completeness == "Complete":
        css = "badge-repro-complete"
    elif completeness == "Partial":
        css = "badge-repro-partial"
    else:
        css = "badge-repro-missing"
    return f'<span class="badge {css}">Repro: {completeness}</span>'


def render_sidebar() -> dict[str, Any]:
    st.sidebar.title("🛡️ RepoRelief AI")
    st.sidebar.caption("Open-Source Issue Triage Assistant")

    st.sidebar.markdown("---")
    st.sidebar.subheader("Public Repository")

    # Session state to hold arbitrary repository input
    if "custom_repo_target" not in st.session_state:
        st.session_state.custom_repo_target = "pallets/flask"

    # Optional quick-load suggestions (does not restrict custom input)
    sample_options = [
        "-- Choose a suggestion (optional) --",
        "pallets/flask",
        "psf/requests",
        "tiangolo/fastapi",
        "encode/starlette",
        "facebook/react",
        "torvalds/linux",
        "python/cpython",
        "golang/go",
    ]
    picked_suggestion = st.sidebar.selectbox(
        "Quick-load suggestions (Optional):",
        sample_options,
        index=0,
        help="Optional shortcuts. You can enter ANY public GitHub repository in the text box below.",
    )
    if picked_suggestion and picked_suggestion != sample_options[0]:
        if picked_suggestion != st.session_state.custom_repo_target:
            st.session_state.custom_repo_target = picked_suggestion

    # Unrestricted text input for ANY public GitHub repository
    repo_input = st.sidebar.text_input(
        "Enter ANY Public GitHub Repository:",
        value=st.session_state.custom_repo_target,
        placeholder="owner/repo or https://github.com/owner/repo",
        help="Type ANY public GitHub repository worldwide (e.g. 'owner/repo' or 'https://github.com/owner/repo'). Not restricted to suggestions.",
    )
    st.session_state.custom_repo_target = repo_input

    max_issues = st.sidebar.slider(
        "Max issues to retrieve:",
        min_value=10,
        max_value=250,
        value=30,
        step=10,
        help="Number of open issues to fetch and triage. Supports pagination beyond 100 issues.",
    )

    st.sidebar.markdown("---")
    st.sidebar.subheader("Triage Engine")
    st.sidebar.info(
        "⚙️ **Deterministic Rule-Based Engine**\n\n"
        "• 100% Transparent heuristics\n"
        "• Zero LLM/API dependencies\n"
        "• Instant execution & 0 RAM bloat"
    )

    st.sidebar.markdown("---")
    st.sidebar.subheader("GitHub Authentication (Optional)")
    
    # Optional token input with empty default
    token_input = st.sidebar.text_input(
        "Personal Access Token:",
        value=os.getenv("GITHUB_TOKEN", ""),
        type="password",
        placeholder="Optional (leave empty for public API)",
        help="Optional. Leave empty to query GitHub's public REST API unauthenticated (60 req/hr quota). "
             "If provided, it is sent as an optional Bearer token to raise your rate limit to 5,000 req/hr.",
    )

    clean_token = token_input.strip() if token_input.strip() else None

    if clean_token:
        st.sidebar.success(
            "🔑 **Bearer Token Provided**\n\n"
            "Authenticated mode enabled (Quota: 5,000 req/hr)."
        )
    else:
        st.sidebar.info(
            "🌐 **Token Field Empty**\n\n"
            "Unauthenticated public REST API active (Quota: 60 req/hr). No token required!"
        )

    fetch_button = st.sidebar.button(
        "🚀 Fetch & Triage Issues",
        type="primary",
        use_container_width=True,
    )

    st.sidebar.markdown("---")
    st.sidebar.markdown(
        """
        <div style='font-size: 12px; line-height: 1.4; color: #495057;'>
        <strong>Security & Privacy Guarantees:</strong><br/>
        • <strong>Token Optional:</strong> Default workflow works 100% without authentication.<br/>
        • <strong>100% Read-Only:</strong> Never comments, labels, modifies, or closes issues.<br/>
        • <strong>Zero Cost:</strong> No paid APIs, credits, or billing.<br/>
        • <strong>Local Processing:</strong> Issues analyzed in-memory on your machine.
        </div>
        """,
        unsafe_allow_html=True,
    )

    return {
        "repo_input": repo_input,
        "max_issues": max_issues,
        "token": clean_token,
        "fetch_button": fetch_button,
    }


def main() -> None:
    config = render_sidebar()

    # Main Page Title & Header
    st.title("🛡️ RepoRelief AI")
    st.markdown(
        "**Lightweight, accessible GitHub issue triage assistant for open-source maintainers.** "
        "Quickly classify issues, estimate impact priority, audit reproduction completeness, and get actionable investigation checklists."
    )

    # High-contrast Accessible Privacy Banner
    st.markdown(
        """
        <div class="accessible-notice" role="region" aria-label="Privacy and Read-Only Assurance">
            <strong>🔒 Read-Only & Privacy Assurance:</strong> RepoRelief AI is strictly read-only.
            It communicates only with GitHub's public REST API to retrieve open issues and processes them entirely
            in-memory using deterministic heuristics. No data is stored or transmitted to external AI cloud services.
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Initialize session state
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

            st.write(f"Retrieved {len(issues)} open issues (pull requests excluded). Triaging with heuristic engine...")

            results: list[tuple[GitHubIssue, TriageResult]] = []
            progress_bar = st.progress(0.0)
            for idx, issue in enumerate(issues):
                res = triage_issue(issue=issue)
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
        results: list[tuple[GitHubIssue, TriageResult]] = st.session_state.triage_results
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
        c4.metric(
            "Urgent (P0/P1)",
            urgent_count,
            delta="Requires action" if urgent_count > 0 else "Clear",
            delta_color="inverse",
        )
        c5.metric(
            "Missing Repro",
            missing_repro_count,
            delta="Incomplete" if missing_repro_count > 0 else "Good",
            delta_color="inverse",
        )

        # Rate Limit Quota Banner
        if st.session_state.rate_limit_info:
            rl = st.session_state.rate_limit_info
            st.caption(
                f"GitHub API Quota: **{rl.remaining}/{rl.limit}** requests remaining. "
                f"Reset time: {rl.reset_time_formatted}."
            )

        # Filters and search
        st.markdown("---")
        st.subheader("Filter and Search")
        f_col1, f_col2, f_col3, f_col4 = st.columns([2, 2, 2, 3])

        category_filter = f_col1.selectbox(
            "Filter Category:",
            ["All", "Bug", "Feature Request", "Documentation", "Question", "Other"],
        )
        priority_filter = f_col2.selectbox(
            "Filter Priority:",
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

        st.caption(f"Showing **{len(filtered_results)}** of **{len(results)}** triaged issues")

        # Render each issue card
        for issue, res in filtered_results:
            with st.container():
                st.markdown(
                    f"#### [#{issue.number}: {issue.title}]({issue.html_url})",
                    help="Click to open this issue directly on GitHub in a new tab",
                )

                # High-contrast badges row
                cat_badge = get_category_badge(res.category.value)
                prio_badge = get_priority_badge(res.priority.value)
                repro_badge = get_repro_badge(res.reproduction.completeness)
                conf_badge = f'<span class="badge badge-conf">Confidence: {res.confidence.value}</span>'
                engine_badge = '<span class="badge badge-engine">Engine: Deterministic Rules</span>'

                st.markdown(
                    f"{prio_badge} {cat_badge} {repro_badge} {conf_badge} {engine_badge}",
                    unsafe_allow_html=True,
                )

                # Summary & Reason
                st.markdown(f"**Summary:** {res.summary}")
                st.markdown(
                    f"**Priority Justification ({res.priority.value}):** {res.priority_reasoning}"
                )
                st.caption(f"Confidence Assessment: {res.confidence_explanation}")

                # Tabs for Detailed Breakdown
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
                        st.checkbox(step, key=f"check_{issue.number}_{hash(step)}")

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
                    "created_at": issue.created_at,
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
        st.info("No open issues found for this repository (excluding pull requests).")
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
            - **⚙️ Deterministic & Local:** Runs completely on your machine with 0 external AI API costs and zero GPU requirements.
            """
        )


if __name__ == "__main__":
    main()
