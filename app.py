"""Streamlit dashboard for Scholarship Scout Pro.

The crawler is async and long-running. Streamlit reruns its script on every
interaction, so we run ``asyncio.run`` inside a daemon thread and use
``SessionState`` (thread-safe, JSON-backed) as the shared state channel.

UX rules this file enforces:
* The user can always tell whether the crawler is IDLE / RUNNING / PAUSED /
  FAILED — there's a coloured status banner at the top.
* Any exception in the crawler thread is captured and rendered in the UI.
* While running, the live panels (progress, results, logs) auto-refresh
  every 2 s via ``st.fragment(run_every=...)``; the controls stay responsive.
* Buttons emit toasts so a click is never silent.
"""
from __future__ import annotations

import asyncio
import threading
import traceback
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st
import yaml

from db import Database
from exporter import COLUMNS, export, export_session, to_dataframe
from ranker import rank
from scraper_engine import (
    PLAYWRIGHT_AVAILABLE,
    STEALTH_AVAILABLE,
    ScraperEngine,
    build_queue,
)
from session_manager import SessionState, make_session_id
from verifier import Verifier


CONFIG_PATH = Path("config.yaml")
SELECTORS_PATH = Path("selectors.yaml")


# -------------------------------------------------------------- runtime helpers
@st.cache_data(show_spinner=False)
def load_yaml(path: str) -> dict[str, Any]:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def get_runtime() -> tuple[dict[str, Any], dict[str, Any], Database, SessionState, Verifier]:
    config = load_yaml(str(CONFIG_PATH))
    selectors = load_yaml(str(SELECTORS_PATH))
    db = Database(config["output"]["db_path"])
    state = SessionState(config["output"]["session_state_path"])
    verifier = Verifier(config)
    return config, selectors, db, state, verifier


def _thread_alive() -> bool:
    """Is the crawler thread (created in this browser session) still running?"""
    thread = st.session_state.get("crawler_thread")
    return bool(thread and thread.is_alive())


# ------------------------------------------------------------- crawler thread
def _crawler_thread(config, selectors, db, state, verifier) -> None:
    """Runs the async crawler. Any unhandled error is captured into SessionState."""
    async def _runner() -> None:
        engine = ScraperEngine(config, selectors, db, state, verifier)
        try:
            await engine.run()
            state.set_status("completed" if not state.is_paused() else "paused")
        finally:
            snap = state.snapshot()
            db.end_session(
                session_id=snap.get("session_id", "unknown"),
                end_time=datetime.utcnow().isoformat(timespec="seconds"),
                countries=engine.stats.countries,
                pages=snap.get("pages_visited", 0),
                records=snap.get("records_found", 0),
                status=snap.get("status") or ("paused" if state.is_paused() else "completed"),
            )

    try:
        asyncio.run(_runner())
    except Exception as exc:                         # crawler-level fatal
        tb = traceback.format_exc()
        state.set_error(str(exc), tb)
        state.log("error", f"crawler crashed: {exc}")


def preflight_check(state: SessionState, queue: list) -> str | None:
    """Return a user-friendly error string, or None if everything is OK."""
    if not queue:
        return (
            "No seed URLs are configured. Edit `config.yaml` and add URLs under `seeds:`."
        )
    if not PLAYWRIGHT_AVAILABLE:
        return (
            "Playwright is not installed. Run:\n\n"
            "```\npip install -r requirements.txt\nplaywright install chromium\n```"
        )
    return None


def start_crawler(*, resume: bool) -> None:
    config, selectors, db, state, verifier = get_runtime()

    if _thread_alive():
        st.toast("Crawler is already running.", icon="ℹ️")
        return

    queue = build_queue(config)
    err = preflight_check(state, queue)
    if err:
        state.set_error(err)
        st.toast("Cannot start — see error banner.", icon="🚫")
        return

    state.clear_error()
    if resume and state.session_id:
        state.resume(queue)
        db.start_session(state.session_id, datetime.utcnow().isoformat(timespec="seconds"))
        st.toast(f"Resuming session {state.session_id}…", icon="▶️")
    else:
        sid = make_session_id()
        db.start_session(sid, datetime.utcnow().isoformat(timespec="seconds"))
        state.init_session(sid, queue)
        st.toast(f"Started new session {sid}…", icon="▶️")

    thread = threading.Thread(
        target=_crawler_thread,
        args=(config, selectors, db, state, verifier),
        daemon=True,
        name=f"scraper-{state.session_id}",
    )
    thread.start()
    st.session_state["crawler_thread"] = thread


