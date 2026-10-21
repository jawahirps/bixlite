"""
Ghaf App — RCM Platform — SQLAlchemy ORM Models

Base claims/remittance/recon models adapted from the existing DHPO ingestion
pipeline, extended with:
  - User accounts + LoginLog (login tracking)
  - ClinicalProfile (per-patient clinical profile setup)
"""
from __future__ import annotations
from datetime import datetime, date
from decimal import Decimal
from typing import Optional, List

from sqlalchemy import (
    BigInteger, Boolean, Column, Date, DateTime, ForeignKey,
    Integer, LargeBinary, Numeric, SmallInteger, String, Text, UniqueConstraint,
    CheckConstraint, Index, text
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP

# SQLAlchemy's postgresql dialect doesn't export a TIMESTAMPTZ name directly;
# this is just TIMESTAMP WITH TIME ZONE.
TIMESTAMPTZ = TIMESTAMP(timezone=True)
from sqlalchemy.orm import DeclarativeBase, relationship, Mapped, mapped_column
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Facility
# ---------------------------------------------------------------------------
class Facility(Base):
    __tablename__ = "facilities"

    id               : Mapped[int]           = mapped_column(Integer, primary_key=True)
    facility_code    : Mapped[str]           = mapped_column(String(20), nullable=False, unique=True)
    facility_name    : Mapped[str]           = mapped_column(String(200), nullable=False)
    license_id       : Mapped[str]           = mapped_column(String(50), nullable=False)
    eclaimlink_username: Mapped[str]         = mapped_column(String(100), nullable=False)
    credential_key   : Mapped[str]           = mapped_column(String(100), nullable=False)
    dhpo_provider_id : Mapped[Optional[str]] = mapped_column(String(50))
    # Riayati (MOH Post Office) credentials — nullable, only set for MOH-connected facilities
    riayati_username : Mapped[Optional[str]] = mapped_column(String(100))
    riayati_password : Mapped[Optional[str]] = mapped_column(String(100))
    is_active        : Mapped[bool]          = mapped_column(Boolean, nullable=False, default=True)
    created_at       : Mapped[datetime]      = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at       : Mapped[datetime]      = mapped_column(TIMESTAMPTZ, server_default=func.now(), onupdate=func.now())

    claims           : Mapped[List["Claim"]]          = relationship(back_populates="facility")
    remittances      : Mapped[List["Remittance"]]     = relationship(back_populates="facility")
    recon_results    : Mapped[List["ReconResult"]]    = relationship(back_populates="facility")
    fetch_logs       : Mapped[List["FetchLog"]]       = relationship(back_populates="facility")
    raw_xml_store    : Mapped[List["RawXmlStore"]]    = relationship(back_populates="facility")
    users            : Mapped[List["User"]]           = relationship(back_populates="facility")
    clinical_profiles: Mapped[List["ClinicalProfile"]] = relationship(back_populates="facility")
    daily_receipts   : Mapped[List["DailyReceipt"]]  = relationship(back_populates="facility")
    payer_mappings   : Mapped[List["ReceiptPayerMapping"]] = relationship(back_populates="facility")

    riayati_accounts : Mapped[List["FacilityRiayatiAccount"]] = relationship(
        back_populates="facility", cascade="all, delete-orphan", order_by="FacilityRiayatiAccount.id")

    def __repr__(self) -> str:
        return f"<Facility {self.facility_code} — {self.facility_name}>"


class FacilityRiayatiAccount(Base):
    """
    Extra Riayati Post Office account for a facility (the primary one is facilities.riayati_username /
    riayati_password). Used when a facility moves regulator but keeps one claim history — e.g. Sabah Al Noor,
    RHA licence until Aug 2026, SHA licence from 1 Sep 2026. The fetch pulls every active account into the same
    facility, so RAs and resubmissions match their claims by claim ID whichever account they arrive on.
    """
    __tablename__ = "facility_riayati_accounts"

    id               : Mapped[int]           = mapped_column(Integer, primary_key=True)
    facility_id      : Mapped[int]           = mapped_column(Integer, ForeignKey("facilities.id", ondelete="CASCADE"), nullable=False)
    label            : Mapped[str]           = mapped_column(String(50), nullable=False)        # e.g. "SHA"
    username         : Mapped[str]           = mapped_column(String(100), nullable=False)
    password         : Mapped[str]           = mapped_column(String(100), nullable=False)
    license_id       : Mapped[Optional[str]] = mapped_column(String(50))
    active_from      : Mapped[Optional[date]]= mapped_column(Date)      # first visit date on this licence (info)
    is_active        : Mapped[bool]          = mapped_column(Boolean, nullable=False, default=True)
    created_at       : Mapped[datetime]      = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at       : Mapped[datetime]      = mapped_column(TIMESTAMPTZ, server_default=func.now(), onupdate=func.now())

    facility         : Mapped["Facility"]    = relationship(back_populates="riayati_accounts")

    __table_args__ = (
        Index("idx_riayati_accounts_facility", "facility_id"),
    )


# ---------------------------------------------------------------------------
# User (auth + login tracking)
# ---------------------------------------------------------------------------
class User(Base):
    __tablename__ = "users"

    id               : Mapped[int]           = mapped_column(Integer, primary_key=True)
    facility_id      : Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("facilities.id"))
    email            : Mapped[str]           = mapped_column(String(255), nullable=False, unique=True)
    full_name        : Mapped[str]           = mapped_column(String(200), nullable=False)
    hashed_password  : Mapped[str]           = mapped_column(String(255), nullable=False)
    role             : Mapped[str]           = mapped_column(String(30), nullable=False, default="staff")
    is_active        : Mapped[bool]          = mapped_column(Boolean, nullable=False, default=True)
    last_login_at    : Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ)
    created_at       : Mapped[datetime]      = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at       : Mapped[datetime]      = mapped_column(TIMESTAMPTZ, server_default=func.now(), onupdate=func.now())

    facility         : Mapped[Optional["Facility"]] = relationship(back_populates="users")
    login_logs       : Mapped[List["LoginLog"]]     = relationship(back_populates="user")

    __table_args__ = (
        CheckConstraint(
            "role IN ('admin','billing_manager','clinician','staff')",
            name="ck_user_role",
        ),
    )

    def __repr__(self) -> str:
        return f"<User {self.email} ({self.role})>"


# ---------------------------------------------------------------------------
# LoginLog (login tracking / audit trail)
# ---------------------------------------------------------------------------
class LoginLog(Base):
    __tablename__ = "login_logs"

    id               : Mapped[int]           = mapped_column(BigInteger, primary_key=True)
    user_id          : Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("users.id"))
    email_attempted  : Mapped[str]           = mapped_column(String(255), nullable=False)
    success          : Mapped[bool]          = mapped_column(Boolean, nullable=False)
    failure_reason   : Mapped[Optional[str]] = mapped_column(String(200))
    ip_address       : Mapped[Optional[str]] = mapped_column(String(64))
    user_agent       : Mapped[Optional[str]] = mapped_column(String(500))
    created_at       : Mapped[datetime]      = mapped_column(TIMESTAMPTZ, server_default=func.now())

    user             : Mapped[Optional["User"]] = relationship(back_populates="login_logs")

    __table_args__ = (
        Index("idx_login_logs_user", "user_id"),
        Index("idx_login_logs_created_at", "created_at"),
    )

    def __repr__(self) -> str:
        return f"<LoginLog {self.email_attempted} success={self.success} @ {self.created_at}>"


