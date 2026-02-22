import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pymongo.errors import DuplicateKeyError

from app.database import get_db
from app.models.user import AliasUpdate
from app.services.alias_service import validate_alias, normalize_slug
from app.services.auth_service import get_current_user

logger = logging.getLogger("quotico.user")
router = APIRouter(prefix="/api/user", tags=["user"])


@router.patch("/alias")
async def update_alias(
    body: AliasUpdate,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """Set or change the user's alias.

    Validates format, checks blacklist, and enforces uniqueness via DB index.
    """
    error = validate_alias(body.alias)
    if error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error,
        )

    slug = normalize_slug(body.alias)
    now = datetime.now(timezone.utc)

    try:
        result = await db.users.update_one(
            {"_id": user["_id"]},
            {
                "$set": {
                    "alias": body.alias,
                    "alias_slug": slug,
                    "has_custom_alias": True,
                    "updated_at": now,
                }
            },
        )
    except DuplicateKeyError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Dieser Name ist bereits vergeben.",
        )

    if result.modified_count == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Alias konnte nicht geändert werden.",
        )

    logger.info("User %s changed alias to: %s", str(user["_id"]), body.alias)
    return {"message": "Alias erfolgreich geändert.", "alias": body.alias, "alias_slug": slug}
