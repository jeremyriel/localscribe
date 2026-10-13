"""Regression checks for project-level transcription and translation choices."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path
from types import ModuleType
from unittest import mock
from unittest.mock import patch

from app import folder_picker, projects

FAILURES: list[str] = []


def check(label: str, condition: bool) -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        FAILURES.append(label)


def test_project_operation_and_queued_task():
    project = projects._normalize_project({
        "data_sensitivity": "no_phi",
        "operation": "translate",
    })
    root = Path(__file__).resolve().parent.parent
    jobs = (root / "app/jobs.py").read_text(encoding="utf-8")
    check("projects preserve the selected operation", project["operation"] == "translate")
    check("legacy project records are not given an operation on load",
          "operation" not in projects._normalize_project({"data_sensitivity": "no_phi"}))
    check("an explicit project operation is returned unchanged",
          projects.project_operation({"operation": "translate"}) == "translate")
    check("translation is carried into the queued job",
          "operation=languages.parse_operation(operation" in jobs)
    check("engine task is explicitly set by the selected operation",
          'overrides["task"] = operation' in jobs)
    check("new project records normalise explicit operation choices",
          projects.new_project_record("Study", operation="translate")["operation"] == "translate")
    check("an unsupported operation is never stored",
          projects.new_project_record("Study", operation="unsupported")["operation"] == "")


def test_native_picker_result_and_cancel():
    with tempfile.TemporaryDirectory() as directory:
        root_path = Path(directory)

        class FakeRoot:
            def withdraw(self):
                pass

            def attributes(self, *_args):
                pass

            def update_idletasks(self):
                pass

            def destroy(self):
                pass

        tkinter = ModuleType("tkinter")
        tkinter.__path__ = []
        tkinter.Tk = FakeRoot
        filedialog = ModuleType("tkinter.filedialog")
        selected = [str(root_path), ""]
        filedialog.askdirectory = lambda **_kwargs: selected.pop(0)

        old_tkinter = sys.modules.get("tkinter")
        old_filedialog = sys.modules.get("tkinter.filedialog")
        sys.modules["tkinter"] = tkinter
        sys.modules["tkinter.filedialog"] = filedialog
        try:
            check("picker returns the selected absolute folder",
                  folder_picker.pick_folder_in_process() == str(root_path.resolve()))
            check("picker cancellation leaves the path unchanged",
                  folder_picker.pick_folder_in_process() == "")
        finally:
            if old_tkinter is None:
                sys.modules.pop("tkinter", None)
            else:
                sys.modules["tkinter"] = old_tkinter
            if old_filedialog is None:
                sys.modules.pop("tkinter.filedialog", None)
            else:
                sys.modules["tkinter.filedialog"] = old_filedialog


def test_picker_runs_in_a_child_process_and_falls_back():
    def child(answer, code=0):
        def run(command, **_kwargs):
            if answer is not None:
                Path(command[2]).write_text(answer, encoding="utf-8")
            return subprocess.CompletedProcess(command, code)
        return run

    with patch.object(folder_picker.subprocess, "run", child("C:\\Synthetic\\Folder\n")):
        check("the folder chosen in the child process is returned",
              folder_picker.choose_folder("") == "C:\\Synthetic\\Folder")
    with patch.object(folder_picker.subprocess, "run", child("")):
        check("cancelling the dialog returns an empty path",
              folder_picker.choose_folder("") == "")
    for label, runner in (
        ("a dialog that cannot start", child(None, 1)),
        ("a missing interpreter", mock.Mock(side_effect=OSError("synthetic"))),
        ("a timeout", mock.Mock(side_effect=subprocess.TimeoutExpired("x", 1))),
    ):
        with patch.object(folder_picker.subprocess, "run", runner):
            try:
                folder_picker.choose_folder("")
            except folder_picker.FolderPickerUnavailable:
                check(f"{label} reports the picker as unavailable", True)
            else:
                check(f"{label} reports the picker as unavailable", False)


def test_project_and_settings_controls_are_present():
    root = Path(__file__).resolve().parent.parent
    project_template = (root / "app/templates/project.html").read_text(encoding="utf-8")
    settings_template = (root / "app/templates/settings.html").read_text(encoding="utf-8")
    project_js = (root / "app/static/js/project.js").read_text(encoding="utf-8")
    settings_js = (root / "app/static/js/settings.js").read_text(encoding="utf-8")
    main = (root / "app/main.py").read_text(encoding="utf-8")
    check("project page offers both transcription operations",
          'value="transcribe"' in project_template
          and 'value="translate"' in project_template
          and "Translate to English" in project_template
          and "T(operation)" in project_js)
    check("operation choice is saved per project",
          "LS.put(`/api/projects/" in project_js
          and "operation });" in project_js)
    catalog = (root / "app/catalog.py").read_text(encoding="utf-8")
    check("translation rejects an English-only model before queueing",
          "Translate to English needs a standard multilingual model" in catalog
          and "catalog.operation_problem(" in main)
    backup_js = (root / "app/static/js/backup.js").read_text(encoding="utf-8")
    check("Browse for a backup folder is on the project page and uses the picker",
          'id="backup-browse"' in project_template
          and "LS.post('/api/folder-picker'" in backup_js)
    check("the shared Box folder control is gone from Settings",
          "browse-box-folder" not in settings_template
          and "browse-box-folder" not in settings_js)


def main() -> int:
    for test in (
        test_project_operation_and_queued_task,
        test_native_picker_result_and_cancel,
        test_picker_runs_in_a_child_process_and_falls_back,
        test_project_and_settings_controls_are_present,
    ):
        print(f"\n{test.__name__}")
        test()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed: {FAILURES}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