# ---------------------------------------------------------------------------
# ClinicalProfile (clinical profile setup per patient)
# ---------------------------------------------------------------------------
class ClinicalProfile(Base):
    __tablename__ = "clinical_profiles"

    id                   : Mapped[int]           = mapped_column(Integer, primary_key=True)
    facility_id          : Mapped[int]           = mapped_column(Integer, ForeignKey("facilities.id"), nullable=False)

    member_id            : Mapped[str]           = mapped_column(String(100), nullable=False)
    emirates_id          : Mapped[Optional[str]] = mapped_column(String(50))
    full_name            : Mapped[str]           = mapped_column(String(200), nullable=False)
    date_of_birth        : Mapped[Optional[date]]= mapped_column(Date)
    gender               : Mapped[Optional[str]] = mapped_column(String(10))
    nationality          : Mapped[Optional[str]] = mapped_column(String(60))

    payer_id             : Mapped[Optional[str]] = mapped_column(String(50))
    policy_number        : Mapped[Optional[str]] = mapped_column(String(100))
    plan_type            : Mapped[Optional[str]] = mapped_column(String(50))

    primary_clinician_id : Mapped[Optional[str]] = mapped_column(String(50))
    blood_type           : Mapped[Optional[str]] = mapped_column(String(5))
    height_cm            : Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 1))
    weight_kg            : Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 1))
    allergies            : Mapped[Optional[str]] = mapped_column(Text)
    chronic_conditions   : Mapped[Optional[str]] = mapped_column(Text)
    current_medications  : Mapped[Optional[str]] = mapped_column(Text)
    notes                : Mapped[Optional[str]] = mapped_column(Text)

    is_active            : Mapped[bool]          = mapped_column(Boolean, nullable=False, default=True)
    created_at           : Mapped[datetime]      = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at           : Mapped[datetime]      = mapped_column(TIMESTAMPTZ, server_default=func.now(), onupdate=func.now())

    facility             : Mapped["Facility"]    = relationship(back_populates="clinical_profiles")

    __table_args__ = (
        UniqueConstraint("facility_id", "member_id", name="uq_clinical_profile_facility_member"),
        CheckConstraint("gender IN ('Male','Female','Other') OR gender IS NULL", name="ck_clinical_profile_gender"),
        Index("idx_clinical_profiles_member", "member_id"),
        Index("idx_clinical_profiles_emirates_id", "emirates_id"),
    )

    def __repr__(self) -> str:
        return f"<ClinicalProfile {self.member_id} — {self.full_name}>"


# ---------------------------------------------------------------------------
# RawXmlStore (Production Optimized)
# ---------------------------------------------------------------------------
class RawXmlStore(Base):
    __tablename__ = "raw_xml_store"

    id               : Mapped[int]           = mapped_column(BigInteger, primary_key=True)
    facility_id      : Mapped[int]           = mapped_column(Integer, ForeignKey("facilities.id"), nullable=False)
    xml_type         : Mapped[str]           = mapped_column(String(30), nullable=False)
    source_filename  : Mapped[Optional[str]] = mapped_column(String(500))
    submission_date  : Mapped[Optional[date]]= mapped_column(Date)
    fetched_at       : Mapped[datetime]      = mapped_column(TIMESTAMPTZ, server_default=func.now())
    file_size_bytes  : Mapped[Optional[int]] = mapped_column(Integer)
    xml_content      : Mapped[str]           = mapped_column(Text, nullable=False) # Handled by PG TOAST out-of-line
    parse_status     : Mapped[str]           = mapped_column(String(20), nullable=False, default="pending")
    parse_error      : Mapped[Optional[str]] = mapped_column(Text)
    records_parsed   : Mapped[Optional[int]] = mapped_column(Integer)
    parsed_at        : Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ)

    facility         : Mapped["Facility"]    = relationship(back_populates="raw_xml_store")

    __table_args__ = (
        CheckConstraint(
            "xml_type IN ('ClaimSubmission','RemittanceAdvice','RiayatiClaimSubmission','RiayatiRemittanceAdvice')",
            name="ck_raw_xml_type",
        ),
        CheckConstraint("parse_status IN ('pending','parsed','failed','quarantined')", name="ck_parse_status"),
        Index("idx_raw_xml_facility_type", "facility_id", "xml_type"),
        Index("idx_raw_xml_parse_status", "parse_status"),
    )


