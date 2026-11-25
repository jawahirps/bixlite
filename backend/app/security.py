"""
Ghaf App — auth helpers: password hashing, JWT issuance/validation,
and FastAPI dependencies for the current user / role checks.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from .config import get_settings
from .database import get_db
from .models import User

settings = get_settings()
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
        user_id: Optional[str] = payload.get("sub")
        if user_id is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    user = db.get(User, int(user_id))
    if user is None or not user.is_active:
        raise credentials_exception
    if is_expired(user):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Your access has expired. Please contact Ghaf.", headers={"WWW-Authenticate": "Bearer"})
    allowed_facility_ids(db, user)   # computed once per request, kept on the user object
    return user


def dubai_today():
    return (datetime.now(timezone.utc) + timedelta(hours=4)).date()


def is_expired(user: User) -> bool:
    return bool(user.expires_at and dubai_today() > user.expires_at)


def require_roles(*roles: str):
    """Dependency factory restricting an endpoint to specific user roles."""

    def _checker(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")
        return user

    return _checker


def allowed_facility_ids(db, user: User):
    """Facility ids a user may see. None = no limit (Super Admin). Cached on the user object per request."""
    from sqlalchemy import text as _t
    if user.role == "admin":
        return None
    if "_rbac_allowed" in user.__dict__:
        return user.__dict__["_rbac_allowed"]
    if db is None:
        from .database import SessionLocal
        with SessionLocal() as s2:
            return allowed_facility_ids(s2, user)
    rows = db.execute(_t("""
        SELECT a.facility_id FROM user_facility_access a WHERE a.user_id = :u AND a.facility_id IS NOT NULL
        UNION
        SELECT f.id FROM user_facility_access a JOIN facilities f ON f.group_id = a.group_id
        WHERE a.user_id = :u AND a.group_id IS NOT NULL"""), {"u": user.id}).scalars().all()
    user.__dict__["_rbac_allowed"] = set(rows)
    return user.__dict__["_rbac_allowed"]


# ---------------------------------------------------------------------------
# RBAC step 2: facility scope enforcement
# ---------------------------------------------------------------------------
CLINIC_ROLES = ("group_admin", "facility_admin", "viewer")   # clinic-side logins (see the facility view)
VIEWER_POST_OK = ("/api/reports/generate/", "/api/reports/jobs/", "/api/dha-sync/export-excel-bulk")


def is_clinic_user(user: User) -> bool:
    return user.role in CLINIC_ROLES


def check_facility(db, user: User, facility_id) -> None:
    """403 unless the user may see this facility (Super Admin: always)."""
    if facility_id is None:
        return
    allowed = allowed_facility_ids(db, user)
    if allowed is not None and int(facility_id) not in allowed:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You don't have access to this facility")


def resolve_facility(db, user: User, facility_id):
    """facility_id to use for a query. Super Admin: as given (None = all facilities).
    Others: must be one of theirs; when omitted, their only facility, else 400."""
    allowed = allowed_facility_ids(db, user)
    if allowed is None:
        return facility_id
    if facility_id is None:
        if len(allowed) == 1:
            return next(iter(allowed))
        raise HTTPException(status_code=400, detail="Choose a facility")
    if int(facility_id) not in allowed:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You don't have access to this facility")
    return int(facility_id)


# Record ids in a path that belong to a facility: (path prefix, param) -> SQL returning that facility_id
_RECORD_FACILITY = [
    ("/api/scrub/sessions/", "session_id", "SELECT facility_id FROM scrub_sessions WHERE id = :v"),
    ("/api/dha-sync/raw-xml/", "record_id", "SELECT facility_id FROM raw_xml_store WHERE id = :v"),
    ("/api/docreview/findings/", "finding_id", "SELECT facility_id FROM doc_findings WHERE id = :v"),
    ("/api/clinical-profiles/", "profile_id", "SELECT facility_id FROM clinical_profiles WHERE id = :v"),
    ("/api/packs/jobs/", "job_id", "SELECT facility_id FROM pack_jobs WHERE id = :v"),
]


def _ids(v):
    out = []
    for part in str(v).split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            out.append(int(part))
    return out


async def facility_guard(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Router-level guard: every facility id a request names (path, query, form or JSON body) must be
    one the user may see; Viewers are read-only. Endpoints still scope 'no facility given' themselves."""
    if user.role == "viewer" and request.method not in ("GET", "HEAD", "OPTIONS") \
            and not request.url.path.startswith(VIEWER_POST_OK):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Viewer accounts are read-only")
    allowed = allowed_facility_ids(db, user)
    if allowed is None:
        return
    named = []
    for k in ("facility_id", "facility_ids"):
        if k in request.path_params:
            named += _ids(request.path_params[k])
        for v in request.query_params.getlist(k):
            named += _ids(v)
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        ctype = request.headers.get("content-type", "")
        try:
            if ctype.startswith("application/json"):
                body = await request.json()
                if isinstance(body, dict):
                    for k in ("facility_id", "facility_ids"):
                        v = body.get(k)
                        if isinstance(v, list):
                            named += [int(x) for x in v if str(x).lstrip("-").isdigit()]
                        elif v is not None:
                            named += _ids(v)
            elif ctype.startswith(("multipart/form-data", "application/x-www-form-urlencoded")):
                form = await request.form()
                for v in form.getlist("facility_id"):
                    named += _ids(v)
        except Exception:
            pass
    from sqlalchemy import text as _t
    path = request.url.path
    for prefix, param, sql in _RECORD_FACILITY:
        if path.startswith(prefix) and param in request.path_params:
            fid = db.execute(_t(sql), {"v": int(request.path_params[param])}).scalar()
            if fid is not None:
                named.append(int(fid))
    if path.startswith("/api/scrub/") and request.query_params.get("session_ids"):
        sids = _ids(request.query_params["session_ids"])
        if sids:
            named += [int(x) for x in db.execute(_t("SELECT facility_id FROM scrub_sessions WHERE id = ANY(:v)"),
                                                 {"v": sids}).scalars().all()]
    bad = [f for f in named if f not in allowed]
    if bad:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You don't have access to this facility")
