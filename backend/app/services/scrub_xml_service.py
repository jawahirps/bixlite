"""
scrub_xml_service.py — Wrapper that runs the Strawberry XML scrubber on a
ScrubSession's uploaded XML and persists findings to scrub_xml_findings.

Usage:
    svc = ScrubXmlService(db)
    summary = svc.run(session_id=42, xml_bytes=raw_bytes, profile="DUBAI")
    # summary = {"status": "WARN", "blocks": 0, "warnings": 3, "risk_level": "MEDIUM", ...}
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import ScrubXmlFinding, ScrubSession, RawXmlStore
from app.services.claim_scrubber import scrub_xml_bytes

logger = logging.getLogger(__name__)

# Map facility emirate → scrubber profile
# Populated from facility.facility_code or emirate field; falls back to UAE_COMMON.
_EMIRATE_PROFILE: dict[str, str] = {
    "DXB": "DUBAI",
    "DHA": "DUBAI",
    "AUH": "ABU_DHABI",
    "DOH": "ABU_DHABI",
    "SHJ": "NORTHERN_EMIRATES",
    "AJM": "NORTHERN_EMIRATES",
    "RAK": "NORTHERN_EMIRATES",
    "UAQ": "NORTHERN_EMIRATES",
    "FUJ": "NORTHERN_EMIRATES",
    "MOH": "NORTHERN_EMIRATES",
    "RHA": "NORTHERN_EMIRATES",
    "SHA": "ABU_DHABI",
}


def _profile_for_facility(db: Session, facility_id: int) -> str:
    """Derive scrubber profile from facility_code prefix."""
    row = db.execute(
        text("SELECT facility_code FROM facilities WHERE id = :fid"),
        {"fid": facility_id},
    ).mappings().first()
    if not row:
        return "UAE_COMMON"
    # Extract emirate prefix from facility_code (e.g. "DHA-F-0047641" → "DHA")
    code = (row.get("facility_code") or "").strip().upper()
    prefix = code[:3]
    return _EMIRATE_PROFILE.get(prefix, "UAE_COMMON")


class ScrubXmlService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def run(
        self,
        session_id: int,
        xml_bytes: bytes,
        profile: Optional[str] = None,
        enable_nas: bool = False,
    ) -> dict:
        """
        Run the XML scrubber on xml_bytes and persist findings under session_id.

        Returns a summary dict:
            {status, profile, blocks, warnings, risk_level, risk_score, findings_count}
        """
        session: Optional[ScrubSession] = self.db.get(ScrubSession, session_id)
        if session is None:
            raise ValueError(f"ScrubSession {session_id} not found")

        # Resolve profile if not provided
        if not profile:
            profile = _profile_for_facility(self.db, session.facility_id)

        logger.info("ScrubXmlService.run: session=%d profile=%s nas=%s", session_id, profile, enable_nas)

        # Run the scrubber (writes to temp file internally)
        from pathlib import Path
        nas_path = None
        if enable_nas:
            from app.services.claim_scrubber.scrubber import NAS_GUIDELINES_PATH
            nas_path = NAS_GUIDELINES_PATH

        report = scrub_xml_bytes(xml_bytes, profile=profile, nas_guidelines_path=nas_path)

        # Delete old findings for this session (idempotent re-run)
        self.db.execute(
            text("DELETE FROM scrub_xml_findings WHERE session_id = :sid"),
            {"sid": session_id},
        )
        self.db.flush()

        findings = report.get("findings", [])
        summary_data = report.get("summary", {})
        denial_risk = summary_data.get("denial_risk", {})
        session_status = report.get("status", "PASS")
        risk_level = denial_risk.get("level", "LOW")
        risk_score = denial_risk.get("score", 0)

        # Persist each finding — first row gets the session-level risk fields
        rows: list[ScrubXmlFinding] = []
        for i, f in enumerate(findings):
            rows.append(ScrubXmlFinding(
                session_id=session_id,
                session_status=session_status,
                risk_level=risk_level if i == 0 else None,
                risk_score=risk_score if i == 0 else None,
                severity=f.get("severity", "PASS"),
                rule_id=f.get("rule_id", "UNKNOWN"),
                location=f.get("location") or None,
                claim_id=f.get("claim_id") or None,
                activity_id=f.get("activity_id") or None,
                message=f.get("message", ""),
                source=f.get("source") or None,
                profile=profile,
            ))

        # If no findings, write a single PASS sentinel row
        if not rows:
            rows.append(ScrubXmlFinding(
                session_id=session_id,
                session_status="PASS",
                risk_level="LOW",
                risk_score=0,
                severity="PASS",
                rule_id="ALL_CLEAR",
                message="No XML scrubber findings.",
                profile=profile,
            ))

        self.db.add_all(rows)
        self.db.flush()

        result = {
            "status": session_status,
            "profile": profile,
            "blocks": summary_data.get("blocks", 0),
            "warnings": summary_data.get("warnings", 0),
            "claims_checked": summary_data.get("claims", 0),
            "risk_level": risk_level,
            "risk_score": risk_score,
            "findings_count": len(findings),
        }
        logger.info("ScrubXmlService.run: session=%d → %s blocks=%d warns=%d risk=%s",
                    session_id, session_status, result["blocks"], result["warnings"], risk_level)
        return result

    def run_for_raw(
        self,
        raw_xml_id: int,
        xml_bytes: bytes,
        facility_id: int,
        profile: Optional[str] = None,
        enable_nas: bool = False,
    ) -> dict:
        """
        Run the XML scrubber on a RawXmlStore ClaimSubmission record.
        Findings are stored with raw_xml_id set (session_id=NULL).
        Called automatically by the DHPO/Riayati fetch pipeline (Layer 2).

        Returns a summary dict:
            {status, profile, blocks, warnings, risk_level, risk_score, findings_count}
        """
        if not profile:
            profile = _profile_for_facility(self.db, facility_id)

        logger.info("ScrubXmlService.run_for_raw: raw_xml_id=%d profile=%s nas=%s", raw_xml_id, profile, enable_nas)

        nas_path = None
        if enable_nas:
            from app.services.claim_scrubber.scrubber import NAS_GUIDELINES_PATH
            nas_path = NAS_GUIDELINES_PATH

        report = scrub_xml_bytes(xml_bytes, profile=profile, nas_guidelines_path=nas_path)

        # Delete old findings for this raw_xml_id (idempotent re-fetch)
        self.db.execute(
            text("DELETE FROM scrub_xml_findings WHERE raw_xml_id = :rid"),
            {"rid": raw_xml_id},
        )
        self.db.flush()

        findings = report.get("findings", [])
        summary_data = report.get("summary", {})
        denial_risk = summary_data.get("denial_risk", {})
        session_status = report.get("status", "PASS")
        risk_level = denial_risk.get("level", "LOW")
        risk_score = denial_risk.get("score", 0)

        rows: list[ScrubXmlFinding] = []
        for i, f in enumerate(findings):
            rows.append(ScrubXmlFinding(
                session_id=None,
                raw_xml_id=raw_xml_id,
                session_status=session_status,
                risk_level=risk_level if i == 0 else None,
                risk_score=risk_score if i == 0 else None,
                severity=f.get("severity", "PASS"),
                rule_id=f.get("rule_id", "UNKNOWN"),
                location=f.get("location") or None,
                claim_id=f.get("claim_id") or None,
                activity_id=f.get("activity_id") or None,
                message=f.get("message", ""),
                source=f.get("source") or None,
                profile=profile,
            ))

        if not rows:
            rows.append(ScrubXmlFinding(
                session_id=None,
                raw_xml_id=raw_xml_id,
                session_status="PASS",
                risk_level="LOW",
                risk_score=0,
                severity="PASS",
                rule_id="ALL_CLEAR",
                message="No XML scrubber findings.",
                profile=profile,
            ))

        self.db.add_all(rows)
        self.db.flush()

        result = {
            "status": session_status,
            "profile": profile,
            "blocks": summary_data.get("blocks", 0),
            "warnings": summary_data.get("warnings", 0),
            "claims_checked": summary_data.get("claims", 0),
            "risk_level": risk_level,
            "risk_score": risk_score,
            "findings_count": len(findings),
        }
        logger.info("ScrubXmlService.run_for_raw: raw_xml_id=%d → %s blocks=%d warns=%d risk=%s",
                    raw_xml_id, session_status, result["blocks"], result["warnings"], risk_level)
        return result