# ---------------------------------------------------------------------------
# Claim (Adjusted for Hybrid Ingestion Channels)
# ---------------------------------------------------------------------------
class Claim(Base):
    __tablename__ = "claims"

    id               : Mapped[int]           = mapped_column(BigInteger, primary_key=True)
    facility_id      : Mapped[int]           = mapped_column(Integer, ForeignKey("facilities.id"), nullable=False)

    # Made nullable to allow historical imports from Excel without breaking constraints
    raw_xml_id       : Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("raw_xml_store.id"), nullable=True)

    # Lineage origin tracker to segment analytical reporting
    source_channel   : Mapped[str]           = mapped_column(String(20), nullable=False, default="API")

    # DHPO XML identifiers
    claim_id         : Mapped[str]           = mapped_column(String(100), nullable=False)
    claim_id_payer   : Mapped[Optional[str]] = mapped_column(String(100))

    # Encounter header
    encounter_id     : Mapped[str]           = mapped_column(String(100), nullable=False)
    encounter_type   : Mapped[Optional[str]] = mapped_column(String(50))
    encounter_start  : Mapped[datetime]      = mapped_column(TIMESTAMPTZ, nullable=False)
    encounter_end    : Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ)
    start_type       : Mapped[Optional[str]] = mapped_column(String(50))
    end_type         : Mapped[Optional[str]] = mapped_column(String(50))
    transfer_source  : Mapped[Optional[str]] = mapped_column(String(50))

    # Patient identifiers
    member_id        : Mapped[str]           = mapped_column(String(100), nullable=False)
    patient_id       : Mapped[Optional[str]] = mapped_column(String(100))   # Encounter/PatientID from claim XML
    emirates_id      : Mapped[Optional[str]] = mapped_column(String(50))
    emirates_id_type : Mapped[Optional[str]] = mapped_column(String(20))

    # Payer & clinician
    payer_id         : Mapped[str]           = mapped_column(String(50), nullable=False)
    payer_name       : Mapped[Optional[str]] = mapped_column(String(150))
    clinician_id     : Mapped[str]           = mapped_column(String(50), nullable=False)
    clinician_name   : Mapped[Optional[str]] = mapped_column(String(150))
    department       : Mapped[Optional[str]] = mapped_column(String(100))

    # eClaimLink / DHPO receiver (TPA that received the submission)
    receiver_id      : Mapped[Optional[str]] = mapped_column(String(50))
    receiver_name    : Mapped[Optional[str]] = mapped_column(String(150))

    # Financial totals
    gross_amount     : Mapped[Decimal]       = mapped_column(Numeric(12, 2), nullable=False, default=0)
    net_amount       : Mapped[Decimal]       = mapped_column(Numeric(12, 2), nullable=False, default=0)
    patient_share_total: Mapped[Decimal]     = mapped_column(Numeric(12, 2), nullable=False, default=0)

    # Submission metadata
    submission_date  : Mapped[date]          = mapped_column(Date, nullable=False)
    claim_status     : Mapped[str]           = mapped_column(String(30), nullable=False, default="submitted")
    resubmission_count: Mapped[int]          = mapped_column(SmallInteger, nullable=False, default=0)
    # Resubmission type from <Resubmission><Type> in claim XML
    # Values: NULL = initial, 'Correction', 'InternalComplaint', 'Reconciliation'
    resubmission_type: Mapped[Optional[str]] = mapped_column(String(50))
    # resubmission_key = '' for initial, resubmission_type value for resubmissions.
    # Used in unique constraint so initial + resubmission rows coexist per claim_id.
    resubmission_key : Mapped[str]           = mapped_column(String(50), nullable=False, default="")
    # Free-text provider justification from <Resubmission><Comment> in CS XML
    resubmission_comment: Mapped[Optional[str]] = mapped_column(Text)

    # Internal
    created_at       : Mapped[datetime]      = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at       : Mapped[datetime]      = mapped_column(TIMESTAMPTZ, server_default=func.now(), onupdate=func.now())

    facility         : Mapped["Facility"]            = relationship(back_populates="claims")
    diagnoses        : Mapped[List["ClaimDiagnosis"]] = relationship(back_populates="claim", cascade="all, delete-orphan")
    activities       : Mapped[List["ClaimActivity"]]  = relationship(back_populates="claim", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("facility_id", "claim_id", "resubmission_key", name="uq_claims_facility_claim_resub"),
        CheckConstraint("source_channel IN ('API', 'FILE', 'EXCEL_HISTORICAL')", name="ck_claim_source_channel"),
        Index("idx_claims_encounter_start", "encounter_start"),
        Index("idx_claims_payer", "payer_id"),
        Index("idx_claims_member", "member_id"),
        Index("idx_claims_channel", "source_channel"),
        Index("idx_claims_receiver", "receiver_id"),
        Index("idx_claims_clinician", "clinician_id"),
        Index("idx_claims_department", "department"),
        Index("idx_claims_status", "claim_status"),
    )

    def __repr__(self) -> str:
        return f"<Claim {self.claim_id} | Channel={self.source_channel} @ {self.facility_id}>"


# ---------------------------------------------------------------------------
# ClaimDiagnosis
# ---------------------------------------------------------------------------
class ClaimDiagnosis(Base):
    __tablename__ = "claim_diagnoses"

    id                 : Mapped[int]           = mapped_column(BigInteger, primary_key=True)
    claim_id_fk        : Mapped[int]           = mapped_column(BigInteger, ForeignKey("claims.id", ondelete="CASCADE"), nullable=False)
    facility_id        : Mapped[int]           = mapped_column(Integer, ForeignKey("facilities.id"), nullable=False)
    diagnosis_type     : Mapped[str]           = mapped_column(String(20), nullable=False)
    diagnosis_code     : Mapped[str]           = mapped_column(String(20), nullable=False)
    diagnosis_description: Mapped[Optional[str]] = mapped_column(String(300))
    sequence_no        : Mapped[int]           = mapped_column(SmallInteger, nullable=False, default=1)
    created_at         : Mapped[datetime]      = mapped_column(TIMESTAMPTZ, server_default=func.now())

    claim              : Mapped["Claim"]       = relationship(back_populates="diagnoses")

    __table_args__ = (
        CheckConstraint("diagnosis_type IN ('Primary','Admitting','Secondary')", name="ck_diag_type"),
        Index("idx_diagnoses_claim", "claim_id_fk"),
        Index("idx_diagnoses_code", "diagnosis_code"),
    )


# ---------------------------------------------------------------------------
# ClaimActivity
# ---------------------------------------------------------------------------
class ClaimActivity(Base):
    __tablename__ = "claim_activities"

    id                 : Mapped[int]            = mapped_column(BigInteger, primary_key=True)
    claim_id_fk        : Mapped[int]            = mapped_column(BigInteger, ForeignKey("claims.id", ondelete="CASCADE"), nullable=False)
    facility_id        : Mapped[int]            = mapped_column(Integer, ForeignKey("facilities.id"), nullable=False)
    activity_id        : Mapped[Optional[str]]  = mapped_column(String(100))
    activity_type      : Mapped[str]            = mapped_column(String(20), nullable=False)
    activity_code      : Mapped[str]            = mapped_column(String(50), nullable=False)
    activity_description: Mapped[Optional[str]] = mapped_column(String(300))
    non_std_code       : Mapped[Optional[str]]  = mapped_column(String(50))
    quantity           : Mapped[Decimal]        = mapped_column(Numeric(10, 3), nullable=False, default=1)
    gross              : Mapped[Decimal]        = mapped_column(Numeric(12, 2), nullable=False)
    net                : Mapped[Decimal]        = mapped_column(Numeric(12, 2), nullable=False)
    patient_share      : Mapped[Decimal]        = mapped_column(Numeric(12, 2), nullable=False, default=0)
    activity_start     : Mapped[date]           = mapped_column(Date, nullable=False)
    authorization_id   : Mapped[Optional[str]]  = mapped_column(String(100))
    clinician_id       : Mapped[Optional[str]]  = mapped_column(String(50))
    sequence_no        : Mapped[int]            = mapped_column(SmallInteger, nullable=False, default=1)
    created_at         : Mapped[datetime]       = mapped_column(TIMESTAMPTZ, server_default=func.now())

    claim              : Mapped["Claim"]               = relationship(back_populates="activities")
    observations       : Mapped[List["ClaimObservation"]] = relationship(back_populates="activity", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint("activity_type IN ('CPT','Dental','Drug','Service','HCPCS')", name="ck_activity_type"),
        Index("idx_activities_claim", "claim_id_fk"),
        Index("idx_activities_type", "activity_type"),
        Index("idx_activities_code", "activity_code"),
    )


# ---------------------------------------------------------------------------
# ClaimObservation
# ---------------------------------------------------------------------------
class ClaimObservation(Base):
    __tablename__ = "claim_observations"

    id                 : Mapped[int]            = mapped_column(BigInteger, primary_key=True)
    activity_id_fk     : Mapped[int]            = mapped_column(BigInteger, ForeignKey("claim_activities.id", ondelete="CASCADE"), nullable=False)
    claim_id_fk        : Mapped[Optional[int]]  = mapped_column(BigInteger, ForeignKey("claims.id", ondelete="CASCADE"), nullable=True)
    observation_type   : Mapped[str]            = mapped_column(String(100), nullable=False)
    observation_code   : Mapped[Optional[str]]  = mapped_column(String(100))
    observation_value  : Mapped[Optional[str]]  = mapped_column(Text)
    value_type         : Mapped[Optional[str]]  = mapped_column(String(50))
    created_at         : Mapped[datetime]       = mapped_column(TIMESTAMPTZ, server_default=func.now())

    activity           : Mapped["ClaimActivity"] = relationship(back_populates="observations")

    __table_args__ = (
        # FK indexes — without them every cascaded claim/activity delete
        # (re-parse) scans this multi-GB table. Created CONCURRENTLY 2026-10-06.
        Index("idx_claim_obs_claim_fk", "claim_id_fk"),
        Index("idx_claim_obs_activity_fk", "activity_id_fk"),
    )


