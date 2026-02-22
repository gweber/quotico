from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, field_validator


class UserInDB(BaseModel):
    """Full user document as stored in MongoDB."""
    email: EmailStr
    hashed_password: str
    alias: str
    alias_slug: str
    has_custom_alias: bool = False
    points: float = 0.0
    is_admin: bool = False
    is_banned: bool = False
    is_2fa_enabled: bool = False
    encrypted_2fa_secret: Optional[str] = None
    encryption_key_version: int = 1
    is_deleted: bool = False
    is_adult: bool = False
    birth_date_verified_at: Optional[datetime] = None
    terms_accepted_version: Optional[str] = None
    terms_accepted_at: Optional[datetime] = None
    # Wallet compliance
    wallet_disclaimer_accepted_at: Optional[datetime] = None
    # Anti-abuse household clustering
    household_group_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class UserCreate(BaseModel):
    """Request body for registration with age verification."""
    email: EmailStr
    password: str
    birth_date: str  # YYYY-MM-DD format, validated server-side
    disclaimer_accepted: bool = False

    @field_validator("password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        if len(v) < 10:
            raise ValueError("Passwort muss mindestens 10 Zeichen lang sein.")
        if not any(c.isupper() for c in v):
            raise ValueError("Passwort muss mindestens einen Großbuchstaben enthalten.")
        if not any(c.isdigit() for c in v):
            raise ValueError("Passwort muss mindestens eine Ziffer enthalten.")
        return v


class UserLogin(BaseModel):
    """Request body for login."""
    email: EmailStr
    password: str


class AliasUpdate(BaseModel):
    """Request body for changing alias."""
    alias: str


class UserResponse(BaseModel):
    """Public user data returned to the client."""
    email: str
    alias: str
    alias_slug: str
    has_custom_alias: bool
    points: float
    is_admin: bool = False
    is_2fa_enabled: bool
    is_adult: bool = True
    terms_accepted_version: Optional[str] = None
    created_at: datetime
