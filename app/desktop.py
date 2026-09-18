"""Desktop entrypoint: runs the server in a background thread and shows it
in a native window via pywebview, instead of opening a browser tab.

Only used by ``bootstrap.py --desktop``, which the packaged Windows/macOS/
Linux installers (see packaging/) launch with. The ordinary run.command/
run.sh/run.bat path keeps using ``python -m app.server`` directly and
opening the system browser - nothing there changes. Single-instance
enforcement, the pidfile, and the port-reclaim race handling are reused
as-is from app.server rather than duplicated.
"""

from __future__ import annotations

import atexit
import sys
import threading
import time

from .config import APP_NAME, HOST, app_version, resolve_port
from .server import clear_pidfile, port_is_free, write_pidfile, _reclaim_port


def _run_uvicorn(port: int) -> None:
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=HOST,
        port=port,
        log_level="warning",
        access_log=False,
        reload=False,
    )


def _open_browser_fallback(port: int, server_thread: threading.Thread) -> int:
    """Used when pywebview itself, or its native backend, isn't available.

    A machine with no registered browser at all (a headless CI runner, most
    notably - this is also what exercises this whole fallback path in the
    build-installers.yml smoke test) makes webbrowser.open() itself raise,
    not just silently fail. The server staying up and reachable is still a
    real, useful outcome even then, so this only logs that and keeps going
    rather than letting the exception crash the process.
    """
    import webbrowser

    try:
        webbrowser.open(f"http://{HOST}:{port}")
    except Exception as exc:
        print(f"  Could not open a browser automatically ({exc}). "
              f"Open http://{HOST}:{port} yourself.")

    try:
        while server_thread.is_alive():
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    return 0


def main() -> int:
    port = resolve_port()

    if not port_is_free(port) and not _reclaim_port(port):
        print(
            f"\n{APP_NAME} cannot start: {HOST}:{port} is already in use.\n",
            file=sys.stderr,
        )
        return 1

    write_pidfile(port)
    atexit.register(clear_pidfile)
    print(f"{APP_NAME} {app_version()} starting in a desktop window "
          f"(http://{HOST}:{port})")

    # Run the server on a background thread, not the main one: pywebview's
    # event loop (webview.start(), below) needs the main thread for itself
    # on every platform it supports.
    server_thread = threading.Thread(target=_run_uvicorn, args=(port,), daemon=True)
    server_thread.start()

    for _ in range(60):
        if not port_is_free(port):
            break
        time.sleep(0.25)

    try:
        import webview
    except ImportError:
        print(
            "  pywebview is not installed; opening a browser tab instead of "
            "a native window."
        )
        return _open_browser_fallback(port, server_thread)

    webview.create_window(
        APP_NAME,
        f"http://{HOST}:{port}",
        width=1320,
        height=860,
        min_size=(900, 600),
    )
    try:
        webview.start()
    except Exception as exc:
        # Most likely a missing native webview backend (e.g. Linux without
        # webkit2gtk or qtwebengine installed) - degrade instead of crashing
        # a launch that would otherwise work fine as a browser tab.
        print(
            f"  Could not open a native window ({exc}); opening a browser "
            "tab instead."
        )
        return _open_browser_fallback(port, server_thread)

    clear_pidfile()
    return 0


if __name__ == "__main__":
    sys.exit(main())
