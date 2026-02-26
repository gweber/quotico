"""
backend/app/services/odds_service.py

Purpose:
    Core odds ingest and market aggregation service. Ingests raw provider
    snapshots into odds_events and materializes averaged market views into
    matches.odds_meta.

Dependencies:
    - app.database
    - app.monitoring.odds_metrics
    - app.services.odds_repository
    - app.utils
"""

from __future__ import annotations

import hashlib
import logging
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from bson import ObjectId

import app.database as _db
from app.config import settings
from app.monitoring.odds_metrics import (
    METRIC_EVENTS_DEDUP,
    METRIC_EVENTS_TOTAL,
    METRIC_INGEST_LATENCY,
    METRIC_LINE_DROPPED,
    METRIC_META_CAS_CONFLICTS,
    METRIC_META_UPDATE_LATENCY,
    METRIC_META_UPDATES,
    METRIC_PROCESSING_LAG,
    METRIC_PROVIDER_COUNT,
    METRIC_PROVIDER_LAST_SEEN,
    METRIC_STALE_EXCLUDED,
    observe_latency,
)
from app.services.odds_repository import OddsMetaVersionConflict, OddsRepository
from app.services.event_bus import event_bus
from app.services.event_models import OddsIngestedEvent, make_correlation_id
from app.utils import ensure_utc, parse_utc, utcnow

logger = logging.getLogger("quotico.odds_service")

STALENESS_WINDOW = timedelta(minutes=120)
MAX_CAS_RETRIES = 3


