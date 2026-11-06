import asyncio
from collections.abc import Awaitable, Callable

from better.domain.errors import BetterError


async def retry(operation: Callable[[], Awaitable], *, retries: int = 1, delay: float = 0.05):
    for attempt in range(retries + 1):
        try:
            return await operation()
        except BetterError as error:
            if not error.detail.retryable or attempt == retries:
                raise
            await asyncio.sleep(delay * 2**attempt)
