"""Retry with exponential backoff for transient API failures.

Wraps async callables. Distinguishes between:
  - Retriable: 429 rate limit, 502/503/504 upstream errors, connection/timeout errors
  - Fatal: 400/401/403/404 (caller bug)

Usage:
    @retry_on_transient(tries=3, base=2.0)
    async def call_api(...):
        ...
"""

import asyncio
import functools
import random
from typing import Awaitable, Callable, TypeVar

import httpx

T = TypeVar("T")

RETRIABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}
RETRIABLE_EXCEPTIONS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
)


def retry_on_transient(
    tries: int = 3,
    base: float = 2.0,
    max_wait: float = 30.0,
) -> Callable:
    """Decorator — retries an async callable on transient failures.

    - RuntimeError messages containing 'failed (429)', 'failed (502)' etc. → retry
    - httpx connection / timeout errors → retry
    - Everything else → raise immediately
    - Between tries: sleep base^attempt + jitter, capped at max_wait
    """
    def decorator(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @functools.wraps(fn)
        async def wrapped(*args, **kwargs):
            last_exc: Exception | None = None
            for attempt in range(tries):
                try:
                    return await fn(*args, **kwargs)
                except RetryableError as e:
                    last_exc = e
                except RuntimeError as e:
                    # Our clients raise RuntimeError with status codes in the message
                    msg = str(e)
                    is_retriable = any(f"({code})" in msg for code in RETRIABLE_STATUS)
                    if not is_retriable:
                        raise
                    last_exc = e
                except RETRIABLE_EXCEPTIONS as e:
                    last_exc = e
                # Wait before next attempt
                if attempt < tries - 1:
                    raw_delay = base ** attempt
                    delay = min(max_wait, raw_delay + random.uniform(0, raw_delay * 0.25))
                    await asyncio.sleep(delay)
            # Out of tries
            if last_exc is None:
                raise RuntimeError(f"{fn.__name__} exhausted {tries} retry attempts without raising")
            raise last_exc
        return wrapped
    return decorator


class RetryableError(Exception):
    """Raise this from within a retried function to force another attempt."""
    pass
