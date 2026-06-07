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


def stop():
    """Stop the asyncio event loop."""
    global _loop, _thread
    if _loop is None:
        return
    _loop.call_soon_threadsafe(_loop.stop)
    _thread.join(timeout=5)
    _loop = None
    _thread = None
