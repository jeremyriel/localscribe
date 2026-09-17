"""The live console: an in-process event bus fanned out to WebSocket clients.

Design notes
------------
Events originate from two very different places: the asyncio event loop that
serves HTTP, and the plain worker thread that runs transcription. So the bus
accepts events from any thread and marshals them onto the loop before touching
any asyncio object.

A ring buffer of recent events is replayed when a client connects, so opening
the console panel part-way through a long job shows the history rather than an
empty box.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

from .config import PATHS, app_version

RING_SIZE = 2000
HEARTBEAT_SECONDS = 2.0

# Severity ordering used by the UI filter.
LEVELS = ("debug", "info", "step", "stat", "warn", "error", "success", "heartbeat")


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def _build_file_logger() -> logging.Logger:
    PATHS.logs.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("localscribe.console")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    if not logger.handlers:
        handler = RotatingFileHandler(
            PATHS.logs / "localscribe.log",
            maxBytes=2_000_000,
            backupCount=5,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-8s %(message)s")
        )
        logger.addHandler(handler)
    return logger


class ConsoleBus:
    """Thread-safe publish/subscribe with replay and a liveness heartbeat."""

    def __init__(self) -> None:
        self._ring: deque = deque(maxlen=RING_SIZE)
        self._subscribers: set[asyncio.Queue] = set()
        self._lock = threading.RLock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._seq = 0
        self._started = time.monotonic()
        self._logger = _build_file_logger()
        self._heartbeat_task: asyncio.Task | None = None
        self._state_providers: dict = {}

    # -- lifecycle ---------------------------------------------------------

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def register_state_provider(self, name: str, fn) -> None:
        """Register a zero-argument callable whose result rides the heartbeat."""
        self._state_providers[name] = fn

    def start_heartbeat(self) -> None:
        if self._heartbeat_task is None or self._heartbeat_task.done():
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def stop_heartbeat(self) -> None:
        task = self._heartbeat_task
        self._heartbeat_task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    @property
    def uptime(self) -> float:
        return time.monotonic() - self._started

    # -- emission ----------------------------------------------------------

    def emit(self, level: str, message: str, **data) -> dict:
        """Publish one event. Safe to call from any thread."""
        with self._lock:
            self._seq += 1
            event = {
                "seq": self._seq,
                "ts": _now_iso(),
                "t": round(self.uptime, 3),
                "level": level if level in LEVELS else "info",
                "message": message,
                "data": data or {},
            }
            # The heartbeat is deliberately kept out of the replay ring: it is
            # pure liveness signal and would otherwise crowd out real history.
            if level != "heartbeat":
                self._ring.append(event)

        if level != "heartbeat":
            self._logger.log(self._py_level(level), self._flatten(event))

        loop = self._loop
        if loop is None:
            return event
        try:
            if threading.current_thread() is threading.main_thread() and loop.is_running():
                try:
                    asyncio.get_running_loop()
                    self._fanout(event)
                    return event
                except RuntimeError:
                    pass
            loop.call_soon_threadsafe(self._fanout, event)
        except RuntimeError:
            # Loop already closed (shutdown race): the file log still has it.
            pass
        return event

    # Convenience wrappers, so call sites read as prose.
    def debug(self, message: str, **data) -> dict:
        return self.emit("debug", message, **data)

    def info(self, message: str, **data) -> dict:
        return self.emit("info", message, **data)

    def step(self, message: str, **data) -> dict:
        return self.emit("step", message, **data)

    def stat(self, message: str, **data) -> dict:
        return self.emit("stat", message, **data)

    def warn(self, message: str, **data) -> dict:
        return self.emit("warn", message, **data)

    def error(self, message: str, **data) -> dict:
        return self.emit("error", message, **data)

    def success(self, message: str, **data) -> dict:
        return self.emit("success", message, **data)

    @staticmethod
    def _py_level(level: str) -> int:
        return {
            "debug": logging.DEBUG,
            "warn": logging.WARNING,
            "error": logging.ERROR,
        }.get(level, logging.INFO)

    @staticmethod
    def _flatten(event: dict) -> str:
        data = event.get("data") or {}
        if not data:
            return f"[{event['level']}] {event['message']}"
        bits = " ".join(f"{k}={v}" for k, v in data.items())
        return f"[{event['level']}] {event['message']} | {bits}"

    def _fanout(self, event: dict) -> None:
        dead = []
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                dead.append(queue)
        for queue in dead:
            self._subscribers.discard(queue)

    # -- subscription ------------------------------------------------------

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def history(self, since: int = 0, limit: int = RING_SIZE) -> list:
        with self._lock:
            items = [e for e in self._ring if e["seq"] > since]
        return items[-limit:]

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    # -- heartbeat ---------------------------------------------------------

    def heartbeat_payload(self) -> dict:
        payload = {
            "uptime": round(self.uptime, 1),
            "version": app_version(),
            "clients": self.subscriber_count,
            "events": self._seq,
        }
        for name, fn in self._state_providers.items():
            try:
                payload[name] = fn()
            except Exception as exc:  # a broken provider must not stop the pulse
                payload[name] = {"error": str(exc)}
        return payload

    async def _heartbeat_loop(self) -> None:
        while True:
            try:
                self.emit("heartbeat", "alive", **self.heartbeat_payload())
                await asyncio.sleep(HEARTBEAT_SECONDS)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.emit("warn", f"Heartbeat hiccup: {exc}")
                await asyncio.sleep(HEARTBEAT_SECONDS)


CONSOLE = ConsoleBus()


# ---------------------------------------------------------------------------
# Small formatting helpers shared by the console call sites
# ---------------------------------------------------------------------------

def fmt_duration(seconds: float | None) -> str:
    """Human duration: 5.2s, 3m 04s, 1h 12m 09s."""
    if seconds is None:
        return "unknown"
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(round(seconds)), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    return f"{minutes}m {secs:02d}s"


def fmt_timestamp(seconds: float, millis: bool = True) -> str:
    """hh:mm:ss.mmm, the form used in transcripts and caption files."""
    seconds = max(0.0, float(seconds))
    hours, rem = divmod(seconds, 3600.0)
    minutes, secs = divmod(rem, 60.0)
    if millis:
        return f"{int(hours):02d}:{int(minutes):02d}:{secs:06.3f}"
    return f"{int(hours):02d}:{int(minutes):02d}:{int(secs):02d}"


def fmt_bytes(count: float | None) -> str:
    if count is None:
        return "unknown"
    step = 1024.0
    value = float(count)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < step:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= step
    return f"{value:.1f} PB"
