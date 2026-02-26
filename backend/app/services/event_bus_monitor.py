"""
backend/app/services/event_bus_monitor.py

Purpose:
    Health monitor for qbus (event bus). Periodically records bus stats, stores
    time-series snapshots, and evaluates alert thresholds for admin monitoring.

Dependencies:
    - app.database
    - app.config
    - app.services.event_bus
    - app.utils
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any

import app.database as _db
from app.config import settings
from app.services.event_bus import event_bus
from app.utils import ensure_utc, utcnow

logger = logging.getLogger("quotico.event_bus_monitor")


class EventBusMonitor:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="event_bus_monitor")
        logger.info("Event bus monitor started")

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Event bus monitor stopped")

    async def _loop(self) -> None:
        interval = max(2, int(settings.QBUS_MONITOR_SAMPLING_SECONDS))
        while self._running:
            try:
                await self.record_stats()
            except Exception:
                logger.exception("Failed to record event bus stats")
            await asyncio.sleep(interval)

    async def record_stats(self) -> dict[str, Any]:
        now = utcnow()
        stats = event_bus.stats()
        threshold_result = self.check_thresholds(stats)
        per_handler = []
        for name, detail in stats.get("per_handler", {}).items():
            per_handler.append(
                {
                    "name": name,
                    "queue_depth": int(detail.get("queue_depth", 0)),
                    "queue_limit": int(detail.get("queue_limit", stats.get("handler_queue_limit", 1) or 1)),
                    "concurrency": int(detail.get("concurrency", 1)),
                    "handled_total": int(detail.get("handled_total", 0)),
                    "failed_total": int(detail.get("failed_total", 0)),
                    "dropped_total": int(detail.get("dropped_total", 0)),
                }
            )
        per_event_type = []
        for name, detail in (stats.get("per_event_type") or {}).items():
            per_event_type.append(
                {
                    "name": name,
                    "published_total": int(detail.get("published_total", 0)),
                    "handled_total": int(detail.get("handled_total", 0)),
                    "failed_total": int(detail.get("failed_total", 0)),
                    "dropped_total": int(detail.get("dropped_total", 0)),
                }
            )
        per_source = []
        for name, detail in (stats.get("per_source") or {}).items():
            per_source.append(
                {
                    "name": name,
                    "published_total": int(detail.get("published_total", 0)),
                    "handled_total": int(detail.get("handled_total", 0)),
                    "failed_total": int(detail.get("failed_total", 0)),
                    "dropped_total": int(detail.get("dropped_total", 0)),
                }
            )

        snapshot = {
            "ts": now,
            "status_level": threshold_result["status_level"],
            "alerts": threshold_result["alerts"],
            "ingress": {
                "depth": int(stats.get("ingress_queue_depth", 0)),
                "limit": int(stats.get("ingress_queue_limit", 1)),
                "usage_pct": float(stats.get("ingress_queue_usage_pct", 0.0)),
                "max_depth_seen": int(stats.get("max_ingress_queue_depth_seen", 0)),
            },
            "rates_1m": {
                "published": float(stats.get("published_rate_1m", 0.0)),
                "handled": float(stats.get("handled_rate_1m", 0.0)),
                "failed": float(stats.get("failed_rate_1m", 0.0)),
                "dropped": float(stats.get("dropped_rate_1m", 0.0)),
            },
            "latency_ms": {
                "avg": float(stats.get("latency_ms", {}).get("avg", 0.0)),
                "p50": float(stats.get("latency_ms", {}).get("p50", 0.0)),
                "p95": float(stats.get("latency_ms", {}).get("p95", 0.0)),
            },
            "totals": {
                "published": int(stats.get("published_total", 0)),
                "handled": int(stats.get("handled_total", 0)),
                "failed": int(stats.get("failed_total", 0)),
                "dropped": int(stats.get("dropped_total", 0)),
            },
            "per_handler": per_handler,
            "per_event_type": per_event_type,
            "per_source": per_source,
            "recent_errors_count": len(stats.get("recent_errors", []) or []),
        }
        await _db.db.event_bus_stats.insert_one(snapshot)
        return snapshot

    def check_thresholds(self, stats: dict[str, Any]) -> dict[str, Any]:
        alerts: list[dict[str, Any]] = []
        level = "green"

        ingress_usage = float(stats.get("ingress_queue_usage_pct", 0.0))
        if ingress_usage >= settings.QBUS_ALERT_QUEUE_CRIT_PCT:
            level = "red"
            alerts.append({"code": "ingress_queue_high", "severity": "critical", "value": ingress_usage})
        elif ingress_usage >= settings.QBUS_ALERT_QUEUE_WARN_PCT:
            if level != "red":
                level = "yellow"
            alerts.append({"code": "ingress_queue_high", "severity": "warning", "value": ingress_usage})

        handler_limit = int(stats.get("handler_queue_limit", 1) or 1)
        for handler, depth in (stats.get("handler_queue_depth") or {}).items():
            usage = (float(depth) / float(handler_limit)) * 100.0
            if usage >= settings.QBUS_ALERT_QUEUE_CRIT_PCT:
                level = "red"
                alerts.append({"code": "handler_queue_high", "severity": "critical", "handler": handler, "value": usage})
            elif usage >= settings.QBUS_ALERT_QUEUE_WARN_PCT:
                if level != "red":
                    level = "yellow"
                alerts.append({"code": "handler_queue_high", "severity": "warning", "handler": handler, "value": usage})

        handled_rate = float(stats.get("handled_rate_1m", 0.0))
        failed_rate = float(stats.get("failed_rate_1m", 0.0))
        failure_ratio = (failed_rate / handled_rate) if handled_rate > 0 else (1.0 if failed_rate > 0 else 0.0)
        if failure_ratio >= settings.QBUS_ALERT_FAILED_RATE_CRIT:
            level = "red"
            alerts.append({"code": "failed_rate_high", "severity": "critical", "value": round(failure_ratio, 4)})
        elif failure_ratio >= settings.QBUS_ALERT_FAILED_RATE_WARN:
            if level != "red":
                level = "yellow"
            alerts.append({"code": "failed_rate_high", "severity": "warning", "value": round(failure_ratio, 4)})

        dropped_per_min = float(stats.get("dropped_rate_1m", 0.0)) * 60.0
        if dropped_per_min >= settings.QBUS_ALERT_DROPPED_CRIT_PER_MIN:
            level = "red"
            alerts.append({"code": "dropped_events", "severity": "critical", "value": round(dropped_per_min, 2)})
        elif dropped_per_min >= settings.QBUS_ALERT_DROPPED_WARN_PER_MIN:
            if level != "red":
                level = "yellow"
            alerts.append({"code": "dropped_events", "severity": "warning", "value": round(dropped_per_min, 2)})

        p95_latency = float(stats.get("latency_ms", {}).get("p95", 0.0))
        if p95_latency >= settings.QBUS_ALERT_LATENCY_P95_CRIT_MS:
            level = "red"
            alerts.append({"code": "latency_p95_high", "severity": "critical", "value": p95_latency})
        elif p95_latency >= settings.QBUS_ALERT_LATENCY_P95_WARN_MS:
            if level != "red":
                level = "yellow"
            alerts.append({"code": "latency_p95_high", "severity": "warning", "value": p95_latency})

        for alert in alerts:
            if alert["severity"] == "critical":
                logger.error("QBus alert code=%s detail=%s", alert["code"], alert)
            else:
                logger.warning("QBus alert code=%s detail=%s", alert["code"], alert)
        return {"status_level": level, "alerts": alerts}

    async def get_recent_stats(self, window: str = "24h", bucket_seconds: int = 10) -> dict[str, Any]:
        window_delta = self._parse_window(window)
        since = utcnow() - window_delta
        docs = await _db.db.event_bus_stats.find({"ts": {"$gte": since}}).sort("ts", 1).to_list(length=50_000)
        if not docs:
            return {"window": window, "bucket_seconds": bucket_seconds, "series": []}

        bucket = max(10, int(bucket_seconds))
        grouped: dict[int, dict[str, Any]] = {}
        for doc in docs:
            ts = ensure_utc(doc.get("ts"))
            key = int(ts.timestamp() // bucket) * bucket
            row = grouped.setdefault(
                key,
                {
                    "ts": key,
                    "ingress_depth": 0.0,
                    "failed_rate_1m": 0.0,
                    "dropped_rate_1m": 0.0,
                    "latency_p95": 0.0,
                    "status_level": "green",
                    "count": 0,
                },
            )
            row["ingress_depth"] += float(doc.get("ingress", {}).get("depth", 0))
            row["failed_rate_1m"] += float(doc.get("rates_1m", {}).get("failed", 0))
            row["dropped_rate_1m"] += float(doc.get("rates_1m", {}).get("dropped", 0))
            row["latency_p95"] += float(doc.get("latency_ms", {}).get("p95", 0))
            row["count"] += 1
            if doc.get("status_level") == "red":
                row["status_level"] = "red"
            elif doc.get("status_level") == "yellow" and row["status_level"] != "red":
                row["status_level"] = "yellow"

        series = []
        for key in sorted(grouped.keys()):
            row = grouped[key]
            count = max(1, row["count"])
            series.append(
                {
                    "ts": ensure_utc(datetime.fromtimestamp(row["ts"], tz=timezone.utc)).isoformat(),
                    "ingress_depth": round(row["ingress_depth"] / count, 3),
                    "failed_rate_1m": round(row["failed_rate_1m"] / count, 6),
                    "dropped_rate_1m": round(row["dropped_rate_1m"] / count, 6),
                    "latency_p95": round(row["latency_p95"] / count, 2),
                    "status_level": row["status_level"],
                }
            )
        return {"window": window, "bucket_seconds": bucket, "series": series}

    async def get_handler_rollups(self, window: str = "1h") -> list[dict[str, Any]]:
        window_delta = self._parse_window(window)
        since = utcnow() - window_delta
        docs = await _db.db.event_bus_stats.find({"ts": {"$gte": since}}).sort("ts", 1).to_list(length=50_000)
        if not docs:
            return []

        rollups: dict[str, dict[str, Any]] = defaultdict(
            lambda: {
                "name": "",
                "concurrency": 1,
                "queue_depth": 0,
                "queue_limit": 1,
                "handled_1h": 0,
                "failed_1h": 0,
                "dropped_1h": 0,
                "_first_totals": None,
                "_last_totals": None,
            }
        )

        for doc in docs:
            for item in doc.get("per_handler", []) or []:
                name = str(item.get("name") or "")
                if not name:
                    continue
                row = rollups[name]
                row["name"] = name
                row["concurrency"] = int(item.get("concurrency", 1))
                row["queue_depth"] = int(item.get("queue_depth", 0))
                row["queue_limit"] = int(item.get("queue_limit", 1))
                totals = (
                    int(item.get("handled_total", 0)),
                    int(item.get("failed_total", 0)),
                    int(item.get("dropped_total", 0)),
                )
                if row["_first_totals"] is None:
                    row["_first_totals"] = totals
                row["_last_totals"] = totals

        out: list[dict[str, Any]] = []
        for name, row in rollups.items():
            first_h, first_f, first_d = row["_first_totals"] or (0, 0, 0)
            last_h, last_f, last_d = row["_last_totals"] or (0, 0, 0)
            out.append(
                {
                    "name": name,
                    "concurrency": row["concurrency"],
                    "queue_depth": row["queue_depth"],
                    "queue_limit": row["queue_limit"],
                    "queue_usage_pct": round((row["queue_depth"] / max(1, row["queue_limit"])) * 100, 2),
                    "handled_1h": max(0, last_h - first_h),
                    "failed_1h": max(0, last_f - first_f),
                    "dropped_1h": max(0, last_d - first_d),
                }
            )
        out.sort(key=lambda x: x["name"])
        return out

    async def get_current_health(self) -> dict[str, Any]:
        stats = event_bus.stats()
        threshold_result = self.check_thresholds(stats)
        rollups_1h = await self._get_breakdown_rollups(window="1h")
        return {
            "status_level": threshold_result["status_level"],
            "alerts": threshold_result["alerts"],
            "stats": stats,
            "recent_errors": stats.get("recent_errors", []),
            "per_source_1h": rollups_1h["per_source"],
            "per_event_type_1h": rollups_1h["per_event_type"],
        }

    async def _get_breakdown_rollups(self, *, window: str) -> dict[str, list[dict[str, Any]]]:
        window_delta = self._parse_window(window)
        since = utcnow() - window_delta
        docs = await _db.db.event_bus_stats.find({"ts": {"$gte": since}}).sort("ts", 1).to_list(length=50_000)
        if not docs:
            return {"per_source": [], "per_event_type": []}

        def _collect_delta(group_key: str) -> list[dict[str, Any]]:
            rows: dict[str, dict[str, Any]] = defaultdict(
                lambda: {
                    "name": "",
                    "_first": None,
                    "_last": None,
                }
            )
            for doc in docs:
                for item in doc.get(group_key, []) or []:
                    name = str(item.get("name") or "")
                    if not name:
                        continue
                    entry = rows[name]
                    entry["name"] = name
                    current = (
                        int(item.get("published_total", 0)),
                        int(item.get("handled_total", 0)),
                        int(item.get("failed_total", 0)),
                        int(item.get("dropped_total", 0)),
                    )
                    if entry["_first"] is None:
                        entry["_first"] = current
                    entry["_last"] = current
            out: list[dict[str, Any]] = []
            for name, entry in rows.items():
                first_pub, first_handled, first_failed, first_dropped = entry["_first"] or (0, 0, 0, 0)
                last_pub, last_handled, last_failed, last_dropped = entry["_last"] or (0, 0, 0, 0)
                out.append(
                    {
                        "name": name,
                        "published_1h": max(0, last_pub - first_pub),
                        "handled_1h": max(0, last_handled - first_handled),
                        "failed_1h": max(0, last_failed - first_failed),
                        "dropped_1h": max(0, last_dropped - first_dropped),
                    }
                )
            out.sort(key=lambda x: x["name"])
            return out

        return {
            "per_source": _collect_delta("per_source"),
            "per_event_type": _collect_delta("per_event_type"),
        }

    @staticmethod
    def _parse_window(window: str) -> timedelta:
        value = str(window or "24h").strip().lower()
        if value == "1h":
            return timedelta(hours=1)
        if value == "6h":
            return timedelta(hours=6)
        return timedelta(hours=24)


event_bus_monitor = EventBusMonitor()
