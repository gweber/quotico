"""
backend/app/models/teams.py

Purpose:
    Pydantic Team Tower models for canonical team identity and alias handling.

Dependencies:
    - pydantic
    - bson.ObjectId
    - app.models.common.PyObjectId
"""

from datetime import datetime

from bson import ObjectId
from pydantic import BaseModel, ConfigDict, Field

from app.models.common import PyObjectId


class TeamAlias(BaseModel):
    name: str
    sport_key: str | None = None


class Team(BaseModel):
    id: PyObjectId | None = Field(alias="_id", default=None)
    display_name: str
    short_name: str | None = None
    code: str | None = None
    aliases: list[TeamAlias] = Field(default_factory=list)
    external_ids: dict[str, str] = Field(default_factory=dict)
    needs_review: bool = False
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(
        populate_by_name=True,
        arbitrary_types_allowed=True,
        json_encoders={ObjectId: str},
    )