# ---------------------------------------------------------------------------
# Remittance (Adjusted for Hybrid Ingestion Channels)
# ---------------------------------------------------------------------------
class Remittance(Base):
    __tablename__ = "remittances"

    id                 : Mapped[int]            = mapped_column(BigInteger, primary_key=True)
    facility_id        : Mapped[int]            = mapped_column(Integer, ForeignKey("facilities.id"), nullable=False)

    # Made nullable to accommodate Excel-extracted Adjudication baselines
    raw_xml_id         : Mapped[Optional[int]]  = mapped_column(BigInteger, ForeignKey("raw_xml_store.id"), nullable=True)
    source_channel     : Mapped[str]            = mapped_column(String(20), nullable=False, default="API")

    claim_id           : Mapped[str]            = mapped_column(String(100), nullable=False)
    claim_id_payer     : Mapped[Optional[str]]  = mapped_column(String(100))
    payer_id           : Mapped[str]            = mapped_column(String(50), nullable=False)
    remittance_date    : Mapped[date]           = mapped_column(Date, nullable=False)
    ra_file_id         : Mapped[Optional[str]]  = mapped_column(String(100))
    gross_amount       : Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    net_amount         : Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    paid_amount        : Mapped[Decimal]        = mapped_column(Numeric(12, 2), nullable=False, default=0)
    patient_share_total: Mapped[Decimal]        = mapped_column(Numeric(12, 2), nullable=False, default=0)
    discount_amount    : Mapped[Decimal]        = mapped_column(Numeric(12, 2), nullable=False, default=0)
    denial_amount      : Mapped[Decimal]        = mapped_column(Numeric(12, 2), nullable=False, default=0)
    claim_status_ra    : Mapped[Optional[str]]  = mapped_column(String(50))
    denial_code        : Mapped[Optional[str]]  = mapped_column(String(50))
    denial_description : Mapped[Optional[str]]  = mapped_column(String(500))
    # TPA free-text comments from RA.Claim/Comments (separate from denial_description)
    tpa_comments       : Mapped[Optional[str]]  = mapped_column(Text)
    created_at         : Mapped[datetime]       = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at         : Mapped[datetime]       = mapped_column(TIMESTAMPTZ, server_default=func.now(), onupdate=func.now())

    facility           : Mapped["Facility"]     = relationship(back_populates="remittances")
    activity_details   : Mapped[List["RemittanceActivityDetail"]] = relationship(
        back_populates="remittance", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("facility_id", "claim_id", "remittance_date", name="uq_remittance_claim_date"),
        CheckConstraint("source_channel IN ('API', 'FILE', 'EXCEL_HISTORICAL')", name="ck_remittance_source_channel"),
        Index("idx_remittances_claim_id", "claim_id"),
        Index("idx_remittances_remittance_date", "remittance_date"),
        Index("idx_remittances_channel", "source_channel"),
        # Composite index for ClaimCycleV2 CTEs: ra_all_agg, latest_ra, first_ra
        # Covers WHERE facility_id = :fid + GROUP/ORDER BY (facility_id, claim_id, id)
        Index("idx_remittances_fac_claim_id", "facility_id", "claim_id", "id"),
    )


# ---------------------------------------------------------------------------
# RemittanceActivityDetail
# ---------------------------------------------------------------------------
class RemittanceActivityDetail(Base):
    __tablename__ = "remittance_activity_details"

    id                 : Mapped[int]            = mapped_column(BigInteger, primary_key=True)
    remittance_id_fk   : Mapped[int]            = mapped_column(BigInteger, ForeignKey("remittances.id", ondelete="CASCADE"), nullable=False)
    facility_id        : Mapped[int]            = mapped_column(Integer, ForeignKey("facilities.id"), nullable=False)
    activity_id        : Mapped[Optional[str]]  = mapped_column(String(100))
    activity_code      : Mapped[Optional[str]]  = mapped_column(String(50))
    activity_type      : Mapped[Optional[str]]  = mapped_column(String(20))
    gross              : Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    net                : Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    paid_amount        : Mapped[Decimal]        = mapped_column(Numeric(12, 2), nullable=False, default=0)
    patient_share      : Mapped[Decimal]        = mapped_column(Numeric(12, 2), nullable=False, default=0)
    denial_code        : Mapped[Optional[str]]  = mapped_column(String(50))
    denial_description : Mapped[Optional[str]]  = mapped_column(String(500))
    denial_type        : Mapped[Optional[str]]  = mapped_column(String(100))
    quantity_approved  : Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 3))
    created_at         : Mapped[datetime]       = mapped_column(TIMESTAMPTZ, server_default=func.now())

    remittance         : Mapped["Remittance"]   = relationship(back_populates="activity_details")

    __table_args__ = (
        # FK lookups (RA re-parse, report joins) — without it every lookup is a
        # full scan of this 3M+ row table. Created CONCURRENTLY 2026-10-06.
        Index("idx_rad_remittance_fk", "remittance_id_fk"),
    )


# ---------------------------------------------------------------------------
# ReconResult
# ---------------------------------------------------------------------------
class ReconResult(Base):
    __tablename__ = "recon_results"

    id                     : Mapped[int]               = mapped_column(BigInteger, primary_key=True)
    facility_id            : Mapped[int]               = mapped_column(Integer, ForeignKey("facilities.id"), nullable=False)
    claim_id               : Mapped[str]               = mapped_column(String(100), nullable=False)
    claim_pk               : Mapped[Optional[int]]     = mapped_column(BigInteger, ForeignKey("claims.id"))
    remittance_pk          : Mapped[Optional[int]]     = mapped_column(BigInteger, ForeignKey("remittances.id"))
    encounter_start        : Mapped[Optional[datetime]]= mapped_column(TIMESTAMPTZ)
    submission_date        : Mapped[Optional[date]]    = mapped_column(Date)
    remittance_date        : Mapped[Optional[date]]    = mapped_column(Date)
    payer_id               : Mapped[Optional[str]]     = mapped_column(String(50))
    member_id              : Mapped[Optional[str]]     = mapped_column(String(100))
    submitted_gross        : Mapped[Decimal]           = mapped_column(Numeric(12, 2), nullable=False, default=0)
    submitted_net          : Mapped[Decimal]           = mapped_column(Numeric(12, 2), nullable=False, default=0)
    paid_amount            : Mapped[Decimal]           = mapped_column(Numeric(12, 2), nullable=False, default=0)
    patient_share          : Mapped[Decimal]           = mapped_column(Numeric(12, 2), nullable=False, default=0)
    discount_amount        : Mapped[Decimal]           = mapped_column(Numeric(12, 2), nullable=False, default=0)
    denial_amount          : Mapped[Decimal]           = mapped_column(Numeric(12, 2), nullable=False, default=0)
    recon_status           : Mapped[str]               = mapped_column(String(30), nullable=False)
    denial_code            : Mapped[Optional[str]]     = mapped_column(String(50))
    denial_description     : Mapped[Optional[str]]     = mapped_column(String(500))
    last_reconciled_at     : Mapped[datetime]          = mapped_column(TIMESTAMPTZ, server_default=func.now())

    facility               : Mapped["Facility"]        = relationship(back_populates="recon_results")

    __table_args__ = (
        UniqueConstraint("facility_id", "claim_id", name="uq_recon_facility_claim"),
        Index("idx_recon_encounter_start", "encounter_start"),
        Index("idx_recon_status", "recon_status"),
    )


