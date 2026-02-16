# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

import time
import functools
from typing import Callable

class RetryError(RuntimeError):
    pass


def retry(
    *,
    retries: int,
    delay: int,
    retry_on: tuple[type[Exception], ...] = (Exception,),
    on_retry: Callable[[int, Exception], None] | None = None,
):
    """
    Retry decorator for idempotent operations.

    retries: number of attempts
    delay: seconds between attempts
    retry_on: exception types to retry
    on_retry: callback(attempt, exception)
    """
    
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(1, retries + 1):
                try:
                    return fn(*args, **kwargs)
                except retry_on as exc:
                    last_exc = exc
                    if on_retry:
                        on_retry(attempt, exc)
                    if attempt == retries:
                        break
                    time.sleep(delay)
            raise RetryError(f"{fn.__name__} failed after {retries} retries") from last_exc
        return wrapper
    return decorator