def request_pause(state: SessionState) -> None:
    if not _thread_alive():
        st.toast("Nothing to pause — crawler is not running.", icon="ℹ️")
        return
    state.request_pause()
    st.toast("Pause requested — will stop after the current page.", icon="⏸")


# -------------------------------------------------------------------- rendering
TIER_LABELS = {
    "Tier 1": "🥇 Tier 1 — Platinum (Full + Full Stipend)",
    "Tier 2": "🥈 Tier 2 — Gold (Full + Partial Stipend)",
    "Tier 3": "🥉 Tier 3 — Silver (Full + Work Rights)",
    "Tier 4": "🏅 Tier 4 — Bronze (Full Tuition Only)",
}
LEVEL_COLORS = {"info": "#16a34a", "warn": "#ca8a04", "error": "#dc2626"}


def render_status_banner(state: SessionState, queue_total: int) -> str:
    """Single source of truth for crawler state. Returns the derived status."""
    snap = state.snapshot()
    err = snap.get("error")
    paused = bool(snap.get("paused"))
    alive = _thread_alive()

    if err:
        status = "failed"
    elif alive and not paused:
        status = "running"
    elif paused:
        status = "paused"
    elif snap.get("session_id"):
        status = "completed"
    else:
        status = "idle"

    badges = {
        "idle":      ("⚪ Idle",      "Crawler hasn't been started yet.", st.info),
        "running":   ("🟢 Running",   f"Crawling — currently {snap.get('current_country','?')} / {snap.get('current_phase','?')}.", st.success),
        "paused":    ("⏸ Paused",    "Crawler stopped after the current page. Click ▶ Resume to continue.", st.warning),
        "completed": ("✅ Completed", "All queued URLs have been visited.", st.success),
        "failed":    ("🔴 Failed",    err.get("message", "Unknown error.") if err else "Unknown error.", st.error),
    }
    label, message, render_fn = badges[status]
    render_fn(f"**{label}** — {message}")

    if status == "failed" and err:
        with st.expander("Show full traceback", expanded=False):
            st.code(err.get("traceback") or err.get("message", ""), language="python")
        if st.button("Clear error", key="clear_err_btn"):
            state.clear_error()
            st.rerun()

    return status


def render_progress(state: SessionState, queue_total: int) -> None:
    snap = state.snapshot()
    cols = st.columns(4)
    cols[0].metric("Region/Country", snap.get("current_country") or "—")
    cols[1].metric("Phase", snap.get("current_phase") or "—")
    cols[2].metric(
        "Pages visited",
        snap.get("pages_visited", 0),
        help="Number of seed URLs the crawler has fetched so far in this session.",
    )
    cols[3].metric(
        "Records found",
        snap.get("records_found", 0),
        help="Verified scholarships saved to the database (confirmed + probable).",
    )

    visited = len(snap.get("visited", []))
    total = max(queue_total, visited, 1)
    pct = min(visited / total, 1.0)
    st.progress(
        pct,
        text=f"Pages crawled: {visited} of {total} seed URLs",
    )
    st.caption(
        f"The queue has {queue_total} seed URLs across the configured countries. "
        f"Each one is visited once per session; resumed sessions skip already-visited URLs."
    )

    last_hb = snap.get("last_heartbeat")
    if last_hb:
        st.caption(f"Last heartbeat: {last_hb} UTC")


def render_results(db: Database, config: dict[str, Any]) -> None:
    confirmed = db.fetch_scholarships(statuses=["confirmed"])
    probable = db.fetch_scholarships(statuses=["probable"])
    confirmed_ranked = rank(confirmed, config)

    counts = defaultdict(int)
    for r in confirmed_ranked:
        counts[r.get("funding_tier", "—")] += 1
    summary_cols = st.columns(4)
    for i, tier in enumerate(["Tier 1", "Tier 2", "Tier 3", "Tier 4"]):
        summary_cols[i].metric(tier, counts.get(tier, 0))

    if not confirmed_ranked and not probable:
        st.info("No scholarships discovered yet. Hit ▶ Start Scraping.")
        return

    by_country: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in confirmed_ranked:
        by_country[r.get("country") or "Unknown"].append(r)

    for country, rows in by_country.items():
        with st.expander(f"🌍 {country}  ·  {len(rows)} scholarships", expanded=False):
            by_tier: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for r in rows:
                by_tier[r.get("funding_tier", "—")].append(r)
            for tier in ["Tier 1", "Tier 2", "Tier 3", "Tier 4"]:
                tier_rows = by_tier.get(tier) or []
                if not tier_rows:
                    continue
                st.markdown(f"**{TIER_LABELS.get(tier, tier)}**")
                df = to_dataframe(tier_rows)
                st.dataframe(df, use_container_width=True, hide_index=True)

    if probable:
        st.subheader("Needs manual review (probable matches)")
        df = to_dataframe([r for r in probable if (r.get("confidence_score") or 0) >= 70])
        st.dataframe(df, use_container_width=True, hide_index=True)