# ---------------------------------------------------------------------------
# FetchLog
# ---------------------------------------------------------------------------
class FetchLog(Base):
    __tablename__ = "fetch_log"

    id                 : Mapped[int]            = mapped_column(BigInteger, primary_key=True)
    facility_id        : Mapped[int]            = mapped_column(Integer, ForeignKey("facilities.id"), nullable=False)
    fetch_type         : Mapped[str]            = mapped_column(String(20), nullable=False)
    date_from          : Mapped[date]           = mapped_column(Date, nullable=False)
    date_to            : Mapped[date]           = mapped_column(Date, nullable=False)
    started_at         : Mapped[datetime]       = mapped_column(TIMESTAMPTZ, server_default=func.now())
    completed_at       : Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ)
    status             : Mapped[str]            = mapped_column(String(20), nullable=False, default="running")
    records_fetched    : Mapped[int]            = mapped_column(Integer, nullable=False, default=0)
    records_parsed     : Mapped[int]            = mapped_column(Integer, nullable=False, default=0)
    records_failed     : Mapped[int]            = mapped_column(Integer, nullable=False, default=0)
    http_errors        : Mapped[int]            = mapped_column(Integer, nullable=False, default=0)
    validation_errors  : Mapped[int]            = mapped_column(Integer, nullable=False, default=0)
    error_detail       : Mapped[Optional[dict]] = mapped_column(JSONB)

    facility           : Mapped["Facility"]     = relationship(back_populates="fetch_logs")

    __table_args__ = (
        CheckConstraint("fetch_type IN ('ClaimSubmission','RemittanceAdvice','Backfill','Riayati')", name="ck_fetch_type"),
        CheckConstraint("status IN ('running','success','partial','failed')", name="ck_fetch_status"),
        Index("idx_fetch_log_started", "started_at"),
    )


# ---------------------------------------------------------------------------
# PortalToken — public client portal access
# ---------------------------------------------------------------------------
class PortalToken(Base):
    __tablename__ = "portal_tokens"

    id            : Mapped[int]           = mapped_column(Integer, primary_key=True)
    token         : Mapped[str]           = mapped_column(String(64), unique=True, nullable=False)
    client_name   : Mapped[str]           = mapped_column(String(200), nullable=False)
    facility_ids  : Mapped[str]           = mapped_column(Text, nullable=False)  # JSON array e.g. "[1,2]"
    expires_at    : Mapped[Optional[date]]= mapped_column(Date, nullable=True)   # NULL = never expires
    is_active     : Mapped[bool]          = mapped_column(Boolean, nullable=False, default=True)
    created_at    : Mapped[datetime]      = mapped_column(TIMESTAMPTZ, server_default=func.now())
    last_used_at  : Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ, nullable=True)

    __table_args__ = (
        Index("idx_portal_tokens_token", "token"),
    )


# ---------------------------------------------------------------------------
# ReportJob — staged report queue
# ---------------------------------------------------------------------------
class ReportJob(Base):
    __tablename__ = "report_jobs"

    id            : Mapped[int]             = mapped_column(Integer, primary_key=True)
    user_id       : Mapped[Optional[int]]   = mapped_column(Integer, nullable=True)  # no ORM FK — avoids User model resolution issues
    report_type   : Mapped[str]             = mapped_column(String(50), nullable=False)
    report_label  : Mapped[str]             = mapped_column(String(100), nullable=False)
    filters_json  : Mapped[Optional[str]]   = mapped_column(Text)        # JSON string of applied filters
    status        : Mapped[str]             = mapped_column(String(20), nullable=False, default="completed")
    file_data     : Mapped[Optional[bytes]] = mapped_column(LargeBinary)  # raw xlsx bytes
    filename      : Mapped[Optional[str]]   = mapped_column(String(255))
    row_count     : Mapped[Optional[int]]   = mapped_column(Integer)
    error_message : Mapped[Optional[str]]   = mapped_column(Text)
    created_at    : Mapped[datetime]        = mapped_column(TIMESTAMPTZ, server_default=func.now())
    completed_at  : Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ)

    __table_args__ = (
        Index("idx_report_jobs_created_at", "created_at"),
    )


# ---------------------------------------------------------------------------
# ReceiptPayerMapping — payer → GL account / cost-centre mapping per facility
# ---------------------------------------------------------------------------
class ReceiptPayerMapping(Base):
    __tablename__ = "receipt_payer_mapping"

    id              : Mapped[int]           = mapped_column(Integer, primary_key=True)
    facility_id     : Mapped[int]           = mapped_column(Integer, ForeignKey("facilities.id"), nullable=False)
    payer_id        : Mapped[str]           = mapped_column(String(50), nullable=False)
    payer_name      : Mapped[Optional[str]] = mapped_column(String(150))
    # 'DHPO' | 'Riayati' | 'ALL' (matches both channels)
    channel         : Mapped[str]           = mapped_column(String(10), nullable=False, default="ALL")
    gl_account_code : Mapped[Optional[str]] = mapped_column(String(50))
    cost_centre     : Mapped[Optional[str]] = mapped_column(String(50))
    # tolerance below which a short-pay is NOT flagged (AED)
    tolerance_aed   : Mapped[Decimal]       = mapped_column(Numeric(10, 2), nullable=False, default=0)
    is_active       : Mapped[bool]          = mapped_column(Boolean, nullable=False, default=True)
    created_at      : Mapped[datetime]      = mapped_column(TIMESTAMPTZ, server_default=func.now())

    facility        : Mapped["Facility"]    = relationship(back_populates="payer_mappings")

    __table_args__ = (
        UniqueConstraint("facility_id", "payer_id", "channel", name="uq_payer_mapping_facility_payer_channel"),
        CheckConstraint("channel IN ('DHPO','Riayati','ALL')", name="ck_payer_mapping_channel"),
        Index("idx_payer_mapping_facility", "facility_id"),
    )

    def __repr__(self) -> str:
        return f"<ReceiptPayerMapping fac={self.facility_id} payer={self.payer_id} ch={self.channel}>"


