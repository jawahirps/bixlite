"""
Ghaf App — authentication & login tracking endpoints.
"""
from datetime import datetime
from typing import List

import io
from typing import Optional

import pyotp
import qrcode
import qrcode.image.svg
from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import User, LoginLog
from ..schemas import Token, UserOut, UserCreate, LoginLogOut, TotpSetupOut, TotpCode
from ..security import (
    verify_password,
    hash_password,
    create_access_token,
    get_current_user,
    require_roles,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])

TOTP_ISSUER = "Ghaf Bix"


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

    user = db.query(User).filter(User.email == form_data.username).first()

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
    return Token(access_token=token, user=UserOut.model_validate(user))


@router.get("/me", response_model=UserOut)
def me(current_user: User = Depends(get_current_user)):
    return current_user


@router.post("/totp/setup", response_model=TotpSetupOut)
def totp_setup(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Start authenticator-app setup: new secret + QR code. Not active until /totp/enable confirms a code."""
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
    if db.query(User).filter(User.email == payload.email).first():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")

    user = User(
        email=payload.email,
        full_name=payload.full_name,
        hashed_password=hash_password(payload.password),
        role=payload.role,
        facility_id=payload.facility_id,
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
    return db.query(User).order_by(User.full_name.asc()).all()
