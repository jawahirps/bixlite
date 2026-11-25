"""
Ghaf App — authentication & login tracking endpoints.
"""
from datetime import datetime
from typing import List
import re

import io
from typing import Optional

import pyotp
import qrcode
import qrcode.image.svg
from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import User, LoginLog, Facility, FacilityGroup, UserFacilityAccess
from ..schemas import Token, UserOut, UserCreate, UserUpdate, PasswordReset, UserAccess, FacilityGroupIn, LoginLogOut, TotpSetupOut, TotpCode
from ..security import is_expired, dubai_today, allowed_facility_ids
from ..security import (
    verify_password,
    hash_password,
    create_access_token,
    get_current_user,
    require_roles,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])

TOTP_ISSUER = "Ghaf Bix"
ROLES = ("admin", "group_admin", "facility_admin", "staff", "viewer", "billing_manager")


def _totp_ok(secret: Optional[str], code: Optional[str]) -> bool:
    """Check a 6-digit authenticator code, allowing one 30s step of clock drift."""
    if not secret or not code:
        return False
    return pyotp.TOTP(secret).verify(code.strip().replace(" ", ""), valid_window=1)


@router.post("/login", response_model=Token)
def login(
    request: Request,
    form_data: OAuth2PasswordRequestForm = Depends(),
    otp: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    """Authenticate a user and record the attempt in the login_logs audit trail."""
    ip = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")

    user = db.query(User).filter(User.email == form_data.username.strip().lower()).first()

    if not user or not verify_password(form_data.password, user.hashed_password):
        db.add(
            LoginLog(
                user_id=user.id if user else None,
                email_attempted=form_data.username,
                success=False,
                failure_reason="invalid_credentials",
                ip_address=ip,
                user_agent=user_agent,
            )
        )
        db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect email or password")

    if user.is_active and is_expired(user):
        db.add(LoginLog(user_id=user.id, email_attempted=form_data.username, success=False,
                        failure_reason="expired", ip_address=ip, user_agent=user_agent))
        db.commit()
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Your access has expired. Please contact Ghaf.")

    if not user.is_active:
        db.add(
            LoginLog(
                user_id=user.id,
                email_attempted=form_data.username,
                success=False,
                failure_reason="inactive_account",
                ip_address=ip,
                user_agent=user_agent,
            )
        )
        db.commit()
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is inactive")

    if user.totp_enabled:
        if not otp:
            # Password was right; the client should now ask for the authenticator code.
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="totp_required")
        if not _totp_ok(user.totp_secret, otp):
            db.add(
                LoginLog(
                    user_id=user.id,
                    email_attempted=form_data.username,
                    success=False,
                    failure_reason="invalid_totp",
                    ip_address=ip,
                    user_agent=user_agent,
                )
            )
            db.commit()
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authenticator code")

    user.last_login_at = datetime.utcnow()
    db.add(
        LoginLog(
            user_id=user.id,
            email_attempted=form_data.username,
            success=True,
            ip_address=ip,
            user_agent=user_agent,
        )
    )
    db.commit()
    db.refresh(user)

    token = create_access_token({"sub": str(user.id), "role": user.role})
    uo = UserOut.model_validate(user)
    uo.pages = pages_for(db, user)
    return Token(access_token=token, user=uo)


