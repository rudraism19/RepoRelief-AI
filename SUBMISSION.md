---
title: RepoRelief AI — Automated GitHub Issue Triage Assistant
published: true
tags: hacktoberfest, devchallenge, ai, opensource
cover_image: https://raw.githubusercontent.com/rudraism19/RepoRelief-AI/main/assets/cover.jpg
canonical_url: https://github.com/rudraism19/RepoRelief-AI
---

# RepoRelief AI — Hacktoberfest 2026 Week 3 Submission

*This is a submission for the [Hacktoberfest Open-Source AI Challenge Week 3: Slow and Steady](https://dev.to/challenges/hacktoberfest-week3-2026-10-19)*

---

## What I Built

**RepoRelief AI** is an automated, lightweight, read-only GitHub issue triage assistant designed to help open-source maintainers understand incoming issues faster without triage burnout.

### What Effort Does It Save?
Triaging open-source repositories is often an exhausting manual chore:
- Reading lengthy issue reports only to realize the author forgot to include reproduction steps, environment versions, or stack traces.
- Sifting through minor documentation typos or questions while critical system-crashing bugs (`P0`/`P1`) sit unnoticed.
- Manually drafting follow-up questions asking contributors for missing info.

RepoRelief AI automates this entire initial inspection in milliseconds:
1. **Automated Categorization:** Classifies issues into `bug`, `feature_request`, `documentation`, `question`, or `other` using label markers and weighted keyword scoring.
2. **Impact-Based Priority Heuristics:** Assigns `P0` (Blocker), `P1` (High), `P2` (Medium), or `P3` (Low) with documented justifications.
3. **4-Pillar Reproduction Audit:** Instantly evaluates reports against 4 vital reproduction criteria:
   - Steps to reproduce
   - Expected vs. actual outcomes
   - Environment / OS / runtime details
   - Code snippet, traceback, or console error logs
4. **Actionable Maintainer Checklists:** Derives 3–5 concrete next steps tailored to the issue category and missing details, complete with interactive checkboxes.
5. **Zero API Cost & 100% Read-Only:** Works directly with GitHub's public unauthenticated REST API—no tokens, no paid subscriptions, and zero write mutations on the repository.

### Who Gets to Be Lazier?
**Open-source maintainers, project leads, and release managers.** Instead of spending 5 to 10 minutes reading and analyzing every issue individually, maintainers get instant high-contrast summaries, pre-filled contributor requests, and prioritized backlogs with a single click.

---

## Demo

- 🌐 **Live Web Application:** [https://rudraism19-reporelief-ai-app-uygfnh.streamlit.app/](https://rudraism19-reporelief-ai-app-uygfnh.streamlit.app/)

### Try It Live:
1. Open the [live demo](https://rudraism19-reporelief-ai-app-uygfnh.streamlit.app/).
2. Type or select any public repository in the world (e.g. `pallets/flask`, `psf/requests`, `tiangolo/fastapi`, `facebook/react`, or your own public projects).
3. Click **🚀 Fetch & Triage Issues** to see instant metrics, category badges, reproduction audits, and maintainer checklists.
4. Export the triaged backlog to JSON or CSV.

---

## Code

{% github rudraism19/RepoRelief-AI %}

- **GitHub Repository:** [https://github.com/rudraism19/RepoRelief-AI](https://github.com/rudraism19/RepoRelief-AI)
- **License:** Permissive open-source [MIT License](https://github.com/rudraism19/RepoRelief-AI/blob/main/LICENSE)
- **Test Suite:** 66 unit tests with 100% mocked network requests (`python -m pytest tests/ -v`).

---

## How I Built It

RepoRelief AI is engineered as a clean, modular Python application built on:
- **Streamlit:** Fast, accessible reactive UI with WCAG AA-compliant high-contrast badges and themes.
- **GitHub REST API Client:** Read-only client querying `GET /repos/{owner}/{repo}/issues?state=open&per_page=100`, handling pagination beyond 100 issues, filtering out pull requests, and providing friendly error diagnostics for rate limits (HTTP 403) and 404s.
- **Deterministic Heuristic Engine:** Fast, zero-RAM, transparent classification and priority estimation engine with anti-prompt-injection input sanitization.
- **AI-Assisted Development:** Built with AI pair programming and GitHub Copilot to rapidly prototype regex heuristics, draft comprehensive unit tests (66 test cases), structure accessible Streamlit UI cards, and handle complex edge cases such as PR-only repositories and network disconnections.

---

## Why Does Open Innovation Matter?

Open-source maintainers shouldn't need expensive $20/month enterprise AI SaaS subscriptions, paid API keys, or bulky GPU clusters just to manage incoming issues for their free software. 

Open innovation made it possible to build a tool that:
- Runs anywhere—from an 8 GB RAM laptop on Windows PowerShell to a free Streamlit Community Cloud instance.
- Operates 100% transparently: every classification, priority score, and reproduction check is backed by clear, documented rules rather than opaque black-box LLM hallucinations.
- Keeps maintainer and repository data private: untrusted issue text is processed locally in-memory without sending data to third-party closed AI APIs.

---

## The Lazy Payoff

- **Time Saved:** Manual issue triage typically consumes 5–10 minutes per issue. RepoRelief AI triages 30–100 open issues in under **1.5 seconds**.
- **No More Back-and-Forth Guesswork:** The 4-Pillar Reproduction Audit immediately highlights what's missing, allowing maintainers to copy pre-formatted questions directly to contributors on the first touchpoint.
- **Zero Config:** Works straight out of the box on any public repository without logging in or setting up API keys.

---

## Prize Categories

- **Best Use of GitHub Copilot**
