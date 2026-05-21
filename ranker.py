"""Ranking logic.

Within each tier, sort by:
  1. Funding tier (Tier 1 -> Tier 4)
  2. AI / ML field relevance
  3. General CSE relevance
  4. Intake match accuracy
  5. Deadline urgency (soonest first)
  6. Work opportunity (yes > unknown > no)
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

import pandas as pd


TIER_ORDER = {"Tier 1": 1, "Tier 2": 2, "Tier 3": 3, "Tier 4": 4}
WORK_ORDER = {"yes": 0, "unknown": 1, "no": 2}


def _ai_score(eligible_fields: str, ai_keywords: list[str]) -> int:
    low = (eligible_fields or "").lower()
    return sum(1 for kw in ai_keywords if kw in low)


def _cse_score(eligible_fields: str, cse_keywords: list[str]) -> int:
    low = (eligible_fields or "").lower()
    return sum(1 for kw in cse_keywords if kw in low)


def _deadline_key(deadline: str) -> int:
    """Smaller = more urgent. Rolling/TBA/missing sort to the end."""
    if not deadline or deadline in {"Rolling", "TBA"}:
        return 10**9
    try:
        dt = datetime.strptime(deadline, "%d-%m-%Y").date()
        delta = (dt - date.today()).days
        return delta if delta >= 0 else 10**9 - 1   # past deadlines: deprioritise
    except ValueError:
        return 10**9


def _work_key(work: str | None) -> int:
    return WORK_ORDER.get((work or "unknown").lower(), 1)


def rank(records: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    if not records:
        return []
    ai_keywords = [k.lower() for k in config.get("field_keywords", {}).get("ai_ml", [])]
    cse_keywords = [k.lower() for k in config.get("field_keywords", {}).get("cse_core", [])]

    df = pd.DataFrame(records)
    df["_tier"] = df.get("funding_tier", "").map(lambda t: TIER_ORDER.get(t, 99))
    df["_ai"] = -df.get("eligible_fields", "").apply(lambda s: _ai_score(str(s or ""), ai_keywords))
    df["_cse"] = -df.get("eligible_fields", "").apply(lambda s: _cse_score(str(s or ""), cse_keywords))
    df["_intake_match"] = df.get("intake", "").apply(lambda s: 0 if s else 1)
    df["_deadline"] = df.get("application_deadline", "").apply(_deadline_key)
    df["_work"] = df.get("work_opportunity", "").apply(_work_key)

    df = df.sort_values(
        by=["_tier", "_ai", "_cse", "_intake_match", "_deadline", "_work"],
        ascending=True,
        kind="mergesort",
    )
    df = df.drop(columns=[c for c in df.columns if c.startswith("_")])
    return df.to_dict(orient="records")
