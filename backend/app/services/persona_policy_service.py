"""
backend/app/services/persona_policy_service.py

Purpose:
    Server-side persona governance for QTip output levels. Resolves effective
    persona (user/default/override) and output level (none/summary/full/
    experimental) from active matrix rules with in-memory cache.

Dependencies:
    - app.database
    - app.utils.utcnow
"""

from __future__ import annotations

import time as _time
from typing import Any, Literal, TypedDict

import app.database as _db
from app.utils import utcnow

TipPersona = Literal["casual", "pro", "silent", "experimental"]
OutputLevel = Literal["none", "summary", "full", "experimental"]
PersonaSource = Literal["default", "user", "override", "policy"]

_PERSONA_VALUES: set[str] = {"casual", "pro", "silent", "experimental"}
_OUTPUT_VALUES: set[str] = {"none", "summary", "full", "experimental"}
_OUTPUT_RANK: dict[str, int] = {"none": 0, "summary": 1, "full": 2, "experimental": 3}
_POLICY_TTL_SECONDS = 30.0
_POLICY_COLLECTION = "tip_persona_policy"
_DEFAULT_PERSONA: TipPersona = "casual"
_DEFAULT_OUTPUT_BY_PERSONA: dict[TipPersona, OutputLevel] = {
    "casual": "summary",
    "pro": "full",
    "silent": "none",
    "experimental": "experimental",
}


class PersonaContext(TypedDict):
    is_authenticated: bool
    is_admin: bool
    league_tipping_enabled: bool


class RuleMatch(TypedDict, total=False):
    persona: TipPersona
    is_authenticated: bool
    is_admin: bool
    league_tipping_enabled: bool


class PolicyRule(TypedDict):
    when: RuleMatch
    set_output_level: OutputLevel


class PersonaPolicyService:
    """Runtime resolver for persona + matrix policy."""

    def __init__(self) -> None:
        self._cached_doc: dict[str, Any] | None = None
        self._expires_at = 0.0

    async def invalidate(self) -> None:
        self._cached_doc = None
        self._expires_at = 0.0

    async def _active_policy(self) -> dict[str, Any]:
        now = _time.time()
        if self._cached_doc is not None and now < self._expires_at:
            return self._cached_doc

        doc = await _db.db[_POLICY_COLLECTION].find_one(
            {"is_active": True},
            sort=[("version", -1)],
        )
        if not isinstance(doc, dict):
            doc = {
                "_id": "tip_policy_v1",
                "version": 1,
                "is_active": True,
                "rules": [],
                "updated_at": utcnow(),
                "updated_by": "system",
            }
        self._cached_doc = doc
        self._expires_at = now + _POLICY_TTL_SECONDS
        return doc

    @staticmethod
    def normalize_persona(value: str | None) -> TipPersona:
        raw = str(value or _DEFAULT_PERSONA).strip().lower()
        if raw in _PERSONA_VALUES:
            return raw  # type: ignore[return-value]
        return _DEFAULT_PERSONA

    @staticmethod
    def normalize_output(value: str | None) -> OutputLevel:
        raw = str(value or "none").strip().lower()
        if raw in _OUTPUT_VALUES:
            return raw  # type: ignore[return-value]
        return "none"

    @staticmethod
    def _rule_matches(rule_when: RuleMatch, *, persona: TipPersona, ctx: PersonaContext) -> bool:
        if "persona" in rule_when and str(rule_when["persona"]) != str(persona):
            return False
        if "is_authenticated" in rule_when and bool(rule_when["is_authenticated"]) != bool(ctx["is_authenticated"]):
            return False
        if "is_admin" in rule_when and bool(rule_when["is_admin"]) != bool(ctx["is_admin"]):
            return False
        if "league_tipping_enabled" in rule_when and bool(rule_when["league_tipping_enabled"]) != bool(ctx["league_tipping_enabled"]):
            return False
        return True

    async def resolve_effective_persona(self, user: dict[str, Any] | None) -> tuple[TipPersona, PersonaSource]:
        if not isinstance(user, dict):
            return _DEFAULT_PERSONA, "default"
        override_value = user.get("tip_override_persona")
        if isinstance(override_value, str) and override_value.strip().lower() in _PERSONA_VALUES:
            return self.normalize_persona(override_value), "override"
        user_value = user.get("tip_persona")
        if isinstance(user_value, str) and user_value.strip().lower() in _PERSONA_VALUES:
            return self.normalize_persona(user_value), "user"
        return _DEFAULT_PERSONA, "default"

    async def resolve_output_level(
        self,
        *,
        persona: TipPersona,
        ctx: PersonaContext,
    ) -> tuple[OutputLevel, int]:
        # Hard gate first
        if not ctx["league_tipping_enabled"]:
            return "none", 0

        current: OutputLevel = _DEFAULT_OUTPUT_BY_PERSONA[persona]
        policy = await self._active_policy()
        version = int(policy.get("version") or 1)
        rules = policy.get("rules") if isinstance(policy.get("rules"), list) else []

        for raw_rule in rules:
            if not isinstance(raw_rule, dict):
                continue
            when = raw_rule.get("when") if isinstance(raw_rule.get("when"), dict) else {}
            out = self.normalize_output(raw_rule.get("set_output_level"))
            if self._rule_matches(when, persona=persona, ctx=ctx):  # type: ignore[arg-type]
                # Matrix may only narrow capabilities to avoid accidental promotion.
                if _OUTPUT_RANK[out] <= _OUTPUT_RANK[current]:
                    current = out

        # Non-admin users can never get experimental via default persona mapping unless persona is explicit.
        if not ctx["is_admin"] and persona != "experimental" and current == "experimental":
            current = "full"
        return current, version


_service_singleton: PersonaPolicyService | None = None


def get_persona_policy_service() -> PersonaPolicyService:
    global _service_singleton
    if _service_singleton is None:
        _service_singleton = PersonaPolicyService()
    return _service_singleton