# ---------------------------------------------------------------------------
# DailyReceipt — daily aggregate receipt per (facility, date, payer, channel)
# ---------------------------------------------------------------------------
class DailyReceipt(Base):
    __tablename__ = "daily_receipts"

    id                  : Mapped[int]           = mapped_column(BigInteger, primary_key=True)
    facility_id         : Mapped[int]           = mapped_column(Integer, ForeignKey("facilities.id"), nullable=False)
    # Filtered by raw_xml_store.submission_date (RA posting date), consistent with existing reports
    receipt_date        : Mapped[date]          = mapped_column(Date, nullable=False)
    payer_id            : Mapped[str]           = mapped_column(String(50), nullable=False)
    payer_name          : Mapped[Optional[str]] = mapped_column(String(150))
    channel             : Mapped[str]           = mapped_column(String(10), nullable=False)  # 'DHPO' | 'Riayati'

    # Financial aggregates (AED)
    expected_aed        : Mapped[Decimal]       = mapped_column(Numeric(12, 2), nullable=False, default=0)
    received_aed        : Mapped[Decimal]       = mapped_column(Numeric(12, 2), nullable=False, default=0)
    denied_aed          : Mapped[Decimal]       = mapped_column(Numeric(12, 2), nullable=False, default=0)
    patient_share_aed   : Mapped[Decimal]       = mapped_column(Numeric(12, 2), nullable=False, default=0)
    claim_count         : Mapped[int]           = mapped_column(Integer, nullable=False, default=0)

    # Payment references from RA files (JSON array of ra_file_id / source_filename strings)
    payment_references  : Mapped[Optional[str]] = mapped_column(Text)

    # reconciled = paid matches expected within tolerance
    # partial    = some claims paid, some denied
    # denied     = all claims denied
    # unmatched  = RA has no matching submission claim
    status              : Mapped[str]           = mapped_column(String(20), nullable=False, default="reconciled")

    # Accounting push
    posted_to_accounting: Mapped[bool]          = mapped_column(Boolean, nullable=False, default=False)
    posted_at           : Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ)
    gl_account_code     : Mapped[Optional[str]] = mapped_column(String(50))   # from ReceiptPayerMapping

    created_at          : Mapped[datetime]      = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at          : Mapped[datetime]      = mapped_column(TIMESTAMPTZ, server_default=func.now(), onupdate=func.now())

    facility            : Mapped["Facility"]    = relationship(back_populates="daily_receipts")

    __table_args__ = (
        UniqueConstraint(
            "facility_id", "receipt_date", "payer_id", "channel",
            name="uq_daily_receipt_facility_date_payer_channel",
        ),
        CheckConstraint("channel IN ('DHPO','Riayati')", name="ck_daily_receipt_channel"),
        CheckConstraint(
            "status IN ('reconciled','partial','denied','unmatched')",
            name="ck_daily_receipt_status",
        ),
        Index("idx_daily_receipts_date", "receipt_date"),
        Index("idx_daily_receipts_facility_date", "facility_id", "receipt_date"),
        Index("idx_daily_receipts_status", "status"),
    )

    def __repr__(self) -> str:
        return (
            f"<DailyReceipt fac={self.facility_id} {self.receipt_date} "
            f"{self.channel}/{self.payer_id} received={self.received_aed}>"
        )


# ---------------------------------------------------------------------------
# ExclusionCode — DHA standard Exclude 1 & 2 reference codes
# ---------------------------------------------------------------------------
class ExclusionCode(Base):
    __tablename__ = "exclusion_codes"

    id               : Mapped[int]           = mapped_column(Integer, primary_key=True)
    # 1 = Exclude 1 (absolute, ERROR); 2 = Exclude 2 (conditional, WARN)
    exclusion_type   : Mapped[int]           = mapped_column(SmallInteger, nullable=False)
    # 'diagnosis' for ICD codes; 'activity' for CPT/HCPCS/Service codes
    code_type        : Mapped[str]           = mapped_column(String(20), nullable=False)
    code             : Mapped[str]           = mapped_column(String(50), nullable=False)
    description      : Mapped[Optional[str]] = mapped_column(Text)
    is_active        : Mapped[bool]          = mapped_column(Boolean, nullable=False, default=True)
    created_at       : Mapped[datetime]      = mapped_column(TIMESTAMPTZ, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("exclusion_type", "code_type", "code", name="uq_exclusion_type_code"),
        CheckConstraint("exclusion_type IN (1, 2)", name="ck_exclusion_type"),
        CheckConstraint("code_type IN ('diagnosis','activity')", name="ck_exclusion_code_type"),
        Index("idx_exclusion_codes_type_code", "exclusion_type", "code_type", "code"),
    )

    def __repr__(self) -> str:
        return f"<ExclusionCode E{self.exclusion_type} {self.code_type}:{self.code}>"


# ---------------------------------------------------------------------------
# ScrubSession — one upload/run of a CS XML file through the scrubber
# ---------------------------------------------------------------------------
class ScrubSession(Base):
    __tablename__ = "scrub_sessions"

    id               : Mapped[int]           = mapped_column(BigInteger, primary_key=True)
    facility_id      : Mapped[int]           = mapped_column(Integer, ForeignKey("facilities.id"), nullable=False)
    uploaded_by      : Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
    uploaded_filename: Mapped[str]           = mapped_column(String(500), nullable=False)
    # 'uploaded' → 'parsed' → 'running' → 'completed' | 'failed'
    status           : Mapped[str]           = mapped_column(String(20), nullable=False, default="uploaded")
    claims_count     : Mapped[Optional[int]] = mapped_column(Integer)
    parse_error      : Mapped[Optional[str]] = mapped_column(Text)
    uploaded_at      : Mapped[datetime]      = mapped_column(TIMESTAMPTZ, server_default=func.now())
    run_completed_at : Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ)

    facility         : Mapped["Facility"]        = relationship("Facility")
    uploader         : Mapped[Optional["User"]]  = relationship("User")
    scrub_claims     : Mapped[List["ScrubClaim"]] = relationship(back_populates="session", cascade="all, delete-orphan")
    scrub_results    : Mapped[List["ScrubResult"]] = relationship(back_populates="session", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint(
            "status IN ('uploaded','parsed','running','completed','failed')",
            name="ck_scrub_session_status",
        ),
        Index("idx_scrub_sessions_facility", "facility_id"),
        Index("idx_scrub_sessions_uploaded_at", "uploaded_at"),
    )

    def __repr__(self) -> str:
        return f"<ScrubSession {self.id} fac={self.facility_id} {self.status}>"