@router.get("/me", response_model=UserOut)
def me(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    o = UserOut.model_validate(current_user).model_dump()
    allowed = allowed_facility_ids(db, current_user)
    o["facility_ids"] = sorted(allowed) if allowed is not None else []   # empty for Super Admin = all
    o["pages"] = pages_for(db, current_user)                              # None for Super Admin = all
    return o


def _no_viewer_2fa(u: User):
    if u.role == "viewer":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Viewer accounts are managed by Ghaf")


@router.post("/totp/setup", response_model=TotpSetupOut)
def totp_setup(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Start authenticator-app setup: new secret + QR code. Not active until /totp/enable confirms a code."""
    _no_viewer_2fa(current_user)
    if current_user.totp_enabled:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Authenticator already enabled")
    secret = pyotp.random_base32()
    current_user.totp_secret = secret
    db.commit()

    uri = pyotp.TOTP(secret).provisioning_uri(name=current_user.email, issuer_name=TOTP_ISSUER)
    buf = io.BytesIO()
    qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage).save(buf)
    return TotpSetupOut(otpauth_uri=uri, secret=secret, qr_svg=buf.getvalue().decode())


@router.post("/totp/enable", response_model=UserOut)
def totp_enable(
    payload: TotpCode,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _no_viewer_2fa(current_user)
    if not _totp_ok(current_user.totp_secret, payload.code):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid authenticator code")
    current_user.totp_enabled = True
    db.commit()
    db.refresh(current_user)
    return current_user


@router.post("/totp/disable", response_model=UserOut)
def totp_disable(
    payload: TotpCode,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _no_viewer_2fa(current_user)
    if not current_user.totp_enabled or not _totp_ok(current_user.totp_secret, payload.code):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid authenticator code")
    current_user.totp_enabled = False
    current_user.totp_secret = None
    db.commit()
    db.refresh(current_user)
    return current_user


@router.post("/users/{user_id}/totp/reset", response_model=UserOut)
def totp_reset(
    user_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_roles("admin")),
):
    """Admin: turn off a user's authenticator (e.g. lost phone) so they can set it up again."""
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    user.totp_enabled = False
    user.totp_secret = None
    db.commit()
    db.refresh(user)
    return user


@router.get("/login-logs", response_model=List[LoginLogOut])
def login_logs(
    limit: int = 100,
    db: Session = Depends(get_db),
    _: User = Depends(require_roles("admin", "billing_manager")),
):
    """Recent login activity (admin / billing manager only)."""
    return (
        db.query(LoginLog)
        .order_by(LoginLog.created_at.desc())
        .limit(min(limit, 500))
        .all()
    )


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(
    payload: UserCreate,
    db: Session = Depends(get_db),
    _: User = Depends(require_roles("admin")),
):
    """Create a new application user (admin only)."""
    if payload.role not in ROLES:
        raise HTTPException(status_code=400, detail="Unknown role")
    login = payload.email.strip().lower()
    if "@" in login:
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", login):
            raise HTTPException(status_code=400, detail="Not a valid email address")
    elif payload.role != "viewer":
        raise HTTPException(status_code=400, detail="Use a real email for this role; plain usernames are for Viewers only")
    elif not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,49}", login):
        raise HTTPException(status_code=400, detail="Username: 3–50 letters, digits, dot, dash or underscore")
    payload.email = login
    if payload.role == "viewer" and payload.expires_at is None:
        t = dubai_today()
        payload.expires_at = t.replace(year=t.year + 1) if not (t.month == 2 and t.day == 29) else t.replace(year=t.year + 1, day=28)
    if db.query(User).filter(User.email.ilike(payload.email)).first():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")

    user = User(
        email=payload.email.lower(),
        full_name=payload.full_name.strip(),
        hashed_password=hash_password(payload.password),
        role=payload.role,
        facility_id=payload.facility_id,
        expires_at=payload.expires_at,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.get("/users", response_model=List[UserOut])
def list_users(
    db: Session = Depends(get_db),
    _: User = Depends(require_roles("admin")),
):
    users = db.query(User).order_by(User.full_name.asc()).all()
    acc = {}
    for a in db.query(UserFacilityAccess).all():
        d = acc.setdefault(a.user_id, {"facility_ids": [], "group_ids": []})
        (d["facility_ids"] if a.facility_id else d["group_ids"]).append(a.facility_id or a.group_id)
    out = []
    for u in users:
        o = UserOut.model_validate(u).model_dump()
        o.update(acc.get(u.id, {}))
        out.append(o)
    return out


def _active_admins(db: Session) -> int:
    return db.query(User).filter(User.role == "admin", User.is_active.is_(True)).count()


@router.patch("/users/{user_id}", response_model=UserOut)
def update_user(
    user_id: int,
    payload: UserUpdate,
    db: Session = Depends(get_db),
    me: User = Depends(require_roles("admin")),
):
    """Admin: change a user's name, role, facility or active status."""
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    data = payload.model_dump(exclude_unset=True)
    if "role" in data and data["role"] not in ROLES:
        raise HTTPException(status_code=400, detail="Unknown role")
    losing_admin = user.role == "admin" and user.is_active and (
        data.get("role", "admin") != "admin" or data.get("is_active", True) is False)
    if losing_admin and user.id == me.id:
        raise HTTPException(status_code=400, detail="You can't remove your own admin access or deactivate yourself")
    if losing_admin and _active_admins(db) <= 1:
        raise HTTPException(status_code=400, detail="At least one active admin must remain")
    if "full_name" in data and data["full_name"]:
        data["full_name"] = data["full_name"].strip()
    for k, v in data.items():
        setattr(user, k, v)
    db.commit()
    db.refresh(user)
    return user


@router.post("/users/{user_id}/password", response_model=UserOut)
def reset_password(
    user_id: int,
    payload: PasswordReset,
    db: Session = Depends(get_db),
    _: User = Depends(require_roles("admin")),
):
    """Admin: set a new password for a user (they should change it after signing in)."""
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    user.hashed_password = hash_password(payload.password)
    db.commit()
    db.refresh(user)
    return user


def _check_access(role: str, facility_ids, group_ids):
    """Scope rules per role (RBAC spec)."""
    if role == "admin":
        return [], []                      # Super Admin sees everything; no rows needed
    if role == "facility_admin" and (len(facility_ids) != 1 or group_ids):
        raise HTTPException(status_code=400, detail="A Facility Admin needs exactly one facility")
    if role == "group_admin" and (not group_ids or facility_ids):
        raise HTTPException(status_code=400, detail="A Group Admin needs one or more facility groups")
    if not facility_ids and not group_ids:
        raise HTTPException(status_code=400, detail="Choose at least one facility or group for this user")
    return facility_ids, group_ids


@router.put("/users/{user_id}/access", response_model=UserAccess)
def set_user_access(
    user_id: int,
    payload: UserAccess,
    db: Session = Depends(get_db),
    _: User = Depends(require_roles("admin")),
):
    """Admin: replace the facilities / facility groups a user may see."""
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    fids = sorted(set(payload.facility_ids)); gids = sorted(set(payload.group_ids))
    fids, gids = _check_access(user.role, fids, gids)
    if fids and db.query(Facility).filter(Facility.id.in_(fids)).count() != len(fids):
        raise HTTPException(status_code=400, detail="Unknown facility")
    if gids and db.query(FacilityGroup).filter(FacilityGroup.id.in_(gids)).count() != len(gids):
        raise HTTPException(status_code=400, detail="Unknown group")
    db.query(UserFacilityAccess).filter(UserFacilityAccess.user_id == user_id).delete()
    for f in fids:
        db.add(UserFacilityAccess(user_id=user_id, facility_id=f))
    for g in gids:
        db.add(UserFacilityAccess(user_id=user_id, group_id=g))
    if user.role == "facility_admin":
        user.facility_id = fids[0]
    db.commit()
    return {"facility_ids": fids, "group_ids": gids}


@router.get("/groups")
def list_groups(db: Session = Depends(get_db), _: User = Depends(require_roles("admin"))):
    """Admin: facility groups with their member facilities, plus all active facilities."""
    facs = db.query(Facility).filter(Facility.is_active.is_(True), Facility.id != 1).order_by(Facility.facility_name).all()
    groups = db.query(FacilityGroup).order_by(FacilityGroup.name).all()
    return {
        "groups": [{"id": g.id, "name": g.name, "facility_ids": [f.id for f in facs if f.group_id == g.id]} for g in groups],
        "facilities": [{"id": f.id, "name": f.facility_name, "group_id": f.group_id} for f in facs],
    }


@router.post("/groups")
def create_group(payload: FacilityGroupIn, db: Session = Depends(get_db), _: User = Depends(require_roles("admin"))):
    if db.query(FacilityGroup).filter(FacilityGroup.name.ilike(payload.name.strip())).first():
        raise HTTPException(status_code=409, detail="A group with that name exists")
    g = FacilityGroup(name=payload.name.strip())
    db.add(g); db.flush()
    db.query(Facility).filter(Facility.id.in_(payload.facility_ids)).update({Facility.group_id: g.id}, synchronize_session=False)
    db.commit()
    return {"id": g.id}


@router.put("/groups/{group_id}")
def update_group(group_id: int, payload: FacilityGroupIn, db: Session = Depends(get_db), _: User = Depends(require_roles("admin"))):
    g = db.get(FacilityGroup, group_id)
    if not g:
        raise HTTPException(status_code=404, detail="Group not found")
    g.name = payload.name.strip()
    db.query(Facility).filter(Facility.group_id == g.id).update({Facility.group_id: None}, synchronize_session=False)
    db.query(Facility).filter(Facility.id.in_(payload.facility_ids)).update({Facility.group_id: g.id}, synchronize_session=False)
    db.commit()
    return {"id": g.id}


# ---------------------------------------------------------------------------
# Menu by role (Super Admin can edit). Admin-only pages are not listed: they stay Super Admin only.
# ---------------------------------------------------------------------------
_W = ["billing_manager", "staff", "group_admin", "facility_admin"]
PAGES = [
    ("/", "Dashboard", _W + ["viewer"]),
    ("/alerts", "Facility alerts", _W + ["viewer"]),
    ("/scrubber", "Claim scrubber", _W),
    ("/doc-review", "Documentation review", _W),
    ("/dha-sync", "XML scrub monitor", _W),
    ("/calibration", "Denial intelligence", _W),
    ("/denial-trends", "Denial trends", _W),
    ("/resub-worklist", "Resubmission worklist", _W),
    ("/analytics", "Analytics & reports", _W + ["viewer"]),
    ("/turnaround", "Payer turnaround", _W + ["viewer"]),
    ("/report-packs", "Client report packs", _W + ["viewer"]),
    ("/clinical-profiles", "Clinical profiles", _W),
    ("/account-security", "Authenticator app", _W),
]
EDIT_ROLES = ["group_admin", "facility_admin", "staff", "viewer"]


def role_pages(db: Session) -> dict:
    from sqlalchemy import text as _t
    saved = db.execute(_t("SELECT value FROM app_settings WHERE key = 'role_pages'")).scalar()
    out = {r: [p for p, _, roles in PAGES if r in roles] for r in EDIT_ROLES + ["billing_manager"]}
    if saved:
        for r, paths in saved.items():
            if r in out:
                out[r] = [p for p in paths if any(p == q for q, _, _ in PAGES)]
    return out


def pages_for(db: Session, user: User):
    """Paths this user may open from the menu; None = everything (Super Admin)."""
    if user.role == "admin":
        return None
    return role_pages(db).get(user.role, [])


@router.get("/role-pages")
def get_role_pages(db: Session = Depends(get_db), _: User = Depends(require_roles("admin"))):
    return {"pages": [{"path": p, "label": l} for p, l, _ in PAGES], "roles": EDIT_ROLES, "matrix": role_pages(db)}


@router.put("/role-pages")
def put_role_pages(payload: dict, db: Session = Depends(get_db), me: User = Depends(require_roles("admin"))):
    import json
    from sqlalchemy import text as _t
    valid = {p for p, _, _ in PAGES}
    m = payload.get("matrix") or {}
    clean = {r: sorted({p for p in (m.get(r) or []) if p in valid}) for r in EDIT_ROLES}
    for r in clean:
        if "/" not in clean[r]:
            clean[r].insert(0, "/")          # everyone keeps the Dashboard (landing page)
    db.execute(_t("""INSERT INTO app_settings(key, value, updated_by) VALUES ('role_pages', CAST(:v AS JSONB), :u)
                     ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now(), updated_by = EXCLUDED.updated_by"""),
               {"v": json.dumps(clean), "u": me.id})
    db.commit()
    return {"matrix": role_pages(db)}
