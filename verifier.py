"""Eligibility verification engine.

Runs every extracted record through five hard checks and produces:
    verification_status: "confirmed" | "probable" | "rejected"
    confidence_score:    0..100

Also normalises a few free-text fields (deadline, intake, funding tier).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from dateutil import parser as dateparser


# ---------------------------------------------------------------------- helpers
INTAKE_PATTERNS: dict[str, list[str]] = {
    "Fall 2026": ["fall 2026", "autumn 2026", "winter 2026/27", "ws 2026", "wintersemester 2026"],
    "Spring 2027": ["spring 2027", "summer 2027", "ss 2027", "sommersemester 2027"],
    "Semester 1 2027": ["semester 1 2027", "sem 1 2027", "s1 2027", "march 2027 intake"],
    "Semester 2 2027": ["semester 2 2027", "sem 2 2027", "s2 2027", "july 2027 intake"],
}

TIER_HINTS = {
    "Tier 1": [
        "full tuition", "fully funded", "monthly stipend", "living allowance",
        "monthly allowance", "subsistence", "€800", "€1000", "€1200", "1,200/month",
    ],
    "Tier 2": ["tuition fee waiver", "one-time grant", "lump sum", "partial stipend"],
    "Tier 3": ["work permit", "right to work", "part-time work allowed"],
    "Tier 4": ["tuition only", "tuition fee covered", "fee waiver"],
}

DISCARD_HINTS = [
    "tuition discount", "tuition reduction", "loan", "interest-free loan",
    "merit reduction", "10% off", "20% off",
]


def _norm(text: str | None) -> str:
    return (text or "").lower()


def normalise_deadline(raw: str | None) -> str:
    """Return DD-MM-YYYY, or 'Rolling' / 'TBA' / '' when the date can't be parsed."""
    if not raw:
        return ""
    low = raw.strip().lower()
    if "rolling" in low:
        return "Rolling"
    if "tba" in low or "to be announced" in low or "announced soon" in low:
        return "TBA"
    try:
        dt = dateparser.parse(raw, dayfirst=True, fuzzy=True, default=datetime(date.today().year, 1, 1))
        return dt.strftime("%d-%m-%Y")
    except (ValueError, OverflowError, TypeError):
        return ""


def detect_intake(text: str) -> str | None:
    low = _norm(text)
    for label, patterns in INTAKE_PATTERNS.items():
        if any(p in low for p in patterns):
            return label
    return None


def detect_tier(text: str) -> str | None:
    low = _norm(text)
    if any(d in low for d in DISCARD_HINTS):
        return None
    # Tier 1 takes priority — most specific markers first.
    for tier in ("Tier 1", "Tier 2", "Tier 3", "Tier 4"):
        if any(h in low for h in TIER_HINTS[tier]):
            return tier
    if "fully funded" in low:
        return "Tier 1"
    if "full tuition" in low or "tuition covered" in low:
        return "Tier 4"
    return None


# ----------------------------------------------------------------------- engine
@dataclass
class VerificationResult:
    status: str                # confirmed | probable | rejected
    score: int                 # 0..100
    notes: list[str]           # human-readable reasons / uncertainties
    normalised: dict[str, Any] # fields the caller should overwrite

    def merge_into(self, record: dict[str, Any]) -> dict[str, Any]:
        record = {**record, **self.normalised}
        record["verification_status"] = self.status
        record["confidence_score"] = self.score
        record["review_notes"] = "; ".join(self.notes)
        return record


