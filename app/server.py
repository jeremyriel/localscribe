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
from datetime import datetime

from .config import APP_NAME, HOST, PATHS, app_version, resolve_port


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
        try:
            probe.bind((HOST, port))
            return True
        except OSError:
            return False


def main() -> int:
    import uvicorn

    port = resolve_port()

    if not port_is_free(port):
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
