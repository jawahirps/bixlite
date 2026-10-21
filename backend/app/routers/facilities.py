"""
Ghaf App — Facility management endpoints.

GET  /api/facilities          — list all facilities (admin only)
GET  /api/facilities/{id}     — get one facility
PUT  /api/facilities/{id}     — update credentials / settings (admin only)
GET/POST/PUT/DELETE /api/facilities/{id}/riayati-accounts[/{account_id}]
                              — extra Riayati accounts (e.g. SHA licence after a regulator change)
"""
from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Facility, FacilityRiayatiAccount, User
from ..security import get_current_user

router = APIRouter(prefix="/api/facilities", tags=["facilities"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class FacilityOut(BaseModel):
    id: int
    facility_code: str
    facility_name: str
    license_id: Optional[str] = None
    eclaimlink_username: Optional[str] = None
    dhpo_provider_id: Optional[str] = None
    riayati_username: Optional[str] = None
    is_active: bool
    has_credential: bool = False
    riayati_accounts: List["RiayatiAccountOut"] = []

    model_config = {"from_attributes": True}

    @classmethod
    def from_orm_facility(cls, fac) -> "FacilityOut":
        data = {
            "id": fac.id,
            "facility_code": fac.facility_code,
            "facility_name": fac.facility_name,
            "license_id": fac.license_id,
            "eclaimlink_username": fac.eclaimlink_username,
            "dhpo_provider_id": fac.dhpo_provider_id,
            "riayati_username": fac.riayati_username,
            "is_active": fac.is_active,
            # Live if DHA credential_key OR Riayati credentials are set
            "has_credential": bool(fac.credential_key) or bool(fac.riayati_username)
                              or any(a.is_active for a in fac.riayati_accounts),
            "riayati_accounts": [RiayatiAccountOut.from_account(a) for a in fac.riayati_accounts],
        }
        return cls(**data)


class RiayatiAccountOut(BaseModel):
    id: int
    label: str
    username: str
    license_id: Optional[str] = None
    active_from: Optional[date] = None
    is_active: bool
    has_password: bool

    @classmethod
    def from_account(cls, a) -> "RiayatiAccountOut":
        return cls(id=a.id, label=a.label, username=a.username, license_id=a.license_id,
                   active_from=a.active_from, is_active=a.is_active, has_password=bool(a.password))


class RiayatiAccountIn(BaseModel):
    label: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None      # blank on update = keep current
    license_id: Optional[str] = None
    active_from: Optional[date] = None
    is_active: Optional[bool] = None


FacilityOut.model_rebuild()


class FacilityCreate(BaseModel):
    facility_code: str
    facility_name: str
    license_id: str = ""
    eclaimlink_username: str = ""
    credential_key: str = ""
    dhpo_provider_id: Optional[str] = None
    is_active: bool = True


class FacilityUpdate(BaseModel):
    facility_name: Optional[str] = None
    license_id: Optional[str] = None
    eclaimlink_username: Optional[str] = None
    credential_key: Optional[str] = None
    dhpo_provider_id: Optional[str] = None
    riayati_username: Optional[str] = None
    riayati_password: Optional[str] = None
    is_active: Optional[bool] = None


def _require_admin(user: User):
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required.")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@router.get("", response_model=list[FacilityOut])
def list_facilities(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    facilities = db.query(Facility).order_by(Facility.facility_name).all()
    return [FacilityOut.from_orm_facility(f) for f in facilities]


@router.post("", response_model=FacilityOut, status_code=status.HTTP_201_CREATED)
def create_facility(
    payload: FacilityCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    existing = db.query(Facility).filter(Facility.facility_code == payload.facility_code).first()
    if existing:
        raise HTTPException(status_code=409, detail=f"Facility code '{payload.facility_code}' already exists.")
    fac = Facility(**payload.model_dump())
    db.add(fac)
    db.commit()
    db.refresh(fac)
    return FacilityOut.from_orm_facility(fac)


@router.get("/{facility_id}", response_model=FacilityOut)
def get_facility(
    facility_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    fac = db.get(Facility, facility_id)
    if not fac:
        raise HTTPException(status_code=404, detail="Facility not found.")
    return FacilityOut.from_orm_facility(fac)


@router.put("/{facility_id}", response_model=FacilityOut)
def update_facility(
    facility_id: int,
    payload: FacilityUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    fac = db.get(Facility, facility_id)
    if not fac:
        raise HTTPException(status_code=404, detail="Facility not found.")

    update_data = payload.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(fac, field, value)

    db.commit()
    db.refresh(fac)
    return FacilityOut.from_orm_facility(fac)


# ---------------------------------------------------------------------------
# Extra Riayati accounts
# ---------------------------------------------------------------------------
def _facility_or_404(db: Session, facility_id: int) -> Facility:
    fac = db.get(Facility, facility_id)
    if not fac:
        raise HTTPException(status_code=404, detail="Facility not found.")
    return fac


def _account_or_404(db: Session, facility_id: int, account_id: int) -> FacilityRiayatiAccount:
    acc = db.get(FacilityRiayatiAccount, account_id)
    if not acc or acc.facility_id != facility_id:
        raise HTTPException(status_code=404, detail="Riayati account not found.")
    return acc


@router.get("/{facility_id}/riayati-accounts", response_model=list[RiayatiAccountOut])
def list_riayati_accounts(
    facility_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    fac = _facility_or_404(db, facility_id)
    return [RiayatiAccountOut.from_account(a) for a in fac.riayati_accounts]


@router.post("/{facility_id}/riayati-accounts", response_model=RiayatiAccountOut, status_code=status.HTTP_201_CREATED)
def add_riayati_account(
    facility_id: int,
    payload: RiayatiAccountIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    _facility_or_404(db, facility_id)
    if not (payload.label and payload.username and payload.password):
        raise HTTPException(status_code=422, detail="Label, username and password are required.")
    acc = FacilityRiayatiAccount(
        facility_id=facility_id, label=payload.label.strip(), username=payload.username.strip(),
        password=payload.password.strip(), license_id=(payload.license_id or "").strip() or None,
        active_from=payload.active_from, is_active=True if payload.is_active is None else payload.is_active,
    )
    db.add(acc)
    db.commit()
    db.refresh(acc)
    return RiayatiAccountOut.from_account(acc)


@router.put("/{facility_id}/riayati-accounts/{account_id}", response_model=RiayatiAccountOut)
def update_riayati_account(
    facility_id: int,
    account_id: int,
    payload: RiayatiAccountIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    acc = _account_or_404(db, facility_id, account_id)
    data = payload.model_dump(exclude_unset=True)
    if not (data.get("password") or "").strip():
        data.pop("password", None)          # blank password keeps the current one
    for field, value in data.items():
        setattr(acc, field, value.strip() if isinstance(value, str) else value)
    db.commit()
    db.refresh(acc)
    return RiayatiAccountOut.from_account(acc)


@router.delete("/{facility_id}/riayati-accounts/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_riayati_account(
    facility_id: int,
    account_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    acc = _account_or_404(db, facility_id, account_id)
    db.delete(acc)
    db.commit()