class Verifier:
    """Five hard checks + scoring. Pure & deterministic; safe to call from anywhere."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.bd_phrases: list[str] = [p.lower() for p in config.get("bangladesh_accepted_phrases", [])]
        self.ai_keywords: list[str] = [k.lower() for k in config.get("field_keywords", {}).get("ai_ml", [])]
        self.cs_keywords: list[str] = [k.lower() for k in config.get("field_keywords", {}).get("cse_core", [])]
        self.allowed_intakes: set[str] = set(
            config.get("targets", {}).get("europe", {}).get("intakes", [])
            + config.get("targets", {}).get("australia_nz", {}).get("intakes", [])
        )
        self.thresholds = config.get("verification", {})
        self.confirmed_min = int(self.thresholds.get("confirmed_min_score", 85))
        self.probable_min = int(self.thresholds.get("probable_min_score", 70))

    # ----------------------------------------------------- individual checks
    def _check_bangladesh(self, text: str, eligible_field_value: str) -> tuple[bool, int, str]:
        low = _norm(text + " " + eligible_field_value)
        if "bangladesh" in low:
            return True, 25, "Bangladesh listed explicitly"
        for phrase in self.bd_phrases:
            if phrase in low:
                return True, 20, f"Open category matched: '{phrase}'"
        return False, 0, "Bangladesh / open category not found"

    def _check_field(self, text: str) -> tuple[bool, int, str]:
        low = _norm(text)
        if any(k in low for k in self.ai_keywords):
            return True, 25, "AI/ML keyword match"
        if any(k in low for k in self.cs_keywords):
            return True, 18, "Core CSE keyword match"
        return False, 0, "No CS/AI keyword match"

    def _check_intake(self, intake: str | None, text: str) -> tuple[bool, int, str]:
        detected = intake or detect_intake(text)
        if detected and detected in self.allowed_intakes:
            return True, 15, f"Intake matches: {detected}"
        if detected:
            return False, 0, f"Intake '{detected}' outside target window"
        return False, 5, "Intake not detected (manual check needed)"

    def _check_tier(self, tier: str | None, text: str) -> tuple[bool, int, str]:
        detected = tier or detect_tier(text)
        if detected in {"Tier 1", "Tier 2", "Tier 3", "Tier 4"}:
            score = {"Tier 1": 25, "Tier 2": 20, "Tier 3": 17, "Tier 4": 13}[detected]
            return True, score, f"Funding classified as {detected}"
        return False, 0, "Funding tier could not be classified"

    def _check_english(self, english: str | None, text: str) -> tuple[bool, int, str]:
        low = _norm((english or "") + " " + text)
        if "taught in english" in low or "english-taught" in low or "language: english" in low:
            return True, 10, "Programme explicitly in English"
        if english and english.lower().startswith("yes"):
            return True, 10, "English flag set"
        if "english" in low:
            return True, 6, "English mentioned (uncertain)"
        return False, 0, "English instruction not confirmed"

    # --------------------------------------------------------------- public
    def verify(self, record: dict[str, Any]) -> VerificationResult:
        text = " ".join(
            str(record.get(k, "") or "")
            for k in (
                "name", "provider", "stipend_details", "eligible_fields",
                "english_program", "tuition_coverage", "intake",
                "application_deadline", "raw_payload", "description",
            )
        )

        notes: list[str] = []
        score = 0

        bd_ok, bd_pts, bd_note = self._check_bangladesh(text, str(record.get("eligible_fields", "")))
        notes.append(bd_note); score += bd_pts

        fld_ok, fld_pts, fld_note = self._check_field(text)
        notes.append(fld_note); score += fld_pts

        intake_ok, intake_pts, intake_note = self._check_intake(record.get("intake"), text)
        notes.append(intake_note); score += intake_pts

        tier_ok, tier_pts, tier_note = self._check_tier(record.get("funding_tier"), text)
        notes.append(tier_note); score += tier_pts

        eng_ok, eng_pts, eng_note = self._check_english(record.get("english_program"), text)
        notes.append(eng_note); score += eng_pts

        normalised = {
            "application_deadline": normalise_deadline(record.get("application_deadline")),
            "intake": record.get("intake") or detect_intake(text) or "",
            "funding_tier": record.get("funding_tier") or detect_tier(text) or "",
            "last_verified": datetime.utcnow().isoformat(timespec="seconds"),
        }

        # Hard gates: any of these failing forces a downgrade.
        hard_gates = [bd_ok, fld_ok, tier_ok]
        if not all(hard_gates):
            score = min(score, self.probable_min - 1 if not all([bd_ok, fld_ok]) else score)

        if score >= self.confirmed_min and all([bd_ok, fld_ok, tier_ok, eng_ok]):
            status = "confirmed"
        elif score >= self.probable_min and bd_ok and fld_ok:
            status = "probable"
        else:
            status = "rejected"

        return VerificationResult(status=status, score=int(min(score, 100)), notes=notes, normalised=normalised)
