"""Checks GitHub for a newer release, once per launch, and never acts on it
without an explicit click.

Follows the same convention app/jobs.py's model-download job already
established for the app's one deliberate exception to being offline: check
the offline lock first, say what network request is about to happen, never
let a failure here block anything else. The check itself runs on a
background thread kicked off once at server startup (see app/main.py); the
result is cached here and read by every page render via base_context() -
no client-side polling.
"""

from __future__ import annotations

import json
import platform
import subprocess
import threading
import urllib.error
import urllib.request

from .config import PATHS, SETTINGS, app_version
from .console import CONSOLE

GITHUB_REPO = "jeremyriel/localscribe"
RELEASES_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
RAW_VERSION_URL = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/VERSION"
REPO_URL = f"https://github.com/{GITHUB_REPO}"

_lock = threading.Lock()
_STATE: dict = {
    "checked": False,
    "available": False,
    "current": app_version(),
    "latest": None,
    "download_url": None,   # a release asset matching this OS, if one exists
    "release_url": None,    # the release page, or the repo itself as a fallback
}


def state() -> dict:
    with _lock:
        return dict(_STATE)


def _version_tuple(text: str) -> tuple[int, ...]:
    """"1.01" -> (1, 1); "1.01.1" -> (1, 1, 1). Not semver - see README's
    Versioning section: MAJOR.MINOR[.BUILD], not zero-padded MINOR."""
    parts = []
    for piece in text.strip().split("."):
        try:
            parts.append(int(piece))
        except ValueError:
            break
    return tuple(parts)


def _is_newer(latest: str, current: str) -> bool:
    a, b = _version_tuple(latest), _version_tuple(current)
    length = max(len(a), len(b))
    a += (0,) * (length - len(a))
    b += (0,) * (length - len(b))
    return a > b


def _platform_asset_patterns() -> tuple[str, ...]:
    """Substrings that identify this OS's installer among release assets,
    matching the exact naming the packaging scripts (packaging/*) produce."""
    system = platform.system()
    if system == "Darwin":
        return ("-macOS-",)
    if system == "Windows":
        return ("-Windows-",)
    return ("-Linux-",)


def _fetch_json(url: str, timeout: float = 6.0) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
        return None


def _fetch_text(url: str, timeout: float = 6.0) -> str | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.read().decode("utf-8").strip()
    except (urllib.error.URLError, OSError, UnicodeDecodeError):
        return None


def refresh_state() -> dict:
    """Run the actual check. Safe to call from any thread; never raises."""
    current = app_version()

    if SETTINGS.get("offline_lock"):
        with _lock:
            _STATE.update(checked=False, available=False, current=current)
        return state()

    if not SETTINGS.get("check_for_updates", True):
        with _lock:
            _STATE.update(checked=False, available=False, current=current)
        return state()

    CONSOLE.step("Checking GitHub for a newer version of Local Scribe",
                 repo=GITHUB_REPO)

    latest = None
    download_url = None
    release_url = REPO_URL

    release = _fetch_json(RELEASES_API)
    if release and release.get("tag_name"):
        latest = str(release["tag_name"]).lstrip("v")
        release_url = release.get("html_url") or release_url
        for asset in release.get("assets") or []:
            name = asset.get("name") or ""
            if any(p in name for p in _platform_asset_patterns()):
                download_url = asset.get("browser_download_url")
                break
    else:
        # No release published yet (or the API call failed) - fall back to
        # comparing against the source on `main`, without a direct
        # installer link since none exists in that case.
        latest = _fetch_text(RAW_VERSION_URL)

    if not latest:
        CONSOLE.debug("Update check found nothing to compare against "
                       "(no release published yet, or the request failed)")
        with _lock:
            _STATE.update(checked=False, available=False, current=current)
        return state()

    available = _is_newer(latest, current)
    with _lock:
        _STATE.update(
            checked=True, available=available, current=current,
            latest=latest, download_url=download_url, release_url=release_url,
        )

    if available:
        CONSOLE.info(f"A newer version is available: {latest} (you have "
                     f"{current}). See it in Settings.",
                     current=current, latest=latest)
    else:
        CONSOLE.info(f"Local Scribe is up to date ({current}).")
    return state()


def refresh_state_async() -> None:
    threading.Thread(target=refresh_state, daemon=True).start()


# ---------------------------------------------------------------------------
# git-checkout update path
# ---------------------------------------------------------------------------


def is_git_checkout() -> bool:
    """True for a `git clone`, false for a packaged installer.

    The packaging scripts (packaging/*/build.sh, .ps1) copy only app/,
    bootstrap.py, requirements.txt, VERSION and LICENSE into the bundle -
    never .git - so this is already a reliable, no-extra-marker-needed
    signal for which update path applies.
    """
    return (PATHS.root / ".git").is_dir()


def _run_git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(PATHS.root),
        capture_output=True, text=True, timeout=30,
    )


def apply_git_update() -> dict:
    """Pull the latest from origin/main. Does not restart the server -
    that is left to the user, deliberately (see app/update_check.py's
    module docstring and the plan this shipped from: a just-updated
    checkout may need new dependencies bootstrap.py only reinstalls on the
    *next* launch, and restarting a process out from under its own
    just-changed source is asking for trouble)."""
    if not is_git_checkout():
        return {"ok": False, "message": "This isn't a git checkout."}

    if SETTINGS.get("offline_lock"):
        return {"ok": False, "message": "The offline lock is engaged."}

    status = _run_git("status", "--porcelain")
    if status.returncode != 0:
        return {"ok": False, "message": f"git status failed: {status.stderr.strip()}"}
    if status.stdout.strip():
        return {"ok": False, "message": (
            "This checkout has local changes that haven't been committed, "
            "so pulling could lose them. Resolve that first (commit, stash, "
            "or discard the changes), then try again."
        )}

    CONSOLE.step("Pulling the latest version from GitHub")
    pull = _run_git("pull", "--ff-only")
    if pull.returncode != 0:
        CONSOLE.error(f"Update failed: {pull.stderr.strip()}")
        return {"ok": False, "message": pull.stderr.strip() or "git pull failed."}

    CONSOLE.success("Updated. Restart Local Scribe to finish.")
    refresh_state()
    return {"ok": True, "message": "Updated. Restart Local Scribe to finish.",
            "output": pull.stdout.strip()}
