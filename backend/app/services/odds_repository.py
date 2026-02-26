"""
backend/app/services/odds_repository.py

Purpose:
    Persistence access layer for odds events and aggregated match odds_meta.
    Supports idempotent event ingest, provider-latest market fetch, CAS updates,
    and write-once closing line updates.

Dependencies:
    - app.database
    - app.utils
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from bson import ObjectId
from pymongo import UpdateOne
from pymongo.errors import BulkWriteError

import app.database as _db
from app.utils import ensure_utc


class OddsMetaVersionConflict(Exception):
    """Raised when optimistic CAS update did not match current version."""


class OddsRepository:
    async def insert_events_idempotent(self, events: list[dict[str, Any]]) -> dict[str, int]:
        if not events:
            return {"inserted": 0, "deduplicated": 0}

        ops = [
            UpdateOne(
                {"event_hash": e["event_hash"]},
                {"$setOnInsert": e},
                upsert=True,
            )
            for e in events
        ]
        try:
            result = await _db.db.odds_events.bulk_write(ops, ordered=False)
            inserted = result.upserted_count
            return {"inserted": inserted, "deduplicated": max(0, len(events) - inserted)}
        except BulkWriteError as exc:
            details = exc.details or {}
            write_errors = details.get("writeErrors", [])
            dup_count = sum(1 for e in write_errors if e.get("code") == 11000)
            inserted = details.get("nUpserted", 0)
            return {"inserted": inserted, "deduplicated": max(dup_count, len(events) - inserted)}

    async def get_latest_provider_market_values(
        self,
        match_id: ObjectId,
        market: str,
        since_ts: datetime,
    ) -> list[dict[str, Any]]:
        cursor = _db.db.odds_events.find(
            {
                "match_id": match_id,
                "market": market,
                "snapshot_at": {"$gte": since_ts},
            },
            {
                "provider": 1,
                "snapshot_at": 1,
                "selection_key": 1,
                "price": 1,
                "line": 1,
            },
        ).sort([("provider", 1), ("snapshot_at", -1)])

        docs = await cursor.to_list(length=20_000)
        latest_ts_by_provider: dict[str, datetime] = {}
        grouped_values: dict[str, dict[str, float]] = defaultdict(dict)
        grouped_line: dict[str, float | None] = {}

        for doc in docs:
            provider = str(doc.get("provider") or "")
            if not provider:
                continue
            snap = ensure_utc(doc["snapshot_at"])
            latest = latest_ts_by_provider.get(provider)
            if latest is None:
                latest_ts_by_provider[provider] = snap
                grouped_line[provider] = doc.get("line")
            elif snap != latest:
                continue

            key = str(doc.get("selection_key") or "")
            price = doc.get("price")
            if key and isinstance(price, (int, float)):
                grouped_values[provider][key] = float(price)
            if grouped_line[provider] is None and doc.get("line") is not None:
                grouped_line[provider] = doc.get("line")

        out: list[dict[str, Any]] = []
        for provider, snapshot_at in latest_ts_by_provider.items():
            values = grouped_values.get(provider, {})
            if not values:
                continue
            out.append(
                {
                    "provider": provider,
                    "snapshot_at": snapshot_at,
                    "line": grouped_line.get(provider),
                    "values": values,
                }
            )
        return out

    async def get_stale_provider_names(
        self,
        match_id: ObjectId,
        market: str,
        since_ts: datetime,
    ) -> list[str]:
        pipeline = [
            {"$match": {"match_id": match_id, "market": market}},
            {"$group": {"_id": "$provider", "latest_snapshot_at": {"$max": "$snapshot_at"}}},
            {"$match": {"latest_snapshot_at": {"$lt": since_ts}}},
        ]
        docs = await _db.db.odds_events.aggregate(pipeline).to_list(length=500)
        return [str(d.get("_id")) for d in docs if d.get("_id")]

    async def update_match_odds_meta(
        self,
        match_id: ObjectId,
        set_fields: dict[str, Any],
        *,
        expected_version: int | None = None,
    ) -> None:
        query: dict[str, Any] = {"_id": match_id}
        if expected_version is not None:
            query["odds_meta.version"] = expected_version

        update_doc = {
            "$set": set_fields,
            "$inc": {"odds_meta.version": 1},
        }
        result = await _db.db.matches.update_one(query, update_doc)
        if expected_version is not None and result.modified_count == 0:
            raise OddsMetaVersionConflict(f"CAS conflict for match_id={match_id}")

    async def set_market_closing_once(
        self,
        match_id: ObjectId,
        market: str,
        closing: dict[str, Any],
        updated_at: datetime,
    ) -> bool:
        result = await _db.db.matches.update_one(
            {
                "_id": match_id,
                f"odds_meta.markets.{market}.closing": {"$exists": False},
            },
            {
                "$set": {
                    f"odds_meta.markets.{market}.closing": closing,
                    f"odds_meta.markets.{market}.updated_at": updated_at,
                    "odds_meta.updated_at": updated_at,
                },
                "$inc": {"odds_meta.version": 1},
            },
        )
        return result.modified_count > 0
