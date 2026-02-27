"""
backend/app/services/event_bus.py

Purpose:
    Lightweight in-memory event bus for process-local reactive workflows.
    Provides async publish/subscribe with per-handler worker queues.

Dependencies:
    - asyncio
    - app.config
    - app.services.event_models
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from app.config import settings
from app.services.event_models import BaseEvent, normalize_event_time
from app.utils import ensure_utc, utcnow

logger = logging.getLogger("quotico.event_bus")

AsyncEventHandler = Callable[[BaseEvent], Awaitable[None]]
_RATE_WINDOW_SECONDS = 60.0
_METRIC_KEYS = ("published", "handled", "failed", "dropped")


@dataclass
class _Subscription:
    event_type: str
    handler_name: str
    handler: AsyncEventHandler
    concurrency: int
    queue: asyncio.Queue[BaseEvent]
    workers: list[asyncio.Task]
    handled_total: int = 0
    failed_total: int = 0
    dropped_total: int = 0
    max_queue_depth_seen: int = 0


class InMemoryEventBus:
    def __init__(
        self,
        *,
        ingress_maxsize: int,
        handler_maxsize: int,
        default_concurrency: int,
        error_buffer_size: int,
    ) -> None:
        self._ingress_maxsize = max(1, int(ingress_maxsize))
        self._handler_maxsize = max(1, int(handler_maxsize))
        self._default_concurrency = max(1, int(default_concurrency))
        self._error_buffer_size = max(1, int(error_buffer_size))

        self._ingress: asyncio.Queue[BaseEvent] = asyncio.Queue(maxsize=self._ingress_maxsize)
        self._subscriptions: dict[str, list[_Subscription]] = defaultdict(list)
        self._dispatcher_task: asyncio.Task | None = None
        self._running = False
        self._lock = asyncio.Lock()

        self._published = 0
        self._handled = 0
        self._failed = 0
        self._dropped = 0
        self._max_ingress_depth_seen = 0
        self._errors: deque[dict[str, Any]] = deque(maxlen=self._error_buffer_size)
        self._published_rate: deque[tuple[float, int]] = deque()
        self._handled_rate: deque[tuple[float, int]] = deque()
        self._failed_rate: deque[tuple[float, int]] = deque()
        self._dropped_rate: deque[tuple[float, int]] = deque()
        self._lag_samples: deque[tuple[float, int]] = deque()
        self._per_event_type_totals: dict[str, dict[str, int]] = defaultdict(self._empty_totals)
        self._per_source_totals: dict[str, dict[str, int]] = defaultdict(self._empty_totals)
        self._per_event_type_rates: dict[str, dict[str, deque[tuple[float, int]]]] = defaultdict(self._empty_rate_buckets)
        self._per_source_rates: dict[str, dict[str, deque[tuple[float, int]]]] = defaultdict(self._empty_rate_buckets)

    async def start(self) -> None:
        async with self._lock:
            if self._running:
                return
            self._running = True
            for subs in self._subscriptions.values():
                for sub in subs:
                    if not sub.workers:
                        sub.workers.extend(self._spawn_workers(sub, sub.concurrency))
            self._dispatcher_task = asyncio.create_task(self._dispatch_loop(), name="event_bus_dispatcher")
            logger.info("Event bus started")

    async def stop(self) -> None:
        async with self._lock:
            if not self._running:
                return
            self._running = False
            if self._dispatcher_task is not None:
                self._dispatcher_task.cancel()
                try:
                    await self._dispatcher_task
                except asyncio.CancelledError:
                    pass
                self._dispatcher_task = None
            for subs in self._subscriptions.values():
                for sub in subs:
                    for worker in sub.workers:
                        worker.cancel()
            for subs in self._subscriptions.values():
                for sub in subs:
                    for worker in sub.workers:
                        try:
                            await worker
                        except asyncio.CancelledError:
                            pass
            for subs in self._subscriptions.values():
                for sub in subs:
                    sub.workers.clear()
            logger.info("Event bus stopped")

    def subscribe(
        self,
        event_type: str,
        handler: AsyncEventHandler,
        *,
        handler_name: str,
        concurrency: int = 1,
    ) -> None:
        worker_count = max(1, int(concurrency or self._default_concurrency))
        queue = asyncio.Queue[BaseEvent](maxsize=self._handler_maxsize)
        sub = _Subscription(
            event_type=event_type,
            handler_name=handler_name,
            handler=handler,
            concurrency=worker_count,
            queue=queue,
            workers=[],
        )
        self._subscriptions[event_type].append(sub)
        if self._running:
            sub.workers.extend(self._spawn_workers(sub, worker_count))

    async def publish(self, event: BaseEvent) -> None:
        normalized = normalize_event_time(event)
        try:
            self._ingress.put_nowait(normalized)
            self._published += 1
            self._record_rate(self._published_rate, 1)
            self._record_breakdown("published", normalized.event_type, normalized.source, 1)
            self._max_ingress_depth_seen = max(self._max_ingress_depth_seen, self._ingress.qsize())
        except asyncio.QueueFull:
            self._dropped += 1
            self._record_rate(self._dropped_rate, 1)
            self._record_breakdown("dropped", normalized.event_type, normalized.source, 1)
            logger.warning("Event bus ingress queue full; dropping event_type=%s", normalized.event_type)

    def stats(self) -> dict[str, Any]:
        now = utcnow()
        self._prune_rate_buffers(now.timestamp())
        self._prune_breakdown_rate_buffers(now.timestamp())
        handler_queue_depth: dict[str, int] = {}
        per_handler: dict[str, dict[str, Any]] = {}
        for event_type, subs in self._subscriptions.items():
            for sub in subs:
                key = f"{event_type}:{sub.handler_name}"
                depth = sub.queue.qsize()
                handler_queue_depth[key] = depth
                per_handler[key] = {
                    "event_type": event_type,
                    "name": sub.handler_name,
                    "concurrency": sub.concurrency,
                    "queue_depth": depth,
                    "queue_limit": self._handler_maxsize,
                    "queue_usage_pct": round((depth / self._handler_maxsize) * 100, 2),
                    "handled_total": sub.handled_total,
                    "failed_total": sub.failed_total,
                    "dropped_total": sub.dropped_total,
                    "max_queue_depth_seen": sub.max_queue_depth_seen,
                }
        ingress_depth = self._ingress.qsize()
        return {
            "enabled": bool(settings.EVENT_BUS_ENABLED),
            "running": self._running,
            "published_total": self._published,
            "handled_total": self._handled,
            "failed_total": self._failed,
            "dropped_total": self._dropped,
            "published_rate_1m": self._rate_per_second(self._published_rate),
            "handled_rate_1m": self._rate_per_second(self._handled_rate),
            "failed_rate_1m": self._rate_per_second(self._failed_rate),
            "dropped_rate_1m": self._rate_per_second(self._dropped_rate),
            "ingress_queue_depth": ingress_depth,
            "ingress_queue_limit": self._ingress_maxsize,
            "ingress_queue_usage_pct": round((ingress_depth / self._ingress_maxsize) * 100, 2),
            "max_ingress_queue_depth_seen": self._max_ingress_depth_seen,
            "latency_ms": self._latency_summary(),
            "handler_queue_depth": handler_queue_depth,
            "handler_queue_limit": self._handler_maxsize,
            "per_handler": per_handler,
            "per_event_type": self._serialize_breakdown(self._per_event_type_totals, self._per_event_type_rates),
            "per_source": self._serialize_breakdown(self._per_source_totals, self._per_source_rates),
            "recent_errors": list(self._errors),
        }

    async def _dispatch_loop(self) -> None:
        while self._running:
            event = await self._ingress.get()
            subs = self._subscriptions.get(event.event_type, [])
            for sub in subs:
                try:
                    sub.queue.put_nowait(event)
                    sub.max_queue_depth_seen = max(sub.max_queue_depth_seen, sub.queue.qsize())
                except asyncio.QueueFull:
                    self._dropped += 1
                    sub.dropped_total += 1
                    self._record_rate(self._dropped_rate, 1)
                    self._record_breakdown("dropped", event.event_type, event.source, 1)
                    logger.warning(
                        "Event bus handler queue full; dropping event_type=%s handler=%s",
                        event.event_type,
                        sub.handler_name,
                    )

    def _spawn_workers(self, sub: _Subscription, worker_count: int) -> list[asyncio.Task]:
        workers: list[asyncio.Task] = []
        for idx in range(worker_count):
            name = f"event_bus_{sub.event_type}_{sub.handler_name}_{idx}"
            workers.append(asyncio.create_task(self._handler_loop(sub), name=name))
        return workers

    async def _handler_loop(self, sub: _Subscription) -> None:
        while self._running:
            event = await sub.queue.get()
            started_at = utcnow()
            processing_lag_ms = int((started_at - ensure_utc(event.occurred_at)).total_seconds() * 1000)
            self._record_lag(processing_lag_ms)
            try:
                await sub.handler(event)
                self._handled += 1
                sub.handled_total += 1
                self._record_rate(self._handled_rate, 1)
                self._record_breakdown("handled", event.event_type, event.source, 1)
            except Exception as exc:
                self._failed += 1
                sub.failed_total += 1
                self._record_rate(self._failed_rate, 1)
                self._record_breakdown("failed", event.event_type, event.source, 1)
                error = {
                    "event_id": event.event_id,
                    "event_type": event.event_type,
                    "source": event.source,
                    "handler_name": sub.handler_name,
                    "correlation_id": event.correlation_id,
                    "ts": utcnow().isoformat(),
                    "processing_lag_ms": processing_lag_ms,
                    "error": str(exc),
                }
                self._errors.append(error)
                logger.error(
                    "Event handler failed event_id=%s event_type=%s handler=%s correlation_id=%s error=%s",
                    event.event_id,
                    event.event_type,
                    sub.handler_name,
                    event.correlation_id,
                    str(exc),
                    exc_info=True,
                )

    def _record_rate(self, bucket: deque[tuple[float, int]], delta: int) -> None:
        now_ts = utcnow().timestamp()
        bucket.append((now_ts, int(delta)))
        self._prune_bucket(bucket, now_ts)

    def _prune_bucket(self, bucket: deque[tuple[float, int]], now_ts: float) -> None:
        min_ts = now_ts - _RATE_WINDOW_SECONDS
        while bucket and bucket[0][0] < min_ts:
            bucket.popleft()

    def _prune_rate_buffers(self, now_ts: float) -> None:
        self._prune_bucket(self._published_rate, now_ts)
        self._prune_bucket(self._handled_rate, now_ts)
        self._prune_bucket(self._failed_rate, now_ts)
        self._prune_bucket(self._dropped_rate, now_ts)
        self._prune_bucket(self._lag_samples, now_ts)

    def _prune_breakdown_rate_buffers(self, now_ts: float) -> None:
        for bucket_map in self._per_event_type_rates.values():
            for metric in _METRIC_KEYS:
                self._prune_bucket(bucket_map[metric], now_ts)
        for bucket_map in self._per_source_rates.values():
            for metric in _METRIC_KEYS:
                self._prune_bucket(bucket_map[metric], now_ts)

    def _rate_per_second(self, bucket: deque[tuple[float, int]]) -> float:
        if not bucket:
            return 0.0
        total = sum(delta for _, delta in bucket)
        return round(total / _RATE_WINDOW_SECONDS, 4)

    def _record_lag(self, lag_ms: int) -> None:
        now_ts = utcnow().timestamp()
        self._lag_samples.append((now_ts, max(0, int(lag_ms))))
        self._prune_bucket(self._lag_samples, now_ts)

    def _latency_summary(self) -> dict[str, float]:
        if not self._lag_samples:
            return {"avg": 0.0, "p50": 0.0, "p95": 0.0}
        values = sorted(val for _, val in self._lag_samples)
        n = len(values)
        avg = round(sum(values) / n, 2)
        p50 = float(values[min(n - 1, int(0.50 * (n - 1)))])
        p95 = float(values[min(n - 1, int(0.95 * (n - 1)))])
        return {"avg": avg, "p50": p50, "p95": p95}

    @staticmethod
    def _empty_totals() -> dict[str, int]:
        return {metric: 0 for metric in _METRIC_KEYS}

    @staticmethod
    def _empty_rate_buckets() -> dict[str, deque[tuple[float, int]]]:
        return {metric: deque() for metric in _METRIC_KEYS}

    def _record_breakdown(self, metric: str, event_type: str, source: str, delta: int) -> None:
        if metric not in _METRIC_KEYS:
            return
        now_ts = utcnow().timestamp()
        event_key = str(event_type or "unknown")
        source_key = str(source or "unknown")
        self._per_event_type_totals[event_key][metric] += int(delta)
        self._per_source_totals[source_key][metric] += int(delta)
        self._per_event_type_rates[event_key][metric].append((now_ts, int(delta)))
        self._per_source_rates[source_key][metric].append((now_ts, int(delta)))
        self._prune_bucket(self._per_event_type_rates[event_key][metric], now_ts)
        self._prune_bucket(self._per_source_rates[source_key][metric], now_ts)

    def _serialize_breakdown(
        self,
        totals: dict[str, dict[str, int]],
        rates: dict[str, dict[str, deque[tuple[float, int]]]],
    ) -> dict[str, dict[str, float | int]]:
        serialized: dict[str, dict[str, float | int]] = {}
        keys = sorted(set(totals.keys()) | set(rates.keys()))
        for key in keys:
            total_row = totals.get(key, {})
            rate_row = rates.get(key, {})
            serialized[key] = {
                "published_total": int(total_row.get("published", 0)),
                "handled_total": int(total_row.get("handled", 0)),
                "failed_total": int(total_row.get("failed", 0)),
                "dropped_total": int(total_row.get("dropped", 0)),
                "published_rate_1m": self._rate_per_second(rate_row.get("published", deque())),
                "handled_rate_1m": self._rate_per_second(rate_row.get("handled", deque())),
                "failed_rate_1m": self._rate_per_second(rate_row.get("failed", deque())),
                "dropped_rate_1m": self._rate_per_second(rate_row.get("dropped", deque())),
            }
        return serialized


event_bus = InMemoryEventBus(
    ingress_maxsize=settings.EVENT_BUS_INGRESS_QUEUE_MAXSIZE,
    handler_maxsize=settings.EVENT_BUS_HANDLER_QUEUE_MAXSIZE,
    default_concurrency=settings.EVENT_BUS_HANDLER_DEFAULT_CONCURRENCY,
    error_buffer_size=settings.EVENT_BUS_ERROR_BUFFER_SIZE,
)
