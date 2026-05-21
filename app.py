"""Streamlit dashboard for Scholarship Scout Pro.

The crawler is async and long-running; Streamlit reruns its script on every
interaction. We bridge the two by running ``asyncio.run`` inside a daemon
thread and using ``SessionState`` (thread-safe, JSON-backed) as the shared
state channel between the crawler thread and the UI.
"""
from __future__ import annotations

import asyncio
import threading
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
from scraper_engine import ScraperEngine, build_queue
from session_manager import SessionState, make_session_id
from verifier import Verifier


# --------------------------------------------------------------------- config
CONFIG_PATH = Path("config.yaml")
SELECTORS_PATH = Path("selectors.yaml")


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


# ---------------------------------------------------------- crawler thread
def _crawler_thread(config, selectors, db, state, verifier) -> None:
    """Runs the async crawler inside a fresh event loop on a daemon thread."""
    async def _runner() -> None:
        engine = ScraperEngine(config, selectors, db, state, verifier)
        try:
            await engine.run()
        finally:
            snap = state.snapshot()
            db.end_session(
                session_id=snap.get("session_id", "unknown"),
                end_time=datetime.utcnow().isoformat(timespec="seconds"),
                countries=engine.stats.countries,
                pages=snap.get("pages_visited", 0),
                records=snap.get("records_found", 0),
                status="paused" if state.is_paused() else "completed",
            )

    asyncio.run(_runner())


def start_crawler(*, resume: bool) -> None:
    config, selectors, db, state, verifier = get_runtime()

    queue = build_queue(config)
    if not queue:
        st.warning("No seed URLs configured. Edit `config.yaml` and add URLs under `seeds:`.")
        return

    if resume and state.session_id:
        state.resume(queue)
        db.start_session(state.session_id, datetime.utcnow().isoformat(timespec="seconds"))
    else:
        sid = make_session_id()
        db.start_session(sid, datetime.utcnow().isoformat(timespec="seconds"))
        state.init_session(sid, queue)

    thread = threading.Thread(
        target=_crawler_thread,
        args=(config, selectors, db, state, verifier),
        daemon=True,
        name=f"scraper-{state.session_id}",
    )
    thread.start()
    st.session_state["crawler_thread_alive"] = True


def request_pause(state: SessionState) -> None:
    state.request_pause()
    st.toast("Pause requested — will stop after the current page", icon="⏸")


# ----------------------------------------------------------------- UI bits
TIER_LABELS = {
    "Tier 1": "🥇 Tier 1 — Platinum (Full + Full Stipend)",
    "Tier 2": "🥈 Tier 2 — Gold (Full + Partial Stipend)",
    "Tier 3": "🥉 Tier 3 — Silver (Full + Work Rights)",
    "Tier 4": "🏅 Tier 4 — Bronze (Full Tuition Only)",
}
LEVEL_COLORS = {"info": "#16a34a", "warn": "#ca8a04", "error": "#dc2626"}


def render_progress(state: SessionState, queue_total: int) -> None:
    snap = state.snapshot()
    cols = st.columns(4)
    cols[0].metric("Region/Country", snap.get("current_country") or "—")
    cols[1].metric("Phase", snap.get("current_phase") or "—")
    cols[2].metric("Pages visited", snap.get("pages_visited", 0))
    cols[3].metric("Records found", snap.get("records_found", 0))

    visited = len(snap.get("visited", []))
    total = max(queue_total, visited, 1)
    st.progress(min(visited / total, 1.0), text=f"{visited} / {total} URLs")


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
        _print(_filter(("error",)))
    with tabs[2]:
        _print(_filter(("warn",), substr="block") + _filter(("warn",), substr="skip") + _filter(("warn",), substr="abandon"))
    with tabs[3]:
        _print(_filter(("info", "warn"), substr="confirm") + _filter(("warn",), substr="probable"))


# -------------------------------------------------------------------- main
def main() -> None:
    st.set_page_config(page_title="Scholarship Scout Pro", page_icon="🎓", layout="wide")
    st.title("🎓 Scholarship Scout Pro")
    st.caption("Fully-funded Master's scholarships for Bangladeshi CSE students — Europe & Australia/NZ")

    config, selectors, db, state, verifier = get_runtime()
    queue_total = len(build_queue(config))

    # ----------------------------------------------------- sidebar / controls
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
        export_scope = st.radio(
            "Scope",
            ("Current session", "All sessions"),
            horizontal=False,
        )
        if st.button("📥 Export Excel", use_container_width=True):
            confirmed = db.fetch_scholarships(statuses=["confirmed"])
            probable = db.fetch_scholarships(statuses=["probable"])
            sessions = db.get_sessions()
            out_dir = config["output"]["directory"]
            min_score = int(config.get("verification", {}).get("probable_min_score", 70))
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

        st.divider()
        st.subheader("Session")
        st.code(f"Session ID: {state.session_id or '—'}")
        if st.toggle("Auto-refresh (3s)", value=False):
            # Streamlit's official auto-refresh idiom: brief sleep + rerun.
            import time
            time.sleep(3)
            st.rerun()

    # ----------------------------------------------------- main panels
    st.subheader("Progress")
    render_progress(state, queue_total)

    st.subheader("Results")
    render_results(db, config)

    st.subheader("Live Logs")
    render_logs(state)


if __name__ == "__main__":
    main()
