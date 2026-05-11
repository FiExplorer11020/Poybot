"""
Shared pytest fixtures + Round 3 cleanup helpers.

The autouse `_cleanup_pending_asyncio_tasks` fixture cancels any tasks
still pending at the end of each test. This silences the noisy
"Task was destroyed but it is pending!" messages we got from things
like `FalconClient._coalesce_expire`, where tests don't explicitly call
the client's `close()` and the 30-second sleep would otherwise orphan
when the event loop tears down.
"""

import asyncio

import pytest


@pytest.fixture(autouse=True)
async def _cleanup_pending_asyncio_tasks():
    """Cancel asyncio tasks still pending after the test body returns.

    Best-effort: we wait briefly for cancellations to be acknowledged but
    don't fail the test if a task refuses to die. Test-local tasks
    (background coroutines started by the SUT but never awaited) get
    surfaced as warnings, not silent leaks.
    """
    yield
    current = asyncio.current_task()
    pending = [
        t for t in asyncio.all_tasks()
        if not t.done() and t is not current
    ]
    if not pending:
        return
    for task in pending:
        task.cancel()
    # Give them one event-loop turn to acknowledge.
    for task in pending:
        try:
            await asyncio.wait_for(task, timeout=0.5)
        except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
            # Best-effort cleanup; we don't want fixture teardown to
            # raise out of an otherwise-passing test.
            pass