def render_pipeline_stats(state: SessionState) -> None:
    """Reads the per-page summary log lines to give the user a clear picture
    of where listings are being filtered. This is the single best answer to
    the question 'is the website blocking me, or am I just not finding
    matching scholarships?'."""
    snap = state.snapshot()
    totals = {"extracted": 0, "junk": 0, "criteria": 0, "probable": 0, "confirmed": 0, "pages": 0}
    for entry in snap.get("logs", []):
        msg = entry.get("msg", "")
        if not msg.startswith("page summary:"):
            continue
        totals["pages"] += 1
        # Format: "page summary: extracted=21 -> junk=18, criteria=2, probable=1, confirmed=0"
        for token in ("extracted", "junk", "criteria", "probable", "confirmed"):
            marker = f"{token}="
            idx = msg.find(marker)
            if idx == -1:
                continue
            tail = msg[idx + len(marker):]
            num = ""
            for ch in tail:
                if ch.isdigit():
                    num += ch
                else:
                    break
            if num:
                totals[token] += int(num)

    cols = st.columns(6)
    cols[0].metric("Pages parsed", totals["pages"])
    cols[1].metric("Listings extracted", totals["extracted"])
    cols[2].metric(
        "Junk filtered",
        totals["junk"],
        help="Footer/nav links (Imprint, LinkedIn, Sitemap, etc.) discarded before verification.",
    )
    cols[3].metric(
        "Criteria-rejected",
        totals["criteria"],
        help="Real listings that did not pass the 5 eligibility checks (Bangladesh / field / intake / tier / English).",
    )
    cols[4].metric("Probable", totals["probable"])
    cols[5].metric("Confirmed", totals["confirmed"])

    if totals["pages"] and totals["confirmed"] == 0 and totals["probable"] == 0:
        if totals["extracted"] == 0:
            st.warning(
                "Pages loaded successfully but no listings were extracted. "
                "Add a per-host entry to `selectors.yaml` for these sites — "
                "the generic parser doesn't know their structure."
            )
        elif totals["junk"] >= 0.8 * totals["extracted"]:
            st.warning(
                "Most listings were filtered as nav/footer junk — meaning the "
                "generic parser is grabbing the page chrome instead of the "
                "scholarship cards. Add per-host CSS in `selectors.yaml`."
            )
        elif totals["criteria"] > 0:
            st.info(
                "Listings were extracted and looked real, but didn't pass the "
                "eligibility filters. Open the **Verification** log tab below "
                "to see the specific reason for each."
            )


def render_logs(state: SessionState) -> None:
    snap = state.snapshot()
    logs = list(reversed(snap.get("logs", [])))
    tabs = st.tabs(["Crawl Activity", "Errors", "Blocked / Skipped", "Verification"])

    def _filter(level_kinds: tuple[str, ...], substr: str | None = None) -> list[dict[str, Any]]:
        out = []
        for entry in logs:
            if entry.get("level") not in level_kinds:
                continue
            if substr and substr not in entry.get("msg", "").lower():
                continue
            out.append(entry)
        return out

    def _print(items: list[dict[str, Any]]) -> None:
        if not items:
            st.caption("No entries yet.")
            return
        for entry in items[:200]:
            color = LEVEL_COLORS.get(entry.get("level", "info"), "#374151")
            st.markdown(
                f"<div style='font-family:monospace;font-size:0.85rem;color:{color}'>"
                f"[{entry.get('ts','')}] {entry.get('msg','')}</div>",
                unsafe_allow_html=True,
            )

    with tabs[0]:
        _print(logs[:200])
    with tabs[1]:
        # Errors tab: real errors only (network, parse, abandon). Criteria
        # rejections are NOT errors — they're routed to Verification.
        err_logs = [
            e for e in _filter(("error",))
            if not e.get("msg", "").startswith("reject")
        ]
        _print(err_logs)
    with tabs[2]:
        block_logs = (
            _filter(("warn",), substr="block")
            + _filter(("warn",), substr="abandon")
            + _filter(("warn",), substr="robots")
        )
        _print(block_logs)
    with tabs[3]:
        verif_logs = (
            _filter(("info",), substr="confirm")
            + _filter(("warn",), substr="probable")
            + _filter(("warn",), substr="reject [criteria]")
        )
        # Sort newest first within the merged set.
        verif_logs.sort(key=lambda e: e.get("ts", ""), reverse=True)
        _print(verif_logs)


