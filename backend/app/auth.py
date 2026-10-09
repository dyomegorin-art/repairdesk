"""Backend authorization. The role is mapped from a pre-seeded identity (docs/SPEC.md, 2.5)."""
from typing import NoReturn

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.audit import audit
from app.models import Identity

IDENTITY_HEADER = "X-Synthetic-Identity"

PERMISSIONS: dict[str, set[str]] = {
    "owner": {"queue.view", "case.view", "case.update_intake_status", "audit.view", "issues.view"},
    "dispatcher": {"queue.view", "case.view", "case.update_intake_status", "audit.view"},
    "technician": {"queue.view_assigned", "case.view_assigned"},
    "developer": {"queue.view", "case.view", "audit.view", "issues.view"},
}


def permissions_for(role: str) -> list[str]:
    return sorted(PERMISSIONS.get(role, set()))


def get_session(request: Request):
    session = request.app.state.sessionmaker()
    try:
        yield session
    finally:
        session.close()


def current_identity(request: Request, session: Session = Depends(get_session)) -> Identity:
    if not request.app.state.settings.synthetic_auth_active:
        raise HTTPException(401, "unauthorized")
    identity_id = request.headers.get(IDENTITY_HEADER)
    identity = session.get(Identity, identity_id) if identity_id else None
    if identity is None:
        raise HTTPException(401, "unauthorized")
    return identity


def deny(session: Session, identity: Identity, request: Request, permission: str,
         case_id: str | None = None) -> NoReturn:
    """Audit and reject. Always 403, so a technician cannot probe which case ids exist."""
    audit(session, "ACCESS_DENIED", identity.identity_id, case_id=case_id,
          permission=permission, path=request.url.path, method=request.method)
    session.commit()
    raise HTTPException(403, "forbidden")


def has_permission(identity: Identity, permission: str) -> bool:
    return permission in PERMISSIONS.get(identity.role, set())


def require_any(*permissions: str):
    """Pass if the identity holds at least one of the permissions."""

    def dependency(
        request: Request,
        identity: Identity = Depends(current_identity),
        session: Session = Depends(get_session),
    ) -> Identity:
        if not any(has_permission(identity, p) for p in permissions):
            deny(session, identity, request, " | ".join(permissions))
        return identity

    return dependency


def require(permission: str):
    return require_any(permission)
