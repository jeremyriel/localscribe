#!/usr/bin/env python3
"""Local Scribe launcher: environment setup, single instance, then start.

This module runs BEFORE any dependency is installed, so it uses the standard
library only. It must work identically on Windows, macOS and Linux.

What it does, in order:

1. Checks the running interpreter. CTranslate2 has no wheel for 3.14 yet, so an
   unsupported interpreter is replaced by a supported one if the machine has
   it, with a clear message rather than a failure mid-install.
2. Creates .venv and installs requirements.txt, but only when the requirements
   have actually changed since the last install.
3. Enforces a single instance: an existing Local Scribe is identified by its
   own signature and shut down. A foreign service holding the port is reported
   rather than worked around.
4. Starts the server and opens a browser.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
LOGS = ROOT / "logs"
PIDFILE = LOGS / "localscribe.pid"
REQUIREMENTS = ROOT / "requirements.txt"
DEPS_HASH = VENV / ".deps-hash"

APP_SIGNATURE = "localscribe/instance/v1"
DEFAULT_PORT = 43707
HOST = "127.0.0.1"

# CPython versions with wheels for the whole dependency set, best first.
SUPPORTED = ((3, 13), (3, 12), (3, 11), (3, 10))
IS_WINDOWS = os.name == "nt"


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def say(message: str = "") -> None:
    print(message, flush=True)


def step(message: str) -> None:
    say(f"  {message}")


def fail(message: str) -> None:
    say()
    say("-" * 68)
    say(f"  Local Scribe could not start.")
    say("-" * 68)
    for line in message.strip().splitlines():
        say(f"  {line}")
    say("-" * 68)
    say()
    if IS_WINDOWS and sys.stdin and sys.stdin.isatty():
        try:
            input("  Press Enter to close this window. ")
        except (EOFError, KeyboardInterrupt):
            pass
    raise SystemExit(1)


def banner(port: int) -> None:
    say()
    say("=" * 68)
    say("  Local Scribe - offline transcription for research")
    say("  Jeremy Riel, UIC TRAILblazer Lab")
    say("=" * 68)
    say(f"  Python   {platform.python_version()} ({sys.executable})")
    say(f"  Folder   {ROOT}")
    say(f"  Address  http://{HOST}:{port}")
    say("=" * 68)
    say()


# ---------------------------------------------------------------------------
# 1. Interpreter selection
# ---------------------------------------------------------------------------

def version_supported(info=None) -> bool:
    major, minor = (info or sys.version_info)[:2]
    return (major, minor) in SUPPORTED


def find_supported_interpreter() -> str | None:
    """Look for a usable interpreter without running anything expensive."""
    candidates: list[list[str]] = []

    if IS_WINDOWS:
        launcher = shutil.which("py")
        if launcher:
            candidates += [[launcher, f"-{major}.{minor}"] for major, minor in SUPPORTED]

    for major, minor in SUPPORTED:
        exe = shutil.which(f"python{major}.{minor}")
        if exe:
            candidates.append([exe])

    for command in candidates:
        try:
            out = subprocess.run(
                command + ["-c", "import sys;print('%d.%d' % sys.version_info[:2])"],
                capture_output=True, text=True, timeout=20,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if out.returncode != 0:
            continue
        found = out.stdout.strip()
        if tuple(int(p) for p in found.split(".")) in SUPPORTED:
            # Resolve to the real executable so the venv is built from it.
            probe = subprocess.run(
                command + ["-c", "import sys;print(sys.executable)"],
                capture_output=True, text=True, timeout=20,
            )
            if probe.returncode == 0 and probe.stdout.strip():
                return probe.stdout.strip()
    return None


def reexec_with_supported_interpreter() -> None:
    current = ".".join(str(p) for p in sys.version_info[:3])
    wanted = ", ".join(f"{a}.{b}" for a, b in SUPPORTED)

    say(f"  This is Python {current}, which the transcription engine does not")
    say(f"  support yet. Looking for Python {wanted}...")

    exe = find_supported_interpreter()
    if exe is None:
        fail(
            f"Python {current} is not supported, and no supported version was found.\n"
            f"\n"
            f"Local Scribe needs one of: Python {wanted}.\n"
            f"CTranslate2, the runtime that executes the Whisper model, does not\n"
            f"publish a build for Python 3.14 yet.\n"
            f"\n"
            f"Install Python 3.13 from https://www.python.org/downloads/ and run\n"
            f"this launcher again. An existing 3.10 to 3.13 installation is found\n"
            f"automatically; nothing needs to be uninstalled."
        )

    step(f"Using {exe}")
    say()
    os.execv(exe, [exe, str(Path(__file__).resolve())] + sys.argv[1:])


# ---------------------------------------------------------------------------
# 2. Virtual environment and dependencies
# ---------------------------------------------------------------------------

def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")


def requirements_hash() -> str:
    digest = hashlib.sha256()
    digest.update(REQUIREMENTS.read_bytes())
    # The interpreter version is part of the key: switching Python must
    # trigger a reinstall, since wheels are version specific.
    digest.update(f"{sys.version_info[:2]}".encode())
    return digest.hexdigest()


def ensure_venv() -> Path:
    python = venv_python()
    if not python.exists():
        say("  First run: creating an isolated Python environment in .venv")
        say("  (this keeps Local Scribe's packages away from your other projects)")
        try:
            venv.EnvBuilder(with_pip=True, clear=False, upgrade=False).create(str(VENV))
        except Exception as exc:
            fail(
                f"The virtual environment could not be created: {exc}\n"
                f"\n"
                f"Check that this folder is writable:\n  {ROOT}\n"
                f"On some managed Windows machines you may need to run the\n"
                f"launcher from a folder outside Program Files."
            )
        say("  Environment created.")
        say()
    return python


def install_dependencies(python: Path) -> None:
    wanted = requirements_hash()
    current = ""
    if DEPS_HASH.exists():
        try:
            current = DEPS_HASH.read_text(encoding="utf-8").strip()
        except OSError:
            current = ""

    if current == wanted:
        step("Dependencies are up to date.")
        return

    first_time = not current
    say("  Installing dependencies. The first run downloads roughly 250 MB and")
    say("  can take several minutes; later runs skip this entirely.")
    say()

    commands = [
        ([str(python), "-m", "pip", "install", "--upgrade", "pip", "--quiet"],
         "Updating pip"),
        ([str(python), "-m", "pip", "install", "-r", str(REQUIREMENTS)],
         "Installing packages"),
    ]

    for command, label in commands:
        step(f"{label}...")
        result = subprocess.run(command, cwd=str(ROOT))
        if result.returncode != 0:
            hint = (
                "\nThe most common cause is no internet connection on this first\n"
                "run. Local Scribe needs network access once, to install its\n"
                "packages and download a Whisper model. After that it runs fully\n"
                "offline."
            ) if first_time else (
                "\nTry deleting the .venv folder and running the launcher again."
            )
            fail(f"'{label}' failed with exit code {result.returncode}.{hint}")

    DEPS_HASH.write_text(wanted, encoding="utf-8")
    say()
    step("Dependencies installed.")


# ---------------------------------------------------------------------------
# 3. Single instance
# ---------------------------------------------------------------------------

def read_pidfile() -> dict | None:
    try:
        return json.loads(PIDFILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.6)
        return probe.connect_ex((HOST, port)) == 0


def identify_listener(port: int, timeout: float = 1.5) -> dict | None:
    """Ask whatever holds the port whether it is Local Scribe.

    This is what makes the shutdown safe: the launcher never terminates a
    process merely because a stale pidfile names its PID. A PID can be reused
    by anything, so identity is confirmed over HTTP first.
    """
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


def process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if IS_WINDOWS:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True,
        )
        return str(pid) in (out.stdout or "")
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def terminate(pid: int) -> None:
    if IS_WINDOWS:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True, text=True,
        )
        return
    import signal as signals
    try:
        os.kill(pid, signals.SIGTERM)
    except OSError:
        return
    for _ in range(20):
        if not process_alive(pid):
            return
        time.sleep(0.25)
    try:
        os.kill(pid, signals.SIGKILL)
    except OSError:
        pass


def describe_port_holder(port: int) -> str:
    """Best-effort description of whatever else is on the port."""
    try:
        if IS_WINDOWS:
            out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True)
            for line in (out.stdout or "").splitlines():
                if f"{HOST}:{port} " in line and "LISTENING" in line:
                    pid = line.split()[-1]
                    name = subprocess.run(
                        ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                        capture_output=True, text=True,
                    ).stdout.split()
                    return f"PID {pid}" + (f" ({name[0]})" if name else "")
        else:
            out = subprocess.run(
                ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
                capture_output=True, text=True,
            )
            lines = [l for l in (out.stdout or "").splitlines()[1:] if l.strip()]
            if lines:
                parts = lines[0].split()
                return f"PID {parts[1]} ({parts[0]})"
    except (OSError, subprocess.SubprocessError, IndexError):
        pass
    return "an unidentified process"


def enforce_single_instance(port: int) -> None:
    """Ensure exactly one Local Scribe, and never kill anything else."""
    running = identify_listener(port)

    if running is not None:
        pid = int(running.get("pid") or 0)
        step(f"An instance is already running (PID {pid}, version "
             f"{running.get('version')}). Shutting it down so only one runs.")
        terminate(pid)

        for _ in range(24):
            if not port_in_use(port):
                break
            time.sleep(0.25)
        else:
            fail(
                f"The previous Local Scribe on port {port} did not shut down.\n"
                f"\n"
                f"Close its window manually, or end process {pid}, then run this\n"
                f"launcher again."
            )
        step("Previous instance stopped.")
        PIDFILE.unlink(missing_ok=True)
        return

    if port_in_use(port):
        # Something is on the port but it is not Local Scribe. Do NOT move to
        # another port: relocating is exactly how local apps end up on each
        # other's ports and start crossing wires.
        holder = describe_port_holder(port)
        fail(
            f"Port {port} is in use by {holder}, which is not Local Scribe.\n"
            f"\n"
            f"Local Scribe deliberately does not move to a different port, so\n"
            f"that it can never collide with another local service.\n"
            f"\n"
            f"Either stop that process, or set a different port and run again:\n"
            + (f"    set LOCALSCRIBE_PORT=43708 && run.bat\n"
               if IS_WINDOWS else
               f"    LOCALSCRIBE_PORT=43708 ./run.sh\n")
        )

    # Port is free. Clean up a stale pidfile, and if it names a live process
    # that failed to answer the identity probe, leave it alone and say so.
    stale = read_pidfile()
    if stale:
        pid = int(stale.get("pid") or 0)
        if pid and pid != os.getpid() and process_alive(pid):
            step(f"A stale record names PID {pid}, which is running but is not "
                 "answering as Local Scribe. Leaving it alone.")
        PIDFILE.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 4. Launch
# ---------------------------------------------------------------------------

def open_browser(port: int, delay: float = 2.0) -> None:
    import threading
    import webbrowser

    def go():
        time.sleep(delay)
        # Wait until the server actually answers, so the browser does not open
        # on a connection error during a slow first start.
        for _ in range(40):
            if port_in_use(port):
                break
            time.sleep(0.5)
        try:
            webbrowser.open(f"http://{HOST}:{port}")
        except Exception:
            say(f"  Open your browser at http://{HOST}:{port}")

    threading.Thread(target=go, daemon=True).start()


def run_server(python: Path, port: int) -> int:
    environment = dict(os.environ)
    environment["LOCALSCRIBE_PORT"] = str(port)
    environment.setdefault("PYTHONUNBUFFERED", "1")
    # Keep HuggingFace's own telemetry off, on top of the app's offline lock.
    environment.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

    say("  Starting the server...")
    say()

    try:
        return subprocess.call(
            [str(python), "-m", "app.server"], cwd=str(ROOT), env=environment
        )
    except KeyboardInterrupt:
        say()
        say("  Stopped.")
        return 0


def resolve_port() -> int:
    raw = os.environ.get("LOCALSCRIBE_PORT")
    if not raw:
        return DEFAULT_PORT
    try:
        port = int(raw)
    except ValueError:
        return DEFAULT_PORT
    return port if 1024 <= port <= 65535 else DEFAULT_PORT


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="Local Scribe", description="Start Local Scribe.",
    )
    parser.add_argument("--no-browser", action="store_true",
                        help="do not open a browser window")
    parser.add_argument("--port", type=int, default=None,
                        help=f"override the port (default {DEFAULT_PORT})")
    parser.add_argument("--reinstall", action="store_true",
                        help="force dependency reinstallation")
    args = parser.parse_args()

    if args.port:
        os.environ["LOCALSCRIBE_PORT"] = str(args.port)
    port = resolve_port()

    if not REQUIREMENTS.exists():
        fail(f"requirements.txt is missing from {ROOT}.\n"
             "The application folder looks incomplete.")

    banner(port)

    if not version_supported():
        reexec_with_supported_interpreter()   # re-executes, does not return

    LOGS.mkdir(parents=True, exist_ok=True)

    if args.reinstall:
        DEPS_HASH.unlink(missing_ok=True)

    python = ensure_venv()
    install_dependencies(python)
    enforce_single_instance(port)

    if not args.no_browser:
        open_browser(port)

    return run_server(python, port)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
