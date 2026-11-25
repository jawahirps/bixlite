"""
Ghaf App — Facility management endpoints.

GET  /api/facilities          — list all facilities (admin only)
GET  /api/facilities/{id}     — get one facility
PUT  /api/facilities/{id}     — update credentials / settings (admin only)
GET/POST/PUT/DELETE /api/facilities/{id}/riayati-accounts[/{account_id}]
                              — extra Riayati accounts (e.g. SHA licence after a regulator change)
POST /api/facilities/{id}/test-connection
                              — one read-only call to check a DHA eClaimLink or Riayati login
"""
from datetime import date, datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Facility, FacilityRiayatiAccount, SettingsChange, User
from ..security import get_current_user, require_roles, allowed_facility_ids

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


SECRET_FIELDS = {"riayati_password", "credential_key", "password"}


def _show(field: str, value):
    """Value as stored in the change history; secrets are reduced to set / cleared."""
    if field in SECRET_FIELDS:
        return "(set)" if value else "(empty)"
    return None if value is None else str(value)


def _log_changes(db: Session, request: Request, user: User, facility_id: int, entity: str, label: str,
                 action: str, changes: list):
    """changes: [(field, old, new)] — writes one history row per field (one row with no field for delete)."""
    ip = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for", "").split(",")[0].strip() \
        or (request.client.host if request.client else None)
    ua = (request.headers.get("user-agent") or "")[:300]
    for field, old, new in changes or [(None, None, None)]:
        if field in SECRET_FIELDS and action == "update" and old != new:
            old_s, new_s = _show(field, old), ("(changed)" if old and new else _show(field, new))
        else:
            old_s, new_s = _show(field or "", old), _show(field or "", new)
        db.add(SettingsChange(facility_id=facility_id, entity=entity, entity_label=label, action=action,
                              field=field, old_value=old_s, new_value=new_s, user_id=user.id,
                              user_email=user.email, client_ip=ip, user_agent=ua))