# ---------------------------------------------------------------------------
# ScrubClaim — claim extracted from the staging XML (not production claims)
# ---------------------------------------------------------------------------
class ScrubClaim(Base):
    __tablename__ = "scrub_claims"

    id               : Mapped[int]           = mapped_column(BigInteger, primary_key=True)
    session_id       : Mapped[int]           = mapped_column(BigInteger, ForeignKey("scrub_sessions.id", ondelete="CASCADE"), nullable=False)
    facility_id      : Mapped[int]           = mapped_column(Integer, ForeignKey("facilities.id"), nullable=False)
    claim_id         : Mapped[str]           = mapped_column(String(100), nullable=False)
    encounter_type   : Mapped[Optional[str]] = mapped_column(String(50))
    encounter_start  : Mapped[Optional[date]]= mapped_column(Date)
    encounter_end    : Mapped[Optional[date]]= mapped_column(Date)
    member_id        : Mapped[Optional[str]] = mapped_column(String(100))
    emirates_id      : Mapped[Optional[str]] = mapped_column(String(50))
    payer_id         : Mapped[Optional[str]] = mapped_column(String(50))
    receiver_id      : Mapped[Optional[str]] = mapped_column(String(50))
    patient_id       : Mapped[Optional[str]] = mapped_column(String(100))   # Encounter/PatientID (MRN)
    resubmission_key : Mapped[str]           = mapped_column(String(50), nullable=False, default="")
    net_amount       : Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    created_at       : Mapped[datetime]      = mapped_column(TIMESTAMPTZ, server_default=func.now())

    session          : Mapped["ScrubSession"]           = relationship(back_populates="scrub_claims")
    activities       : Mapped[List["ScrubClaimActivity"]] = relationship(back_populates="scrub_claim", cascade="all, delete-orphan")
    diagnoses        : Mapped[List["ScrubClaimDiagnosis"]] = relationship(back_populates="scrub_claim", cascade="all, delete-orphan")

    __table_args__ = (
        Index("idx_scrub_claims_session", "session_id"),
        Index("idx_scrub_claims_emirates_id", "emirates_id"),
        Index("idx_scrub_claims_encounter_start", "encounter_start"),
    )

    def __repr__(self) -> str:
        return f"<ScrubClaim {self.claim_id} session={self.session_id}>"


# ---------------------------------------------------------------------------
# ScrubClaimActivity — activities from the staging CS XML
# ---------------------------------------------------------------------------
class ScrubClaimActivity(Base):
    __tablename__ = "scrub_claim_activities"

    id               : Mapped[int]           = mapped_column(BigInteger, primary_key=True)
    scrub_claim_id   : Mapped[int]           = mapped_column(BigInteger, ForeignKey("scrub_claims.id", ondelete="CASCADE"), nullable=False)
    session_id       : Mapped[int]           = mapped_column(BigInteger, ForeignKey("scrub_sessions.id", ondelete="CASCADE"), nullable=False)
    activity_type    : Mapped[Optional[str]] = mapped_column(String(20))
    activity_code    : Mapped[str]           = mapped_column(String(50), nullable=False)
    quantity         : Mapped[Decimal]       = mapped_column(Numeric(10, 3), nullable=False, default=1)
    net              : Mapped[Decimal]       = mapped_column(Numeric(12, 2), nullable=False, default=0)
    authorization_id : Mapped[Optional[str]] = mapped_column(String(100))
    clinician_id     : Mapped[Optional[str]] = mapped_column(String(50))
    activity_start   : Mapped[Optional[date]]= mapped_column(Date)
    sequence_no      : Mapped[int]           = mapped_column(SmallInteger, nullable=False, default=1)
    created_at       : Mapped[datetime]      = mapped_column(TIMESTAMPTZ, server_default=func.now())

    scrub_claim      : Mapped["ScrubClaim"]  = relationship(back_populates="activities")

    __table_args__ = (
        Index("idx_scrub_activities_claim", "scrub_claim_id"),
        Index("idx_scrub_activities_code", "activity_code"),
    )


# ---------------------------------------------------------------------------
# ScrubClaimDiagnosis — diagnoses from the staging CS XML
# ---------------------------------------------------------------------------
class ScrubClaimDiagnosis(Base):
    __tablename__ = "scrub_claim_diagnoses"

    id               : Mapped[int]           = mapped_column(BigInteger, primary_key=True)
    scrub_claim_id   : Mapped[int]           = mapped_column(BigInteger, ForeignKey("scrub_claims.id", ondelete="CASCADE"), nullable=False)
    session_id       : Mapped[int]           = mapped_column(BigInteger, ForeignKey("scrub_sessions.id", ondelete="CASCADE"), nullable=False)
    diagnosis_type   : Mapped[str]           = mapped_column(String(20), nullable=False)
    diagnosis_code   : Mapped[str]           = mapped_column(String(20), nullable=False)
    sequence_no      : Mapped[int]           = mapped_column(SmallInteger, nullable=False, default=1)
    created_at       : Mapped[datetime]      = mapped_column(TIMESTAMPTZ, server_default=func.now())

    scrub_claim      : Mapped["ScrubClaim"]  = relationship(back_populates="diagnoses")

    __table_args__ = (
        Index("idx_scrub_diagnoses_claim", "scrub_claim_id"),
        Index("idx_scrub_diagnoses_code", "diagnosis_code"),
    )


# ---------------------------------------------------------------------------
# ScrubResult — one rule result per (session, claim, rule)
# ---------------------------------------------------------------------------
class ScrubResult(Base):
    __tablename__ = "scrub_results"

    id               : Mapped[int]           = mapped_column(BigInteger, primary_key=True)
    session_id       : Mapped[int]           = mapped_column(BigInteger, ForeignKey("scrub_sessions.id", ondelete="CASCADE"), nullable=False)
    scrub_claim_id   : Mapped[int]           = mapped_column(BigInteger, ForeignKey("scrub_claims.id", ondelete="CASCADE"), nullable=False)
    # FREE_FOLLOWUP | DUPLICATE | MISSING_AUTH | EXCLUDE_1 | EXCLUDE_2
    rule_code        : Mapped[str]           = mapped_column(String(30), nullable=False)
    # ERROR | WARN | PASS
    severity         : Mapped[str]           = mapped_column(String(10), nullable=False)
    message          : Mapped[str]           = mapped_column(Text, nullable=False)
    # Extra context: matched production claim IDs, conflicting codes, etc.
    detail_json      : Mapped[Optional[dict]]= mapped_column(JSONB)
    created_at       : Mapped[datetime]      = mapped_column(TIMESTAMPTZ, server_default=func.now())

    session          : Mapped["ScrubSession"] = relationship(back_populates="scrub_results")

    __table_args__ = (
        CheckConstraint(
            "rule_code IN ('FREE_FOLLOWUP','DUPLICATE','MISSING_AUTH','EXCLUDE_1','EXCLUDE_2')",
            name="ck_scrub_result_rule_code",
        ),
        CheckConstraint("severity IN ('ERROR','WARN','PASS')", name="ck_scrub_result_severity"),
        UniqueConstraint("session_id", "scrub_claim_id", "rule_code", name="uq_scrub_result_session_claim_rule"),
        Index("idx_scrub_results_session", "session_id"),
        Index("idx_scrub_results_severity", "severity"),
    )

    def __repr__(self) -> str:
        return f"<ScrubResult session={self.session_id} claim={self.scrub_claim_id} {self.rule_code}={self.severity}>"


