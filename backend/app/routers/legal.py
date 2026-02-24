"""Legal documents API — serves pre-rendered HTML for Impressum, Datenschutz, AGB, Jugendschutz."""

from datetime import datetime

from app.utils import utcnow
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from app.config_legal import LEGAL_DOCS, TERMS_VERSION, TERMS_UPDATED_AT
from app.database import get_db
from app.services.auth_service import get_current_user
from app.services.audit_service import log_audit

router = APIRouter(prefix="/api/legal", tags=["legal"])


class LegalDocResponse(BaseModel):
    key: str
    title: str
    slug: str
    content_html: str
    version: Optional[str] = None
    updated_at: str


class AcceptTermsRequest(BaseModel):
    version: str


def _doc_response(key: str) -> LegalDocResponse:
    doc = LEGAL_DOCS[key]
    return LegalDocResponse(
        key=key,
        title=doc["title"],
        slug=doc["slug"],
        content_html=doc["content_html"],
        version=TERMS_VERSION if key == "terms" else None,
        updated_at=TERMS_UPDATED_AT,
    )


@router.get("/imprint", response_model=LegalDocResponse)
async def get_imprint():
    return _doc_response("imprint")


@router.get("/privacy", response_model=LegalDocResponse)
async def get_privacy():
    return _doc_response("privacy")


@router.get("/terms", response_model=LegalDocResponse)
async def get_terms():
    return _doc_response("terms")


@router.get("/youth-protection", response_model=LegalDocResponse)
async def get_youth_protection():
    return _doc_response("youth-protection")


@router.get("/all")
async def get_all_docs():
    """Return metadata for all legal documents (no content)."""
    return [
        {
            "key": key,
            "title": doc["title"],
            "slug": doc["slug"],
            "version": TERMS_VERSION if key == "terms" else None,
        }
        for key, doc in LEGAL_DOCS.items()
    ]


@router.post("/accept-terms")
async def accept_terms(
    body: AcceptTermsRequest,
    request: Request,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """Accept the current AGB version. Logged in audit_logs for compliance."""
    if body.version != TERMS_VERSION:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Version {body.version} is outdated. Current version: {TERMS_VERSION}.",
        )

    now = utcnow()
    user_id = str(user["_id"])

    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {
            "terms_accepted_version": TERMS_VERSION,
            "terms_accepted_at": now,
            "updated_at": now,
        }},
    )

    await log_audit(
        actor_id=user_id,
        target_id=user_id,
        action="TERMS_ACCEPTED",
        metadata={"version": TERMS_VERSION},
        request=request,
    )

    return {"message": "Terms accepted.", "version": TERMS_VERSION}


@router.get("/terms-status")
async def get_terms_status(user=Depends(get_current_user)):
    """Check if the current user has accepted the latest AGB version."""
    accepted = user.get("terms_accepted_version")
    return {
        "current_version": TERMS_VERSION,
        "accepted_version": accepted,
        "accepted": accepted == TERMS_VERSION,
    }