def _require_admin(user: User):
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required.")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@router.get("", response_model=list[FacilityOut])
def list_facilities(
    include_inactive: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    q = db.query(Facility)
    if current_user.role != "admin":
        # RBAC: other roles get only their own facilities, names only (no usernames, no Riayati accounts)
        allowed = allowed_facility_ids(db, current_user) or set()
        rows = q.filter(Facility.is_active.is_(True), Facility.id.in_(allowed or [-1])).order_by(Facility.facility_name).all()
        return [FacilityOut(id=f.id, facility_code=f.facility_code, facility_name=f.facility_name,
                            license_id=f.license_id, is_active=f.is_active) for f in rows]
    if not include_inactive:  # merged / switched-off facilities only show on Facility settings
        q = q.filter(Facility.is_active.is_(True))
    facilities = q.order_by(Facility.facility_name).all()
    return [FacilityOut.from_orm_facility(f) for f in facilities]


@router.post("", response_model=FacilityOut, status_code=status.HTTP_201_CREATED)
def create_facility(
    payload: FacilityCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin")),
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
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin")),
):
    _require_admin(current_user)
    fac = db.get(Facility, facility_id)
    if not fac:
        raise HTTPException(status_code=404, detail="Facility not found.")

    update_data = payload.model_dump(exclude_unset=True)
    # secrets are write-only: a blank password / key in the form means "keep the current one"
    for secret in ("riayati_password", "credential_key"):
        if not (update_data.get(secret) or "").strip():
            update_data.pop(secret, None)
    changes = [(f, getattr(fac, f), v) for f, v in update_data.items() if getattr(fac, f) != v]
    for field, value in update_data.items():
        setattr(fac, field, value)
    if changes:
        _log_changes(db, request, current_user, fac.id, "facility", fac.facility_name, "update", changes)

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
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin")),
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
    _log_changes(db, request, current_user, facility_id, "riayati_account", f"{acc.label} ({acc.username})", "create",
                 [(f, None, getattr(acc, f)) for f in ("label", "username", "password", "license_id", "active_from", "is_active")])
    db.commit()
    db.refresh(acc)
    return RiayatiAccountOut.from_account(acc)


@router.put("/{facility_id}/riayati-accounts/{account_id}", response_model=RiayatiAccountOut)
def update_riayati_account(
    facility_id: int,
    account_id: int,
    payload: RiayatiAccountIn,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin")),
):
    _require_admin(current_user)
    acc = _account_or_404(db, facility_id, account_id)
    data = payload.model_dump(exclude_unset=True)
    if not (data.get("password") or "").strip():
        data.pop("password", None)          # blank password keeps the current one
    data = {f: (v.strip() if isinstance(v, str) else v) for f, v in data.items()}
    changes = [(f, getattr(acc, f), v) for f, v in data.items() if getattr(acc, f) != v]
    for field, value in data.items():
        setattr(acc, field, value)
    if changes:
        _log_changes(db, request, current_user, facility_id, "riayati_account", f"{acc.label} ({acc.username})",
                     "update", changes)
    db.commit()
    db.refresh(acc)
    return RiayatiAccountOut.from_account(acc)


@router.delete("/{facility_id}/riayati-accounts/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_riayati_account(
    facility_id: int,
    account_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin")),
):
    _require_admin(current_user)
    acc = _account_or_404(db, facility_id, account_id)
    _log_changes(db, request, current_user, facility_id, "riayati_account", f"{acc.label} ({acc.username})",
                 "delete", [])
    db.delete(acc)
    db.commit()


# ---------------------------------------------------------------------------
# Connection test (one read-only call per login; credentials are never returned)
# ---------------------------------------------------------------------------
class ConnectionTestIn(BaseModel):
    target: str                         # "dha" | "riayati"
    account_id: Optional[int] = None    # extra Riayati account; None = primary login


class ConnectionTestOut(BaseModel):
    ok: bool
    target: str
    label: str
    message: str


DEMO_FACILITY_CODES = {"GHAF-001"}       # never used against real DHPO


def _test_dha(fac: Facility) -> ConnectionTestOut:
    from ..services.fetch_service import DHPO_LIVE_WSDL, DHPOSoapClient
    label = f"DHA eClaimLink ({fac.eclaimlink_username})"
    if fac.facility_code in DEMO_FACILITY_CODES:
        return ConnectionTestOut(ok=False, target="dha", label=label, message="Demo facility — not tested against DHPO.")
    if not fac.eclaimlink_username or fac.eclaimlink_username.upper() == "N/A" or not fac.credential_key:
        return ConnectionTestOut(ok=False, target="dha", label=label, message="No DHA eClaimLink login set.")
    try:
        files = DHPOSoapClient(DHPO_LIVE_WSDL, timeout=30).get_new_transactions(fac.eclaimlink_username, fac.credential_key)
        return ConnectionTestOut(ok=True, target="dha", label=label,
                                 message=f"Login OK — {len(files)} new transaction file(s) waiting.")
    except Exception as exc:                      # DHPO puts the reason in the error text
        return ConnectionTestOut(ok=False, target="dha", label=label, message=str(exc)[:300])


def _test_riayati(db: Session, fac: Facility, account_id: Optional[int]) -> ConnectionTestOut:
    from datetime import timedelta
    import requests as _requests
    from ..services.riayati_service import (DIRECTION_RECEIVED, RiayatiClient, RiayatiInactiveFacilityError)
    if account_id:
        acc = _account_or_404(db, fac.id, account_id)
        username, password, label = acc.username, acc.password, f"Riayati {acc.label} ({acc.username})"
    else:
        username, password, label = fac.riayati_username, fac.riayati_password, f"Riayati primary ({fac.riayati_username})"
    if not username or not password:
        return ConnectionTestOut(ok=False, target="riayati", label=label, message="Username or password not set.")
    today = date.today()
    try:
        found = RiayatiClient(username, password, timeout=60).search(today - timedelta(days=1), today, DIRECTION_RECEIVED)
        return ConnectionTestOut(ok=True, target="riayati", label=label,
                                 message=f"Login OK — {len(found)} remittance transaction(s) in the last 2 days.")
    except RiayatiInactiveFacilityError as exc:
        return ConnectionTestOut(ok=False, target="riayati", label=label,
                                 message=f"Facility licence inactive on Riayati: {exc}")
    except _requests.HTTPError as exc:
        msg = ""
        try:
            msg = exc.response.json().get("Message") or ""
        except Exception:
            pass
        code = exc.response.status_code if exc.response is not None else "?"
        return ConnectionTestOut(ok=False, target="riayati", label=label,
                                 message=f"Rejected by Riayati ({code}){': ' + msg if msg else ''}")
    except Exception as exc:
        return ConnectionTestOut(ok=False, target="riayati", label=label,
                                 message=f"Could not reach Riayati (tunnel / network): {str(exc)[:200]}")


@router.post("/{facility_id}/test-connection", response_model=ConnectionTestOut)
def test_connection(
    facility_id: int,
    payload: ConnectionTestIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin")),
):
    _require_admin(current_user)
    fac = _facility_or_404(db, facility_id)
    if payload.target == "dha":
        return _test_dha(fac)
    if payload.target == "riayati":
        return _test_riayati(db, fac, payload.account_id)
    raise HTTPException(status_code=422, detail="target must be 'dha' or 'riayati'.")


# ---------------------------------------------------------------------------
# Settings change history
# ---------------------------------------------------------------------------
class SettingsChangeOut(BaseModel):
    id: int
    changed_at: datetime
    entity: str
    entity_label: Optional[str] = None
    action: str
    field: Optional[str] = None
    old_value: Optional[str] = None
    new_value: Optional[str] = None
    user_email: Optional[str] = None
    client_ip: Optional[str] = None
    user_agent: Optional[str] = None

    model_config = {"from_attributes": True}


@router.get("/{facility_id}/changes", response_model=list[SettingsChangeOut])
def facility_changes(
    facility_id: int,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    _facility_or_404(db, facility_id)
    return (db.query(SettingsChange).filter_by(facility_id=facility_id)
            .order_by(SettingsChange.changed_at.desc(), SettingsChange.id.desc()).limit(min(limit, 500)).all())