# ---------------------------------------------------------------------------
# ScrubXmlFinding — findings from the Strawberry XML claim scrubber
# Complements ScrubResult (DB-backed business rules) with structural, coding,
# arithmetic, MUE, frequency, and denial-risk checks from scrubber.py.
# ---------------------------------------------------------------------------
class ScrubXmlFinding(Base):
    __tablename__ = "scrub_xml_findings"

    id             : Mapped[int]           = mapped_column(BigInteger, primary_key=True)
    # Either session_id (manual upload) OR raw_xml_id (auto-fetch) must be set
    session_id     : Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("scrub_sessions.id", ondelete="CASCADE"), nullable=True)
    raw_xml_id     : Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("raw_xml_store.id", ondelete="CASCADE"), nullable=True)
    # BLOCK | WARN | PASS  (overall session status stored on the first row; per-finding below)
    session_status : Mapped[str]           = mapped_column(String(10), nullable=False)
    # Denial-risk fields (populated once per session on the first finding row; NULL on subsequent rows)
    risk_level     : Mapped[Optional[str]] = mapped_column(String(10))   # HIGH | MEDIUM | LOW
    risk_score     : Mapped[Optional[int]] = mapped_column(Integer)
    # Per-finding fields (from scrubber findings[])
    severity       : Mapped[str]           = mapped_column(String(10), nullable=False)   # BLOCK | WARN | PASS
    rule_id        : Mapped[str]           = mapped_column(String(60), nullable=False)
    location       : Mapped[Optional[str]] = mapped_column(String(200))
    claim_id       : Mapped[Optional[str]] = mapped_column(String(100))
    activity_id    : Mapped[Optional[str]] = mapped_column(String(100))
    message        : Mapped[str]           = mapped_column(Text, nullable=False)
    source         : Mapped[Optional[str]] = mapped_column(Text)
    profile        : Mapped[str]           = mapped_column(String(30), nullable=False, default="UAE_COMMON")
    scrubbed_at    : Mapped[datetime]      = mapped_column(TIMESTAMPTZ, server_default=func.now())

    __table_args__ = (
        CheckConstraint("session_status IN ('BLOCK','WARN','PASS')", name="ck_scrub_xml_session_status"),
        CheckConstraint("severity IN ('BLOCK','WARN','PASS')", name="ck_scrub_xml_severity"),
        CheckConstraint("(session_id IS NOT NULL OR raw_xml_id IS NOT NULL)", name="ck_scrub_xml_source"),
        Index("idx_scrub_xml_session", "session_id"),
        Index("idx_scrub_xml_raw_xml", "raw_xml_id"),
        Index("idx_scrub_xml_severity", "severity"),
        Index("idx_scrub_xml_rule_id", "rule_id"),
    )

    def __repr__(self) -> str:
        return f"<ScrubXmlFinding session={self.session_id} {self.rule_id}={self.severity}>"


# ---------------------------------------------------------------------------
# Resubmission worklist — editable playbook + per-activity tracking
# ---------------------------------------------------------------------------
class ScrubQuery(Base):
    """
    Claim query generated by the pre-submission check, in the Ghaf claim-query
    template wording (one row per query, exported as the batch query Excel).
    """
    __tablename__ = "scrub_queries"

    id               : Mapped[int]           = mapped_column(BigInteger, primary_key=True)
    session_id       : Mapped[int]           = mapped_column(BigInteger, ForeignKey("scrub_sessions.id", ondelete="CASCADE"), nullable=False)
    scrub_claim_id   : Mapped[int]           = mapped_column(BigInteger, ForeignKey("scrub_claims.id", ondelete="CASCADE"), nullable=False)
    rule_code        : Mapped[str]           = mapped_column(String(30), nullable=False)
    query_type       : Mapped[str]           = mapped_column(String(80), nullable=False)
    status           : Mapped[str]           = mapped_column(String(40), nullable=False)
    incharge         : Mapped[str]           = mapped_column(String(20), nullable=False, default="GENERAL")
    item             : Mapped[Optional[str]] = mapped_column(String(300))   # Procedure / Code / Dx column
    query_text       : Mapped[str]           = mapped_column(Text, nullable=False)
    evidence         : Mapped[Optional[str]] = mapped_column(Text)          # why the rule fired (not exported)
    created_at       : Mapped[datetime]      = mapped_column(TIMESTAMPTZ, server_default=func.now())

    __table_args__ = (
        Index("idx_scrub_queries_session", "session_id"),
    )


class ResubPlaybook(Base):
    """What to do for each denial-code pattern. Edited from the Resubmission Worklist screen."""
    __tablename__ = "resub_playbook"

    id               : Mapped[int]           = mapped_column(Integer, primary_key=True)
    sort_order       : Mapped[int]           = mapped_column(Integer, nullable=False, default=100)
    pattern          : Mapped[str]           = mapped_column(String(200), nullable=False)   # regex on denial code
    activity_pattern : Mapped[Optional[str]] = mapped_column(String(200))   # optional regex on activity code
    reason_pattern   : Mapped[Optional[str]] = mapped_column(String(300))   # optional regex on denial reason text
    category         : Mapped[str]           = mapped_column(String(80), nullable=False)
    owner            : Mapped[str]           = mapped_column(String(20), nullable=False, default="Ghaf")  # Ghaf | Facility
    facility_role    : Mapped[Optional[str]] = mapped_column(String(80))                   # e.g. Doctor, Front office
    action           : Mapped[str]           = mapped_column(Text, nullable=False)
    facility_request : Mapped[Optional[str]] = mapped_column(Text)                         # what we ask the facility for
    documents        : Mapped[Optional[str]] = mapped_column(Text)
    draft_comment    : Mapped[Optional[str]] = mapped_column(Text)
    is_active        : Mapped[bool]          = mapped_column(Boolean, nullable=False, default=True)
    updated_by       : Mapped[Optional[str]] = mapped_column(String(255))
    updated_at       : Mapped[datetime]      = mapped_column(TIMESTAMPTZ, server_default=func.now(), onupdate=func.now())

    __table_args__ = (CheckConstraint("owner IN ('Ghaf','Facility')", name="ck_resub_playbook_owner"),)


class ResubTracking(Base):
    """Team status for one denied activity (facility + claim + activity + RA date)."""
    __tablename__ = "resub_tracking"

    id             : Mapped[int]           = mapped_column(Integer, primary_key=True)
    facility_id    : Mapped[int]           = mapped_column(Integer, ForeignKey("facilities.id"), nullable=False)
    claim_id       : Mapped[str]           = mapped_column(String(100), nullable=False)
    activity_id    : Mapped[str]           = mapped_column(String(100), nullable=False)
    ra_date        : Mapped[date]          = mapped_column(Date, nullable=False)
    status         : Mapped[str]           = mapped_column(String(60), nullable=False, default="New")
    owner_override : Mapped[Optional[str]] = mapped_column(String(20))   # Ghaf | Facility
    assigned_to    : Mapped[Optional[str]] = mapped_column(String(120))
    remarks        : Mapped[Optional[str]] = mapped_column(Text)
    facility_reply : Mapped[Optional[str]] = mapped_column(Text)
    updated_by     : Mapped[Optional[str]] = mapped_column(String(255))
    updated_at     : Mapped[datetime]      = mapped_column(TIMESTAMPTZ, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("facility_id", "claim_id", "activity_id", "ra_date", name="uq_resub_tracking_key"),
        Index("ix_resub_tracking_fac", "facility_id"),
    )
