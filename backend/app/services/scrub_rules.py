"""
scrub_rules.py — Pre-submission claim scrub rule engine (Phase 2).

Runs 5 rules against each ScrubClaim in a session, comparing against:
  - Production claims/remittances tables
  - ExclusionCode reference table

Rules
-----
FREE_FOLLOWUP (ERROR)
    Same patient (Emirates ID) + same facility + encounter_start within 7 days
    of a prior production claim whose RA status is 'Settled' or 'Paid'.
    UAE DHA: follow-up within 7 days of a settled outpatient visit should not
    be billed as a new consultation.

DUPLICATE (ERROR)
    Same Emirates ID + same facility + at least one matching activity_code on
    the same encounter_start date already exists in production claims.

MISSING_AUTH (WARN)
    Any scrub claim activity with net > 0 and authorization_id IS NULL/empty,
    where the activity_type is 'CPT' or 'HCPCS' (auth-required types).

EXCLUDE_1 (ERROR)
    Any diagnosis_code or activity_code in the staging claim matches an
    ExclusionCode row with exclusion_type = 1 (absolute DHA exclusion).

EXCLUDE_2 (WARN)
    Any diagnosis_code or activity_code in the staging claim matches an
    ExclusionCode row with exclusion_type = 2 (conditional DHA exclusion).
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import ScrubSession, ScrubResult

logger = logging.getLogger(__name__)

# Auth-required activity types for MISSING_AUTH rule
_AUTH_REQUIRED_TYPES = {"CPT", "HCPCS"}


class ScrubRuleEngine:
    """
    Run all rules for every ScrubClaim in a given ScrubSession and write
    ScrubResult rows.  Existing results for the session are deleted first so
    the engine is idempotent (safe to re-run).

    Usage:
        engine = ScrubRuleEngine(db, session_id=42)
        summary = engine.run()
        # summary = {'total': N, 'errors': E, 'warnings': W, 'passed': P}
    """

    def __init__(self, db: Session, session_id: int) -> None:
        self.db = db
        self.session_id = session_id
        self._results: list[ScrubResult] = []

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self) -> dict[str, int]:
        # Load session + all claims eagerly
        session: ScrubSession | None = self.db.get(ScrubSession, self.session_id)
        if session is None:
            raise ValueError(f"ScrubSession {self.session_id} not found")

        facility_id = session.facility_id

        # Delete previous results for this session (idempotent re-run)
        self.db.execute(
            text("DELETE FROM scrub_results WHERE session_id = :sid"),
            {"sid": self.session_id},
        )
        self.db.flush()

        # Load all scrub claims for this session
        claims_rows = self.db.execute(
            text("""
                SELECT sc.id, sc.claim_id, sc.emirates_id, sc.member_id,
                       sc.encounter_start, sc.encounter_type, sc.payer_id
                FROM scrub_claims sc
                WHERE sc.session_id = :sid
            """),
            {"sid": self.session_id},
        ).mappings().all()

        totals = {"total": len(claims_rows), "errors": 0, "warnings": 0, "passed": 0}

        # Map of rule_code → (severity, message, detail) for fired rules
        _ALL_RULES = [
            "FREE_FOLLOWUP",
            "DUPLICATE",
            "MISSING_AUTH",
            "EXCLUDE_1",
            "EXCLUDE_2",
        ]

        for row in claims_rows:
            claim_id_pk = row["id"]

            # Run each rule — collect (rule, severity, message, detail) tuples
            rule_results: dict[str, tuple[str, str, dict]] = {}

            for result_tuple in self._rule_free_followup(row, facility_id):
                rule_results[result_tuple[0]] = (result_tuple[1], result_tuple[2], result_tuple[3])
            for result_tuple in self._rule_duplicate(row, facility_id):
                rule_results[result_tuple[0]] = (result_tuple[1], result_tuple[2], result_tuple[3])
            for result_tuple in self._rule_missing_auth(claim_id_pk):
                rule_results[result_tuple[0]] = (result_tuple[1], result_tuple[2], result_tuple[3])
            for result_tuple in self._rule_exclude(claim_id_pk, exclusion_type=1):
                rule_results[result_tuple[0]] = (result_tuple[1], result_tuple[2], result_tuple[3])
            for result_tuple in self._rule_exclude(claim_id_pk, exclusion_type=2):
                rule_results[result_tuple[0]] = (result_tuple[1], result_tuple[2], result_tuple[3])

            # Write one row per rule — PASS for rules that did not fire
            has_error = False
            has_warn = False
            for rule_code in _ALL_RULES:
                if rule_code in rule_results:
                    sev, msg, detail = rule_results[rule_code]
                    self._add_result(claim_id_pk, rule_code, sev, msg, detail)
                    if sev == "ERROR":
                        has_error = True
                    elif sev == "WARN":
                        has_warn = True
                else:
                    self._add_result(claim_id_pk, rule_code, "PASS", "No issues found", {})

            if has_error:
                totals["errors"] += 1
            elif has_warn:
                totals["warnings"] += 1
            else:
                totals["passed"] += 1

        self.db.add_all(self._results)
        self.db.flush()
        return totals

    # ------------------------------------------------------------------
    # Rule implementations
    # ------------------------------------------------------------------

    def _rule_free_followup(
        self, row: Any, facility_id: int
    ) -> list[tuple[str, str, str, dict]]:
        """
        FREE_FOLLOWUP (ERROR): same Emirates ID + facility, encounter_start
        within 7 days of a prior production claim (any adjudication status).

        UAE DHA rule: a follow-up visit within 7 days of a prior outpatient
        consultation at the same facility should not be billed as a new
        consultation — the follow-up cost is considered included in the
        original visit fee.  The rule applies based on encounter dates alone;
        we do NOT require a settled RA because the prior claim may still be
        pending adjudication when the patient returns.

        Only fires when emirates_id and encounter_start are present.
        """
        emirates_id = row["emirates_id"]
        enc_start = row["encounter_start"]
        if not emirates_id or not enc_start:
            return []

        window_start = enc_start - timedelta(days=7)

        result = self.db.execute(
            text("""
                SELECT c.claim_id, c.encounter_start::text AS enc_start,
                       c.claim_status
                FROM claims c
                WHERE c.facility_id    = :fac
                  AND c.emirates_id    = :eid
                  AND c.encounter_start >= :win_start
                  AND c.encounter_start <  :enc_start
                  AND c.resubmission_key = ''
                ORDER BY c.encounter_start DESC
                LIMIT 5
            """),
            {
                "fac": facility_id,
                "eid": emirates_id,
                "win_start": window_start,
                "enc_start": enc_start,
            },
        ).mappings().all()

        if not result:
            return []

        matched = [
            {"claim_id": r["claim_id"], "encounter_start": r["enc_start"], "status": r["claim_status"]}
            for r in result
        ]
        return [(
            "FREE_FOLLOWUP",
            "ERROR",
            f"Patient {emirates_id} has {len(matched)} prior claim(s) at this facility "
            f"within 7 days before encounter {enc_start}. "
            f"Billing as a new consultation may violate the UAE DHA free follow-up period rule.",
            {"matched_claims": matched},
        )]

    def _rule_duplicate(
        self, row: Any, facility_id: int
    ) -> list[tuple[str, str, str, dict]]:
        """
        DUPLICATE (ERROR): same Emirates ID + facility + overlapping activity_code
        on the same encounter_start already exists in production claims.
        """
        emirates_id = row["emirates_id"]
        enc_start = row["encounter_start"]
        scrub_claim_pk = row["id"]
        if not emirates_id or not enc_start:
            return []

        # Get activity codes from staging
        staging_codes = self.db.execute(
            text("""
                SELECT DISTINCT activity_code FROM scrub_claim_activities
                WHERE scrub_claim_id = :scid
            """),
            {"scid": scrub_claim_pk},
        ).scalars().all()

        if not staging_codes:
            return []

        result = self.db.execute(
            text("""
                SELECT c.claim_id, ca.activity_code, c.encounter_start
                FROM claims c
                JOIN claim_activities ca ON ca.claim_id_fk = c.id
                WHERE c.facility_id    = :fac
                  AND c.emirates_id    = :eid
                  AND c.encounter_start::date = :enc_start
                  AND ca.activity_code IN :codes
                  AND c.resubmission_key = ''
                LIMIT 10
            """),
            {
                "fac": facility_id,
                "eid": emirates_id,
                "enc_start": enc_start,
                "codes": tuple(staging_codes),
            },
        ).mappings().all()

        if not result:
            return []

        matched = list({r["claim_id"] for r in result})
        dup_codes = list({r["activity_code"] for r in result})
        return [(
            "DUPLICATE",
            "ERROR",
            f"Activity code(s) {', '.join(dup_codes)} for patient {emirates_id} on "
            f"{enc_start} already exist in production claim(s): {', '.join(matched)}.",
            {"matched_claims": matched, "duplicate_codes": dup_codes},
        )]

    def _rule_missing_auth(
        self, scrub_claim_pk: int
    ) -> list[tuple[str, str, str, dict]]:
        """
        MISSING_AUTH (WARN): CPT or HCPCS activity with net > 0 and no authorization_id.
        """
        result = self.db.execute(
            text("""
                SELECT activity_code, activity_type, net
                FROM scrub_claim_activities
                WHERE scrub_claim_id = :scid
                  AND activity_type  IN :auth_types
                  AND (authorization_id IS NULL OR authorization_id = '')
                  AND net > 0
            """),
            {
                "scid": scrub_claim_pk,
                "auth_types": tuple(_AUTH_REQUIRED_TYPES),
            },
        ).mappings().all()

        if not result:
            return []

        missing = [
            {"code": r["activity_code"], "type": r["activity_type"], "net": str(r["net"])}
            for r in result
        ]
        return [(
            "MISSING_AUTH",
            "WARN",
            f"{len(missing)} activity item(s) of type CPT/HCPCS have no Prior Authorization ID.",
            {"activities": missing},
        )]

    def _rule_exclude(
        self, scrub_claim_pk: int, exclusion_type: int
    ) -> list[tuple[str, str, str, dict]]:
        """
        EXCLUDE_1 (ERROR) / EXCLUDE_2 (WARN):
        Match staging diagnosis/activity codes against the ExclusionCode table.
        """
        severity = "ERROR" if exclusion_type == 1 else "WARN"
        rule_code = f"EXCLUDE_{exclusion_type}"

        # Check diagnosis codes
        diag_hits = self.db.execute(
            text("""
                SELECT scd.diagnosis_code, ec.description
                FROM scrub_claim_diagnoses scd
                JOIN exclusion_codes ec
                  ON ec.code_type       = 'diagnosis'
                 AND ec.code            = scd.diagnosis_code
                 AND ec.exclusion_type  = :etype
                 AND ec.is_active       = TRUE
                WHERE scd.scrub_claim_id = :scid
            """),
            {"scid": scrub_claim_pk, "etype": exclusion_type},
        ).mappings().all()

        # Check activity codes
        act_hits = self.db.execute(
            text("""
                SELECT sca.activity_code, ec.description
                FROM scrub_claim_activities sca
                JOIN exclusion_codes ec
                  ON ec.code_type       = 'activity'
                 AND ec.code            = sca.activity_code
                 AND ec.exclusion_type  = :etype
                 AND ec.is_active       = TRUE
                WHERE sca.scrub_claim_id = :scid
            """),
            {"scid": scrub_claim_pk, "etype": exclusion_type},
        ).mappings().all()

        if not diag_hits and not act_hits:
            return []

        hits: list[dict] = []
        for r in diag_hits:
            hits.append({"type": "diagnosis", "code": r["diagnosis_code"], "description": r["description"] or ""})
        for r in act_hits:
            hits.append({"type": "activity", "code": r["activity_code"], "description": r["description"] or ""})

        label = "absolute" if exclusion_type == 1 else "conditional"
        return [(
            rule_code,
            severity,
            f"{len(hits)} DHA Exclude {exclusion_type} ({label}) code(s) found: "
            f"{', '.join(h['code'] for h in hits)}.",
            {"exclusion_hits": hits},
        )]

    # ------------------------------------------------------------------
    # Helper
    # ------------------------------------------------------------------

    def _add_result(
        self,
        scrub_claim_id: int,
        rule_code: str,
        severity: str,
        message: str,
        detail: dict,
    ) -> None:
        self._results.append(ScrubResult(
            session_id=self.session_id,
            scrub_claim_id=scrub_claim_id,
            rule_code=rule_code,
            severity=severity,
            message=message,
            detail_json=detail or None,
        ))
