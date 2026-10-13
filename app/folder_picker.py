"""Native local folder selection for settings that require filesystem paths.

The dialog runs in a short-lived child process. Tk must own its process's main
thread (macOS aborts otherwise) and would collide with the desktop window's
event loop if it ran inside the server, so the server only reads the answer.
If no dialog can be shown the caller raises and the user types the path.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

DIALOG_TIMEOUT_SECONDS = 600


class FolderPickerUnavailable(RuntimeError):
    """No native folder dialog could be shown on this computer."""


def pick_folder_in_process(initial_path: str = "") -> str:
    """Show a native directory chooser and return its absolute path or empty."""
    import tkinter as tk
    from tkinter import filedialog

    initial = Path(initial_path).expanduser() if initial_path else None
    if initial and not initial.is_dir():
        initial = initial.parent

    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
        root.update_idletasks()
        selected = filedialog.askdirectory(
            parent=root,
            title="Choose a local folder",
            initialdir=str(initial) if initial and initial.is_dir() else None,
            mustexist=True,
        )
        return str(Path(selected).expanduser().resolve()) if selected else ""
    finally:
        root.destroy()


def choose_folder(initial_path: str = "") -> str:
    """Return the folder the user picked, or "" if they cancelled.

    Raises FolderPickerUnavailable when the dialog cannot be shown (no Tk, no
    display, or it failed); the caller should fall back to manual entry.
    """
    with tempfile.TemporaryDirectory() as scratch:
        answer = Path(scratch) / "answer.txt"
        try:
            finished = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), str(answer), initial_path or ""],
                timeout=DIALOG_TIMEOUT_SECONDS,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise FolderPickerUnavailable(type(exc).__name__) from exc
        if finished.returncode != 0 or not answer.is_file():
            raise FolderPickerUnavailable("dialog did not complete")
        return answer.read_text(encoding="utf-8").strip()


if __name__ == "__main__":
    try:
        chosen = pick_folder_in_process(sys.argv[2] if len(sys.argv) > 2 else "")
        Path(sys.argv[1]).write_text(chosen, encoding="utf-8")
    except Exception:
        raise SystemExit(1)
