import asyncio
import logging
import time
from typing import Optional
from urllib.parse import urlparse

import httpx

logger = logging.getLogger("quotico.http_client")

# Retryable HTTP status codes
_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


class CircuitBreaker:
    """Simple circuit breaker for external API calls."""

    def __init__(self, failure_threshold: int = 3, recovery_timeout: int = 300):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.failure_count = 0
        self.last_failure_time: Optional[float] = None
        self.is_open = False

    def record_success(self) -> None:
        self.failure_count = 0
        self.is_open = False

    def record_failure(self) -> None:
        self.failure_count += 1
        self.last_failure_time = time.time()
        if self.failure_count >= self.failure_threshold:
            self.is_open = True
            logger.warning(
                "Circuit breaker OPEN after %d failures", self.failure_count
            )

    def can_attempt(self) -> bool:
        if not self.is_open:
            return True
        # Allow retry after recovery timeout (half-open)
        if self.last_failure_time and (
            time.time() - self.last_failure_time > self.recovery_timeout
        ):
            logger.info("Circuit breaker half-open, allowing retry")
            return True
        return False


def _parse_retry_after(response: httpx.Response) -> Optional[float]:
    """Extract wait time from Retry-After or X-RateLimit-Retry-After headers."""
    for header in ("retry-after", "x-ratelimit-retry-after"):
        value = response.headers.get(header)
        if value is None:
            continue
        try:
            return float(value)
        except (ValueError, TypeError):
            continue
    return None


def _safe_url(url: str) -> str:
    """Strip query params (may contain API keys) for safe logging."""
    parsed = urlparse(str(url))
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


class ResilientClient:
    """httpx.AsyncClient wrapper with retry, exponential backoff, and circuit breaker."""

    def __init__(
        self,
        name: str,
        timeout: float = 15.0,
        max_retries: int = 3,
        base_delay: float = 10.0,
    ):
        self._client = httpx.AsyncClient(timeout=timeout)
        self._name = name
        self._max_retries = max_retries
        self._base_delay = base_delay
        self.circuit = CircuitBreaker()

    async def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Execute an HTTP request with retry/backoff on transient failures."""
        last_exc: Optional[Exception] = None
        last_resp: Optional[httpx.Response] = None

        for attempt in range(self._max_retries + 1):
            try:
                resp = await self._client.request(method, url, **kwargs)

                if resp.status_code not in _RETRYABLE_STATUSES:
                    return resp

                # Retryable status — log and maybe retry
                last_resp = resp

                if resp.status_code == 429:
                    logger.warning(
                        "[%s] Rate limited (429) on %s %s (attempt %d/%d)",
                        self._name, method, _safe_url(url),
                        attempt + 1, self._max_retries + 1,
                    )
                else:
                    logger.warning(
                        "[%s] Server error %d on %s %s (attempt %d/%d)",
                        self._name, resp.status_code, method, _safe_url(url),
                        attempt + 1, self._max_retries + 1,
                    )

                if attempt < self._max_retries:
                    delay = _parse_retry_after(resp)
                    if delay is None:
                        delay = self._base_delay * (2 ** attempt)
                    # Cap delay at 60s
                    delay = min(delay, 60.0)
                    await asyncio.sleep(delay)

            except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as exc:
                last_exc = exc
                logger.warning(
                    "[%s] Network error on %s %s (attempt %d/%d): %s",
                    self._name, method, _safe_url(url),
                    attempt + 1, self._max_retries + 1, exc,
                )
                if attempt < self._max_retries:
                    delay = self._base_delay * (2 ** attempt)
                    await asyncio.sleep(delay)

        # All retries exhausted
        if last_resp is not None:
            logger.error(
                "[%s] All %d attempts failed for %s %s (last status: %d)",
                self._name, self._max_retries + 1, method, _safe_url(url),
                last_resp.status_code,
            )
            return last_resp

        # Network error on all attempts
        logger.error(
            "[%s] All %d attempts failed for %s %s: %s",
            self._name, self._max_retries + 1, method, _safe_url(url), last_exc,
        )
        raise last_exc  # type: ignore[misc]

    async def get(self, url: str, **kwargs) -> httpx.Response:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs) -> httpx.Response:
        return await self.request("POST", url, **kwargs)

    async def aclose(self) -> None:
        await self._client.aclose()
