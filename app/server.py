"""Server entrypoint: writes the pidfile, then runs uvicorn on the fixed port.

Run through the launchers (run.bat / run.command / run.sh), which handle the
virtual environment and single-instance enforcement first. Running this module
directly is supported for development:

    .venv/Scripts/python.exe -m app.server
"""

from __future__ import annotations

import atexit
import json
import os
import signal
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

from .config import APP_NAME, HOST, PATHS, app_version, resolve_port

# Must match bootstrap.py's APP_SIGNATURE - both sides use it to confirm a
# port holder really is Local Scribe before touching it.
APP_SIGNATURE = "localscribe/instance/v1"


def write_pidfile(port: int) -> None:
    PATHS.logs.mkdir(parents=True, exist_ok=True)
    PATHS.pidfile.write_text(
        json.dumps({
            "pid": os.getpid(),
            "port": port,
            "host": HOST,
            "version": app_version(),
            "started": datetime.now().isoformat(timespec="seconds"),
        }, indent=2),
        encoding="utf-8",
    )


def clear_pidfile() -> None:
    try:
        if not PATHS.pidfile.exists():
            return
        data = json.loads(PATHS.pidfile.read_text(encoding="utf-8"))
        # Only remove our own pidfile; a newer instance may have replaced it.
        if int(data.get("pid", -1)) == os.getpid():
            PATHS.pidfile.unlink(missing_ok=True)
    except Exception:
        pass


def port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        # SO_REUSEADDR matters here: any HTTP request this process (or the
        # identity probe below) makes to the port leaves a connection that,
        # once closed, can sit in TIME_WAIT for a long time afterward. A
        # plain bind() refuses the port for that whole window even though
        # nothing is actually listening any more; uvicorn's own real bind
        # sets this by default, so without it here this probe reports the
        # port busy for far longer than uvicorn itself actually would.
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((HOST, port))
            return True
        except OSError:
            return False


def _identify_port_holder(port: int, timeout: float = 1.5) -> dict | None:
    """Ask whatever holds the port whether it is Local Scribe. Never raises."""
    try:
        with urllib.request.urlopen(
            f"http://{HOST}:{port}/api/instance", timeout=timeout
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
        return None
    if payload.get("signature") != APP_SIGNATURE:
        return None
    return payload


def _terminate(pid: int) -> None:
    """Stop a stale/racing instance found holding the port, quickly.

    Unlike a normal Ctrl+C shutdown, this process is not going to be given
    time for a graceful drain: uvicorn installs its own SIGTERM handler
    once it starts serving, which runs a graceful-shutdown sequence that
    can take several seconds even with no active connections, and this
    function's whole job is to make the port bindable again as fast as
    possible. There is nothing unsaved to lose - all state is written to
    disk as it happens - so escalating to SIGKILL quickly is safe here.
    """
    if os.name == "nt":
        import subprocess
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, text=True)
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    for _ in range(8):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            return
        time.sleep(0.25)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def _reclaim_port(port: int) -> bool:
    """Make the port bindable, stopping a prior Local Scribe instance if needed.

    The launchers (run.bat/.command/.sh -> bootstrap.py) already stop an
    existing instance before getting here, but that check and this bind
    happen moments apart, so two overlapping launches (a double-click, or a
    launcher run again before the first one finished starting) can still
    both pass bootstrap's check and race for the port. This is the last
    point before the bind, so it is where that race actually gets resolved:
    if whatever is on the port identifies itself as Local Scribe over HTTP,
    it is stopped; a foreign process is left alone, same rule bootstrap.py
    follows.

    A port can also, briefly, refuse a fresh bind right after its previous
    listener closes even once nothing is listening any more and there is
    nothing left to identify or kill - a transitional OS state, not a real
    conflict. Rather than treat "nobody answered" as "give up immediately",
    this keeps giving the port a moment to become bindable on its own.
    """
    running = _identify_port_holder(port)
    if running is not None:
        pid = int(running.get("pid") or 0)
        if pid and pid != os.getpid():
            print(
                f"Another {APP_NAME} instance (PID {pid}) is already on "
                "this port; stopping it...",
                file=sys.stderr,
            )
            _terminate(pid)

    # Generous on purpose: this only ever runs in the rare collision case (a
    # normal single launch never enters this function at all, since the
    # port is already free), and a loaded machine can genuinely take a few
    # seconds to finish tearing down a prior process's model/HTTP/WebSocket
    # state before the OS lets the port bind again.
    for _ in range(40):
        if port_is_free(port):
            return True
        time.sleep(0.25)
    return False


def main() -> int:
    import uvicorn

    port = resolve_port()

    if not port_is_free(port) and not _reclaim_port(port):
        print(
            f"\n{APP_NAME} cannot start: {HOST}:{port} is already in use.\n"
            "Run the launcher (run.bat / run.command / run.sh) instead, which\n"
            "shuts down an existing instance before starting a new one.\n",
            file=sys.stderr,
        )
        return 1

    write_pidfile(port)
    atexit.register(clear_pidfile)

    def shutdown(signum, _frame):
        clear_pidfile()
        raise SystemExit(0)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, shutdown)
        except (ValueError, OSError):
            pass

    print(f"{APP_NAME} {app_version()} listening on http://{HOST}:{port}")
    print("Close this window or press Ctrl+C to stop.\n")

    uvicorn.run(
        "app.main:app",
        host=HOST,          # loopback only, never 0.0.0.0
        port=port,
        log_level="warning",   # the in-app console is the real log surface
        access_log=False,
        reload=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