class OddsService:
    def __init__(self, repository: OddsRepository | None = None):
        self.repo = repository or OddsRepository()

    async def ingest_snapshot_batch(
        self,
        provider: str,
        snapshots: list[dict[str, Any]],
        reference_ts: datetime | None = None,
        correlation_id: str | None = None,
    ) -> dict[str, int]:
        """
        Ingest provider snapshots and recompute affected match markets.

        `reference_ts` controls the staleness window anchor for recompute:
        - provided: use historical/import timestamp window
        - None: keep live behavior anchored to `utcnow()`
        """
        with observe_latency(METRIC_INGEST_LATENCY):
            now = utcnow()
            events: list[dict[str, Any]] = []
            touched: set[tuple[ObjectId, str]] = set()
            touched_event_counts: dict[tuple[ObjectId, str], int] = {}
            provider = provider.strip().lower()
            explicit_reference = ensure_utc(reference_ts) if reference_ts is not None else None

            for snap in snapshots:
                match_id = await self._resolve_match_id(provider, snap)
                if not match_id:
                    continue
                sport_key = str(snap.get("sport_key") or "")
                snapshot_at = parse_utc(snap.get("snapshot_at") or snap.get("commence_time") or now)
                lag = max(0.0, (now - ensure_utc(snapshot_at)).total_seconds())
                METRIC_PROCESSING_LAG.observe(lag)
                METRIC_PROVIDER_LAST_SEEN.labels(provider=provider, sport_key=sport_key).set(now.timestamp())

                for market, values, line in self._extract_markets(snap).values():
                    if not values:
                        continue
                    key = (match_id, market)
                    touched.add(key)
                    touched_event_counts[key] = touched_event_counts.get(key, 0) + len(values)
                    for selection_key, price in values.items():
                        if price is None:
                            continue
                        event_hash = self._event_hash(
                            match_id=str(match_id),
                            provider=provider,
                            market=market,
                            selection_key=selection_key,
                            price=float(price),
                            line=line,
                            snapshot_at=snapshot_at,
                        )
                        events.append(
                            {
                                "event_hash": event_hash,
                                "match_id": match_id,
                                "league_id": snap.get("league_id"),
                                "sport_key": sport_key,
                                "provider": provider,
                                "market": market,
                                "selection_key": selection_key,
                                "price": float(price),
                                "line": float(line) if isinstance(line, (int, float)) else None,
                                "snapshot_at": ensure_utc(snapshot_at),
                                "ingested_at": now,
                            }
                        )
                        METRIC_EVENTS_TOTAL.labels(provider=provider, market=market).inc()

            insert_stats = await self.repo.insert_events_idempotent(events)
            deduped = insert_stats.get("deduplicated", 0)
            if deduped:
                for event in events:
                    METRIC_EVENTS_DEDUP.labels(provider=event["provider"], market=event["market"]).inc()

            markets_updated = 0
            for match_id, market in touched:
                key = (match_id, market)
                recompute_reference = explicit_reference
                updated, effective_reference, since = await self._recompute_market(
                    match_id,
                    market,
                    reference_ts=recompute_reference,
                )
                if updated:
                    markets_updated += 1
                elif touched_event_counts.get(key, 0) > 0:
                    logger.warning(
                        "No provider data within staleness window for match=%s market=%s "
                        "reference_ts=%s since=%s events_in_batch=%d",
                        str(match_id),
                        market,
                        ensure_utc(effective_reference).isoformat(),
                        ensure_utc(since).isoformat(),
                        touched_event_counts.get(key, 0),
                    )

            result = {
                "events": len(events),
                "inserted": insert_stats.get("inserted", 0),
                "deduplicated": deduped,
                "markets_updated": markets_updated,
            }
            if settings.EVENT_BUS_ENABLED and settings.EVENT_PUBLISH_ODDS_INGESTED:
                try:
                    event_bus.publish(
                        OddsIngestedEvent(
                            source=provider,
                            correlation_id=str(correlation_id or make_correlation_id()),
                            provider=provider,
                            match_ids=sorted({str(match_id) for match_id, _ in touched}),
                            inserted=int(result["inserted"]),
                            deduplicated=int(result["deduplicated"]),
                            markets_updated=int(result["markets_updated"]),
                        )
                    )
                except Exception:
                    logger.warning("Failed to publish odds.ingested provider=%s", provider, exc_info=True)
            return result

    async def _resolve_match_id(self, provider: str, snap: dict[str, Any]) -> ObjectId | None:
        external_id = snap.get("external_id")
        if external_id:
            doc = await _db.db.matches.find_one(
                {
                    "$or": [
                        {f"external_ids.{provider}": str(external_id)},
                        {f"metadata.{provider}_id": str(external_id)},
                        {"metadata.theoddsapi_id": str(external_id)},
                    ]
                },
                {"_id": 1},
            )
            if doc:
                return doc["_id"]

        match_id = snap.get("match_id")
        if match_id:
            try:
                return ObjectId(str(match_id))
            except Exception:
                return None
        return None

    def _extract_markets(self, snap: dict[str, Any]) -> dict[str, tuple[str, dict[str, float], float | None]]:
        out: dict[str, tuple[str, dict[str, float], float | None]] = {}

        h2h = snap.get("odds") or {}
        if isinstance(h2h, dict):
            market = {k: float(v) for k, v in h2h.items() if k in {"1", "X", "2"} and isinstance(v, (int, float))}
            if market:
                out["h2h"] = ("h2h", market, None)

        totals = snap.get("totals") or snap.get("totals_odds") or {}
        if isinstance(totals, dict):
            line = totals.get("line")
            market = {
                "over": float(totals["over"]) if isinstance(totals.get("over"), (int, float)) else None,
                "under": float(totals["under"]) if isinstance(totals.get("under"), (int, float)) else None,
            }
            market = {k: v for k, v in market.items() if v is not None}
            if market:
                out["totals"] = ("totals", market, float(line) if isinstance(line, (int, float)) else None)

        spreads = snap.get("spreads") or snap.get("spreads_odds") or {}
        if isinstance(spreads, dict):
            line = spreads.get("line")
            market = {
                "home": float(spreads["home"]) if isinstance(spreads.get("home"), (int, float)) else None,
                "away": float(spreads["away"]) if isinstance(spreads.get("away"), (int, float)) else None,
            }
            market = {k: v for k, v in market.items() if v is not None}
            if market:
                out["spreads"] = ("spreads", market, float(line) if isinstance(line, (int, float)) else None)

        return out

    async def _recompute_market(
        self,
        match_id: ObjectId,
        market: str,
        reference_ts: datetime | None = None,
    ) -> tuple[bool, datetime, datetime]:
        # Historical imports pass `reference_ts`; live ingestion keeps utcnow().
        with observe_latency(METRIC_META_UPDATE_LATENCY.labels(market=market)):
            now = utcnow()
            effective_reference = ensure_utc(reference_ts) if reference_ts is not None else now
            since = effective_reference - STALENESS_WINDOW
            provider_rows = await self.repo.get_latest_provider_market_values(match_id, market, since)
            stale_providers = await self.repo.get_stale_provider_names(match_id, market, since)
            stale_excluded = len(stale_providers)
            for provider in stale_providers:
                METRIC_STALE_EXCLUDED.labels(provider=provider, market=market).inc()
            if not provider_rows:
                return False, effective_reference, since

            line_mode = None
            active_rows = provider_rows
            dropped_by_line = 0
            if market in {"totals", "spreads"}:
                line_counts = Counter(row.get("line") for row in provider_rows if row.get("line") is not None)
                if not line_counts:
                    return False, effective_reference, since
                line_mode = sorted(line_counts.items(), key=lambda x: (-x[1], x[0]))[0][0]
                active_rows = [r for r in provider_rows if r.get("line") == line_mode]
                dropped_by_line = max(0, len(provider_rows) - len(active_rows))
                for row in provider_rows:
                    if row.get("line") != line_mode:
                        METRIC_LINE_DROPPED.labels(provider=row["provider"], market=market).inc()

            provider_count = len(active_rows)
            if provider_count == 0:
                return False, effective_reference, since

            selections = sorted({k for r in active_rows for k in r.get("values", {}).keys()})
            current: dict[str, float] = {}
            min_vals: dict[str, float] = {}
            max_vals: dict[str, float] = {}

            for sel in selections:
                vals = [float(r["values"][sel]) for r in active_rows if sel in r.get("values", {})]
                if not vals:
                    continue
                current[sel] = round(sum(vals) / len(vals), 4)
                min_vals[sel] = round(min(vals), 4)
                max_vals[sel] = round(max(vals), 4)

            if market in {"totals", "spreads"} and isinstance(line_mode, (int, float)):
                current["line"] = float(line_mode)
                min_vals["line"] = float(line_mode)
                max_vals["line"] = float(line_mode)

            if not current:
                return False, effective_reference, since

            match_ctx = await _db.db.matches.find_one({"_id": match_id}, {"sport_key": 1})
            sport_key = str((match_ctx or {}).get("sport_key") or "unknown")
            for row in active_rows:
                METRIC_PROVIDER_LAST_SEEN.labels(provider=row["provider"], sport_key=sport_key).set(now.timestamp())
            METRIC_PROVIDER_COUNT.labels(sport_key=sport_key, market=market).set(provider_count)

            for attempt in range(MAX_CAS_RETRIES):
                match = await _db.db.matches.find_one(
                    {"_id": match_id},
                    {"odds_meta": 1, "sport_key": 1},
                )
                if not match:
                    return False, effective_reference, since
                odds_meta = match.get("odds_meta") or {}
                markets = odds_meta.get("markets") or {}
                old_market = markets.get(market) or {}
                expected_version = int(odds_meta.get("version", 0))

                set_fields: dict[str, Any] = {
                    f"odds_meta.updated_at": now,
                    f"odds_meta.markets.{market}.current": current,
                    f"odds_meta.markets.{market}.min": min_vals,
                    f"odds_meta.markets.{market}.max": max_vals,
                    f"odds_meta.markets.{market}.provider_count": provider_count,
                    f"odds_meta.markets.{market}.updated_at": now,
                    f"odds_meta.markets.{market}.dropped_by_line": dropped_by_line,
                    f"odds_meta.markets.{market}.stale_excluded": stale_excluded,
                    f"odds_meta.markets.{market}.staleness_window_minutes": int(STALENESS_WINDOW.total_seconds() // 60),
                }
                if line_mode is not None:
                    set_fields[f"odds_meta.markets.{market}.reference_line"] = float(line_mode)
                if not old_market.get("opening"):
                    set_fields[f"odds_meta.markets.{market}.opening"] = current

                try:
                    await self.repo.update_match_odds_meta(
                        match_id,
                        set_fields,
                        expected_version=expected_version,
                    )
                    METRIC_META_UPDATES.labels(market=market).inc()
                    return True, effective_reference, since
                except OddsMetaVersionConflict:
                    METRIC_META_CAS_CONFLICTS.labels(market=market).inc()
                    if attempt == MAX_CAS_RETRIES - 1:
                        logger.warning("odds_meta CAS conflicts exhausted for match=%s market=%s", match_id, market)
                        return False, effective_reference, since
            return False, effective_reference, since
                
    async def set_closing_from_current(self, match_id: ObjectId) -> None:
        match = await _db.db.matches.find_one(
            {"_id": match_id},
            {"odds_meta": 1},
        )
        if not match:
            return
        markets = ((match.get("odds_meta") or {}).get("markets") or {})
        now = utcnow()
        for market in ("h2h", "totals", "spreads"):
            node = markets.get(market) or {}
            current = node.get("current")
            if isinstance(current, dict) and current:
                await self.repo.set_market_closing_once(match_id, market, current, now)

    @staticmethod
    def _event_hash(
        *,
        match_id: str,
        provider: str,
        market: str,
        selection_key: str,
        price: float,
        line: float | None,
        snapshot_at,
    ) -> str:
        payload = "|".join(
            [
                match_id,
                provider,
                market,
                selection_key,
                f"{price:.8f}",
                "" if line is None else f"{line:.8f}",
                ensure_utc(snapshot_at).isoformat(),
            ]
        )
        return hashlib.sha1(payload.encode("utf-8"), usedforsecurity=False).hexdigest()


odds_service = OddsService()