def render_diagnostics() -> None:
    """Self-check panel — surfaces install issues before the user clicks Start."""
    st.markdown("**Environment**")
    rows = [
        ("Playwright (Python pkg)", PLAYWRIGHT_AVAILABLE),
        ("playwright-stealth", STEALTH_AVAILABLE),
        ("`config.yaml`", CONFIG_PATH.exists()),
        ("`selectors.yaml`", SELECTORS_PATH.exists()),
    ]
    for label, ok in rows:
        st.markdown(f"- {'✅' if ok else '❌'} {label}")
    if not PLAYWRIGHT_AVAILABLE:
        st.code("pip install -r requirements.txt\nplaywright install chromium", language="bash")
    elif not STEALTH_AVAILABLE:
        st.caption("Stealth is optional — the crawler still works without it.")


# ----------------------------------------------------------- live fragment
# Auto-refresh the live panels every 2 s. Controls stay outside the fragment
# so they remain responsive.
@st.fragment(run_every="2s")
def live_panels() -> None:
    config, _, db, state, _ = get_runtime()
    queue_total = len(build_queue(config))

    status = render_status_banner(state, queue_total)

    st.subheader("Progress")
    render_progress(state, queue_total)

    st.subheader("Results")
    render_results(db, config)

    st.subheader("Pipeline Summary")
    render_pipeline_stats(state)

    st.subheader("Live Logs")
    render_logs(state)

    # Stop the auto-refresh once we settle into a non-running state — saves
    # cycles when the page is just sitting there.
    if status not in {"running", "paused"}:
        return


# -------------------------------------------------------------------- main
def main() -> None:
    st.set_page_config(page_title="Scholarship Scout Pro", page_icon="🎓", layout="wide")
    st.title("🎓 Scholarship Scout Pro")
    st.caption("Fully-funded Master's scholarships for Bangladeshi CSE students — Europe & Australia/NZ")

    config, selectors, db, state, verifier = get_runtime()

    # ---------------------------------------------------- sidebar / controls
    with st.sidebar:
        st.header("Control Panel")
        c1, c2 = st.columns(2)
        if c1.button("▶ Start", use_container_width=True, type="primary"):
            start_crawler(resume=False)
            st.rerun()
        if c2.button("⏸ Pause", use_container_width=True):
            request_pause(state)

        if st.button("▶ Resume", use_container_width=True):
            start_crawler(resume=True)
            st.rerun()

        st.divider()
        st.subheader("Export")
        export_scope = st.radio("Scope", ("Current session", "All sessions"))
        if st.button("📥 Export Excel", use_container_width=True):
            confirmed = db.fetch_scholarships(statuses=["confirmed"])
            probable = db.fetch_scholarships(statuses=["probable"])
            sessions = db.get_sessions()
            out_dir = config["output"]["directory"]
            min_score = int(config.get("verification", {}).get("probable_min_score", 70))
            try:
                if export_scope == "Current session" and state.session_id:
                    path = export_session(
                        rank(confirmed, config), probable, sessions,
                        state.session_id, out_dir, probable_min_score=min_score,
                    )
                else:
                    path = export(
                        rank(confirmed, config), probable, sessions, out_dir,
                        probable_min_score=min_score,
                    )
                st.success(f"Exported: `{path}`")
            except Exception as exc:
                st.error(f"Export failed: {exc}")

        st.divider()
        st.subheader("Session")
        st.code(f"Session ID: {state.session_id or '—'}")
        st.caption(f"Crawler thread: {'alive' if _thread_alive() else 'not running'}")

        with st.expander("Diagnostics", expanded=not PLAYWRIGHT_AVAILABLE):
            render_diagnostics()

    # ---------------------------------------------------------- main panels
    live_panels()


if __name__ == "__main__":
    main()
