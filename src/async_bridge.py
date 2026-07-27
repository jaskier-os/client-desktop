"""Bridge between sync/threaded code and asyncio (for aiortc WebRTC)."""

import asyncio
import threading
import logging

log = logging.getLogger(__name__)

_loop = None
_thread = None


def start():
    """Start the asyncio event loop in a background daemon thread."""
    global _loop, _thread
    if _loop is not None:
        return
    _loop = asyncio.new_event_loop()
    _thread = threading.Thread(target=_run, daemon=True, name="asyncio-bridge")
    _thread.start()
    log.info("Asyncio bridge started")


def _run():
    asyncio.set_event_loop(_loop)
    _loop.run_forever()


def run_coro(coro):
    """Schedule a coroutine on the asyncio loop from any thread. Returns a concurrent.futures.Future."""
    if _loop is None:
        raise RuntimeError("Asyncio bridge not started")
    return asyncio.run_coroutine_threadsafe(coro, _loop)


def stop(drain_timeout=3.0):
    """Stop the asyncio event loop, letting pending work finish first.

    Stopping the loop outright abandons in-flight tasks -- including the peer.close()
    calls scheduled during shutdown, which is how a parec capture and its UDP sockets
    could survive the app exiting. Give them a bounded chance to run.
    """
    global _loop, _thread
    if _loop is None:
        return

    async def _drain():
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if not pending:
            return
        log.info("Asyncio bridge: draining %d pending task(s)", len(pending))
        _, still_pending = await asyncio.wait(pending, timeout=drain_timeout)
        for task in still_pending:
            task.cancel()
        if still_pending:
            await asyncio.gather(*still_pending, return_exceptions=True)

    try:
        asyncio.run_coroutine_threadsafe(_drain(), _loop).result(timeout=drain_timeout + 2)
    except Exception as e:
        log.warning("Asyncio bridge: drain incomplete: %s", e)

    _loop.call_soon_threadsafe(_loop.stop)
    _thread.join(timeout=5)
    _loop = None
    _thread = None
