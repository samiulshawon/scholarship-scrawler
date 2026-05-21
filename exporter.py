"""Excel export.

Produces a workbook with three sheets:
  Sheet 1 — Confirmed Scholarships  (verification_status == 'confirmed', ranked)
  Sheet 2 — Needs Manual Review     (verification_status == 'probable', score >= 70)
  Sheet 3 — Session Log

Formatting: bold + frozen header row, AutoFilter on all columns, auto column
width, hyperlinked Application URL. Dedup happens on application_url before
writing.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter


COLUMNS = [
    ("name", "Scholarship Name"),
    ("provider", "University / Provider"),
    ("country", "Country"),
    ("region", "Region"),
    ("funding_tier", "Funding Tier"),
    ("stipend_details", "Stipend Details"),
    ("tuition_coverage", "Tuition Coverage"),
    ("travel_allowance", "Travel Allowance"),
    ("housing_support", "Housing Support"),
    ("work_opportunity", "Work Opportunity"),
    ("eligible_fields", "Eligible Fields"),
    ("bangladesh_eligible", "Bangladesh Eligible"),
    ("english_program", "English Program"),
    ("intake", "Intake"),
    ("application_deadline", "Application Deadline"),
    ("application_url", "Application URL"),
    ("source_page", "Source Page"),
    ("verification_status", "Verification Status"),
    ("confidence_score", "Confidence Score"),
    ("session_id", "Session ID"),
    ("last_verified", "Last Verified"),
]

REVIEW_COLUMNS = COLUMNS + [("review_notes", "Review Notes")]

HEADER_FILL = PatternFill("solid", fgColor="FFE5E7EB")
HEADER_FONT = Font(bold=True)


def _dedup(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen, out = set(), []
    for r in records:
        url = (r.get("application_url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(r)
    return out


def _write_sheet(ws, columns: list[tuple[str, str]], rows: list[dict[str, Any]]) -> None:
    headers = [label for _, label in columns]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(vertical="center", wrap_text=True)

    url_col_idx = next((i + 1 for i, (k, _) in enumerate(columns) if k == "application_url"), None)

    for row_idx, record in enumerate(rows, start=2):
        for col_idx, (key, _) in enumerate(columns, start=1):
            value = record.get(key, "")
            if isinstance(value, (list, dict)):
                value = ", ".join(map(str, value)) if isinstance(value, list) else str(value)
            cell = ws.cell(row=row_idx, column=col_idx, value=value)
            if col_idx == url_col_idx and value:
                cell.hyperlink = str(value)
                cell.style = "Hyperlink"

    last_col = get_column_letter(len(columns))
    ws.freeze_panes = "A2"
    if rows:
        ws.auto_filter.ref = f"A1:{last_col}{len(rows) + 1}"

    # Auto column width — capped at 60 to keep the file readable.
    for col_idx, (key, label) in enumerate(columns, start=1):
        max_len = len(label)
        for r in rows:
            v = r.get(key, "")
            if v is None:
                continue
            max_len = max(max_len, min(len(str(v)), 80))
        ws.column_dimensions[get_column_letter(col_idx)].width = min(max(max_len + 2, 12), 60)


def _write_session_log(ws, sessions: list[dict[str, Any]]) -> None:
    headers = ["Session ID", "Start Time", "End Time", "Countries Scanned", "Pages Visited", "Records Found", "Status"]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
    for s in sessions:
        ws.append([
            s.get("session_id", ""),
            s.get("start_time", ""),
            s.get("end_time", ""),
            s.get("countries_scanned", ""),
            s.get("pages_visited", 0),
            s.get("records_found", 0),
            s.get("status", ""),
        ])
    ws.freeze_panes = "A2"


def export(
    confirmed: list[dict[str, Any]],
    probable: list[dict[str, Any]],
    sessions: list[dict[str, Any]],
    out_dir: str | Path,
    filename: str | None = None,
    probable_min_score: int = 70,
) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    confirmed = _dedup(confirmed)
    probable = [r for r in _dedup(probable) if (r.get("confidence_score") or 0) >= probable_min_score]

    wb = Workbook()
    ws1 = wb.active
    ws1.title = "Confirmed"
    _write_sheet(ws1, COLUMNS, confirmed)

    ws2 = wb.create_sheet("Needs Manual Review")
    _write_sheet(ws2, REVIEW_COLUMNS, probable)

    ws3 = wb.create_sheet("Session Log")
    _write_session_log(ws3, sessions)

    if not filename:
        filename = f"scholarships_all_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.xlsx"
    path = out_dir / filename
    wb.save(path)
    return path


def export_session(
    confirmed: list[dict[str, Any]],
    probable: list[dict[str, Any]],
    sessions: list[dict[str, Any]],
    session_id: str,
    out_dir: str | Path,
    probable_min_score: int = 70,
) -> Path:
    """Per-session export, used on every pause."""
    return export(
        confirmed=[r for r in confirmed if r.get("session_id") == session_id],
        probable=[r for r in probable if r.get("session_id") == session_id],
        sessions=[s for s in sessions if s.get("session_id") == session_id],
        out_dir=out_dir,
        filename=f"scholarships_session_{session_id}.xlsx",
        probable_min_score=probable_min_score,
    )


def to_dataframe(records: list[dict[str, Any]]) -> pd.DataFrame:
    """Convenience for the Streamlit UI — returns a DataFrame with display labels."""
    df = pd.DataFrame(records)
    rename = {k: v for k, v in COLUMNS if k in df.columns}
    return df.rename(columns=rename)
