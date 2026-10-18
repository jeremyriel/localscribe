"""Synthetic local Box Sync copy and policy tests.

Run with: python -m tests.test_box_sync
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import tempfile
import time
import zipfile
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app import activity, box_sync, config, projects
from fastapi import HTTPException, Request

FAILURES: list = []


class FakeSettings:
    def __init__(self, destination: str = "", offline_lock: bool = False):
        self.values = {
            "box_sync_destination": destination,
            "offline_lock": offline_lock,
        }

    def get(self, key, fallback=None):
        return self.values.get(key, fallback)


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        FAILURES.append(label)


@contextmanager
def backup_context(*, classification="no_phi", offline_lock=False, configure=True):
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        projects_root = root / "projects"
        logs = root / "logs"
        destination = root / "Box Sync [Box Health - Synthetic]"
        destination.mkdir()
        paths = replace(
            config.PATHS,
            projects=projects_root,
            logs=logs,
            models=root / "models",
            settings_file=root / "settings.json",
        )
        settings = FakeSettings("", offline_lock)
        with (
            patch.object(projects, "PATHS", paths),
            patch.object(config, "PATHS", paths),
            patch.object(config, "SETTINGS", settings),
        ):
            project = projects.create_project(
                "Synthetic Box backup", data_sensitivity=classification
            )
            if configure:
                projects.set_backup_destination(project["slug"], destination)
            document = projects.add_document(
                project["slug"], "synthetic.wav", data=b"synthetic media only"
            )
            projects.write_json(
                projects.document_dir(project["slug"], document["id"])
                / projects.TRANSCRIPT_FILE,
                {"text": "synthetic transcript only"},
            )
            yield root, destination, settings, project, document


def wait_for_job(manager: box_sync.BackupManager, slug: str, job_id: str):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        job = manager.get(slug, job_id)
        if job and job["status"] in ("complete", "failed", "cancelled"):
            return job
        time.sleep(0.01)
    raise AssertionError("backup job did not finish before timeout")


def test_explicit_destination_and_sensitivity_gate():
    with backup_context(classification="no_phi") as (root, dest, settings, project, _):
        manager = box_sync.BackupManager()
        try:
            manager.submit(
                project["slug"],
                confirm_sync_destination=False,
                independently_verified_box_health=False,
            )
        except box_sync.BackupError:
            check("requires explicit Box Sync folder confirmation", True)
        else:
            check("requires explicit Box Sync folder confirmation", False)

        projects.clear_backup_destination(project["slug"])
        try:
            box_sync.configured_destination(project["slug"])
        except box_sync.BackupError:
            check("empty destination is never auto-selected", True)
        else:
            check("empty destination is never auto-selected", False)

        for label, value in (
            ("relative destination is rejected", "relative/Box"),
            ("malformed destination path is rejected", "\x00invalid"),
            ("project storage cannot be used as Box destination",
             str(projects.project_dir(project["slug"]))),
        ):
            try:
                box_sync.check_destination(value)
            except box_sync.BackupError:
                check(label, True)
            else:
                check(label, False)

        projects.set_backup_destination(project["slug"], dest)
        settings.values["offline_lock"] = True
        try:
            manager.submit(
                project["slug"],
                confirm_sync_destination=True,
                independently_verified_box_health=True,
            )
        except box_sync.BackupError as exc:
            check("Offline Lock blocks backup", "Offline Lock" in str(exc))
        else:
            check("Offline Lock blocks backup", False)

    with backup_context(classification="unknown_treat_as_phi") as (
        _root, _dest, settings, project, _
    ):
        ordinary_box = Path(_root) / "Box"
        ordinary_box.mkdir()
        projects.set_backup_destination(project["slug"], ordinary_box)
        try:
            box_sync.validate_backup_request(
                project["slug"],
                confirm_sync_destination=True,
                independently_verified_box_health=False,
            )
        except box_sync.BackupError as exc:
            check("PHI and unknown require Box Health", "Box Health" in str(exc))
        else:
            check("PHI and unknown require Box Health", False)

        validated, actual_destination = box_sync.validate_backup_request(
            project["slug"],
            confirm_sync_destination=True,
            independently_verified_box_health=True,
        )
        check("explicit independent verification permits policy gate",
              validated["slug"] == project["slug"] and actual_destination == ordinary_box)
        check("manual attestation is bound to the selected path",
              projects.load_backup_record(project["slug"])["attestation"][
                  "destination_root"
              ] == str(ordinary_box))
        check("the attestation is no longer written into the project record",
              "box_health_override" not in projects.load_project(project["slug"]))


def test_copy_verification_and_retry_after_interruption():
    with backup_context() as (_root, destination, _settings, project, _):
        manager = box_sync.BackupManager()
        original_open = Path.open

        def interrupted_open(path, mode="r", *args, **kwargs):
            if mode == "xb" and path.name.endswith(".partial"):
                partial = original_open(path, mode, *args, **kwargs)
                partial.write(b"interrupted partial bytes")
                partial.close()
                raise OSError("synthetic interrupted write")
            return original_open(path, mode, *args, **kwargs)

        with patch.object(Path, "open", interrupted_open):
            first = manager.submit(
                project["slug"],
                confirm_sync_destination=True,
                independently_verified_box_health=False,
            )
            failed = wait_for_job(manager, project["slug"], first["job_id"])
        check("interrupted transfer reports a retryable failure",
              failed["status"] == "failed" and bool(failed["error"]))
        check("interrupted transfer leaves no partial or final archive",
              not list(destination.glob("*.lsproj"))
              and not list(destination.glob(".*.partial")))

        retry = manager.submit(
            project["slug"],
            confirm_sync_destination=True,
            independently_verified_box_health=False,
        )
        completed = wait_for_job(manager, project["slug"], retry["job_id"])
        backed_up = destination / completed["filename"]
        check("retry completes with checksum verification",
              completed["status"] == "complete" and completed["verified"])
        check("verified archive is present at destination",
              backed_up.is_file() and backed_up.stat().st_size == completed["total_bytes"])
        check("destination archive manifest verifies",
              projects.inspect_archive(backed_up)["integrity_verified"])
        digest = hashlib.sha256(backed_up.read_bytes()).hexdigest()
        with zipfile.ZipFile(backed_up) as archive:
            check("archive carries the synthetic transcript",
                  b"synthetic transcript only" in archive.read(
                      f"documents/{projects.list_documents(project['slug'])[0]['id']}/"
                      f"{projects.TRANSCRIPT_FILE}"
                  ))
            media_entry = next(
                name for name in archive.namelist() if name.startswith("media/")
            )
            check("archive media remains compressed",
                  archive.getinfo(media_entry).compress_type == zipfile.ZIP_DEFLATED)
        check("backup outcome is recorded without a configured path",
              bool(activity.recent(project_id=project["project_id"]))
              and str(destination) not in str(activity.recent()))
        check("archive bytes are non-empty", bool(digest))


def test_failure_messages_name_the_cause_without_paths():
    with backup_context() as (_root, destination, _settings, project, _):
        manager = box_sync.BackupManager()
        cases = (
            ("an unbuildable archive", zipfile.BadZipFile(str(destination)), "archive could not be built"),
            ("an unexpected error", RuntimeError(str(destination)), "stopped unexpectedly"),
        )
        for label, error, expected in cases:
            with patch.object(projects, "export_project", side_effect=error):
                job = manager.submit(
                    project["slug"],
                    confirm_sync_destination=True,
                    independently_verified_box_health=False,
                )
                failed = wait_for_job(manager, project["slug"], job["job_id"])
            check(f"{label} fails with a specific message",
                  failed["status"] == "failed" and expected in failed["error"], failed["error"])
            check(f"{label} does not expose the destination path",
                  str(destination) not in failed["error"])
        check("failed backups publish no archive", not list(destination.glob("*.lsproj")))


def test_cancellation_cleans_partial_output():
    with backup_context() as (_root, destination, settings, project, _):
        manager = box_sync.BackupManager()
        original_copy = box_sync._copy_and_verify
        entered_copy = threading.Event()

        def cancel_during_copy(source, target, cancel_event, progress, check_allowed):
            entered_copy.set()
            cancel_event.wait(timeout=5)
            return original_copy(
                source, target, cancel_event, progress, check_allowed
            )

        with patch.object(box_sync, "_copy_and_verify", cancel_during_copy):
            job = manager.submit(
                project["slug"],
                confirm_sync_destination=True,
                independently_verified_box_health=False,
            )
            check("backup entered cancellable copy stage",
                  entered_copy.wait(timeout=5))
            check("active backup accepts cancellation",
                  manager.cancel(project["slug"], job["job_id"]))
            result = wait_for_job(manager, project["slug"], job["job_id"])
        check("cancellation reports a cancelled outcome", result["status"] == "cancelled")
        check("cancelled archive is not published",
              not list(destination.glob("*.lsproj"))
              and not list(destination.glob(".*.partial")))

        def engage_offline_during_copy(
            source, target, cancel_event, progress, check_allowed
        ):
            settings.values["offline_lock"] = True
            return original_copy(
                source, target, cancel_event, progress, check_allowed
            )

        with patch.object(
            box_sync, "_copy_and_verify", engage_offline_during_copy
        ):
            locked = manager.submit(
                project["slug"],
                confirm_sync_destination=True,
                independently_verified_box_health=False,
            )
            stopped = wait_for_job(manager, project["slug"], locked["job_id"])
        check("engaging Offline Lock stops an active copy",
              stopped["status"] == "failed"
              and "Offline Lock" in stopped["error"])
        check("Offline Lock interruption publishes no archive",
              not list(destination.glob("*.lsproj"))
              and not list(destination.glob(".*.partial")))

        settings.values["offline_lock"] = False
        retry = manager.submit(
            project["slug"],
            confirm_sync_destination=True,
            independently_verified_box_health=False,
        )
        completed = wait_for_job(manager, project["slug"], retry["job_id"])
        check("cancellation can be explicitly retried",
              completed["status"] == "complete" and completed["verified"])


def test_backup_api_and_project_ui():
    with backup_context() as (_root, destination, settings, project, _):
        with patch("app.update_check.refresh_state_async"), patch.object(
            config, "install_offline_guard"
        ):
            from app import main as web

        original_settings = web.SETTINGS
        web.SETTINGS = settings

        def request(payload=None):
            scope = {
                "type": "http", "method": "GET" if payload is None else "POST",
                "path": "/", "headers": [], "scheme": "http",
                "server": ("127.0.0.1", 43707), "query_string": b"",
            }

            async def receive():
                body = b"" if payload is None else json.dumps(payload).encode()
                return {"type": "http.request", "body": body, "more_body": False}

            return Request(scope, receive)

        async def exercise():
            try:
                await web.api_start_box_backup(
                    project["slug"],
                    request({"confirm_sync_destination": False}),
                )
            except HTTPException as exc:
                check("backup API requires destination confirmation",
                      exc.status_code == 400)
            else:
                check("backup API requires destination confirmation", False)

            page = await web.page_project(request(), project["slug"])
            check("project UI shows Box Sync confirmation and media option",
                  b"Back up to this folder" in page.body
                  and b"confirm-box-sync-destination" in page.body
                  and b"box-backup-include-media" in page.body)
            check("project UI offers Browse, Change, Clear and manual entry",
                  b'id="backup-browse"' in page.body
                  and b'id="backup-clear"' in page.body
                  and b'id="backup-path-input"' in page.body)

            result = await web.api_start_box_backup(
                project["slug"],
                request({
                    "confirm_sync_destination": True,
                    "independently_verified_box_health": False,
                    "include_media": False,
                }),
            )
            job_id = result["backup"]["job_id"]
            completed = wait_for_job(box_sync.BACKUPS, project["slug"], job_id)
            status = await web.api_get_box_backup(project["slug"], job_id)
            check("backup API reports completed verified copy",
                  completed["status"] == "complete"
                  and status["backup"]["verified"])
            check("backup API exposes no destination path",
                  str(destination) not in str(status))

        try:
            asyncio.run(exercise())
        finally:
            web.SETTINGS = original_settings


def _web():
    with patch("app.update_check.refresh_state_async"), patch.object(
        config, "install_offline_guard"
    ):
        from app import main as web
    return web


def _request(payload=None, method="POST"):
    scope = {
        "type": "http", "method": method, "path": "/", "headers": [],
        "scheme": "http", "server": ("127.0.0.1", 43707), "query_string": b"",
    }

    async def receive():
        body = b"" if payload is None else json.dumps(payload).encode()
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


def test_destinations_are_per_project_and_persist():
    with backup_context(configure=False) as (root, destination, _settings, project, _):
        other = projects.create_project("Synthetic second project")
        second_dir = root / "Second Box"
        second_dir.mkdir()
        check("a new project has no backup folder", not projects.load_backup_record(
            project["slug"])["destination"])

        box_sync.select_destination(project["slug"], str(destination), "picker")
        check("choosing a folder sets it for that project",
              projects.load_backup_record(project["slug"])["destination"]
              == str(destination.resolve()))
        check("another project is not given that folder",
              projects.load_backup_record(other["slug"])["destination"] == "")
        box_sync.select_destination(other["slug"], str(second_dir))
        box_sync.select_destination(project["slug"], str(destination))
        check("each project keeps its own folder",
              projects.load_backup_record(other["slug"])["destination"]
              == str(second_dir.resolve()))

        stored = json.loads(
            (projects.project_dir(project["slug"]) / projects.BACKUP_FILE).read_text("utf-8")
        )
        check("the record is small, versioned and local",
              stored["version"] == 1 and set(stored) == {
                  "version", "destination", "selection", "selected_at",
                  "attestation", "history"})
        check("the record survives a restart (re-read from disk)",
              projects.load_backup_record(project["slug"])["selection"] == "manual")

        for label, bad in (
            ("an empty path", ""),
            ("a relative path", "relative/Box"),
            ("a missing folder", str(root / "does-not-exist")),
            ("a file instead of a folder", str(root / "a-file.txt")),
        ):
            (root / "a-file.txt").write_text("x", encoding="utf-8")
            before = projects.load_backup_record(project["slug"])
            try:
                box_sync.select_destination(project["slug"], bad)
            except box_sync.BackupError:
                rejected = True
            else:
                rejected = False
            check(f"{label} is rejected and changes nothing",
                  rejected and projects.load_backup_record(project["slug"]) == before)

        projects.clear_backup_destination(project["slug"])
        check("clearing removes only this project's folder",
              projects.load_backup_record(project["slug"])["destination"] == ""
              and projects.load_backup_record(other["slug"])["destination"]
              == str(second_dir.resolve()))

        record_path = projects.project_dir(project["slug"]) / projects.BACKUP_FILE
        record_path.write_text("{not json", encoding="utf-8")
        check("a damaged record reads as no folder instead of failing",
              projects.load_backup_record(project["slug"])["destination"] == "")
        record_path.write_text(json.dumps({"version": 99, "destination": "x"}), encoding="utf-8")
        check("an unknown record version is ignored, not trusted",
              projects.load_backup_record(project["slug"])["destination"] == "")


def test_previous_global_folder_is_offered_never_applied():
    with backup_context(configure=False) as (root, destination, settings, project, _):
        other = projects.create_project("Synthetic other")
        settings.values["box_sync_destination"] = str(destination)
        state = box_sync.describe(project["slug"])
        check("the earlier shared folder is offered, not applied",
              state["legacy_available"] and not state["configured"])
        check("no project received the earlier folder silently",
              not projects.load_backup_record(other["slug"])["destination"])

        web = _web()

        async def exercise():
            result = await web.api_use_previous_backup_folder(project["slug"])
            return result["backup"]

        used = asyncio.run(exercise())
        record = projects.load_backup_record(project["slug"])
        check("explicit reuse sets the folder for that project only",
              used["configured"] and record["selection"] == "legacy_global"
              and not projects.load_backup_record(other["slug"])["destination"])
        check("the earlier setting is preserved, not erased",
              settings.values["box_sync_destination"] == str(destination))
        settings.values["box_sync_destination"] = ""

        async def nothing_to_reuse():
            try:
                await web.api_use_previous_backup_folder(other["slug"])
            except HTTPException as exc:
                return exc.status_code
            return None

        check("reuse without an earlier folder is a clear conflict",
              asyncio.run(nothing_to_reuse()) == 409)


def test_backup_history_and_status_do_not_claim_remote_sync():
    with backup_context(classification="may_contain_phi") as (root, destination, _s, project, _):
        ordinary = root / "Ordinary"
        ordinary.mkdir()
        box_sync.select_destination(project["slug"], str(ordinary))
        state = box_sync.describe(project["slug"])
        check("PHI projects are told Box Health verification is needed for this folder",
              state["available"] and state["needs_box_health"])
        box_sync.select_destination(project["slug"], str(destination))
        check("a Box Health named folder does not need the extra confirmation",
              not box_sync.describe(project["slug"])["needs_box_health"])

        manager = box_sync.BackupManager()
        with patch.object(projects, "export_project", side_effect=RuntimeError("x")):
            failed = wait_for_job(manager, project["slug"], manager.submit(
                project["slug"], confirm_sync_destination=True,
                independently_verified_box_health=False)["job_id"])
        done = wait_for_job(manager, project["slug"], manager.submit(
            project["slug"], confirm_sync_destination=True,
            independently_verified_box_health=False)["job_id"])
        history = box_sync.describe(project["slug"])["history"]
        check("each attempt is kept in the project's local history, newest first",
              [e["status"] for e in history] == ["complete", "failed"])
        check("a verified local copy is recorded as only locally verified",
              history[0]["local_copy_verified"] and history[0]["remote_sync"] == "unknown")
        check("a failed attempt records a reason but is not verified",
              not history[1]["local_copy_verified"] and bool(history[1].get("error")))
        check("job status separates local verification from remote sync",
              done["local_copy_verified"] and done["remote_sync"] == "unknown"
              and failed["remote_sync"] == "unknown")
        raw = json.loads(
            (projects.project_dir(project["slug"]) / projects.BACKUP_FILE).read_text("utf-8")
        )
        check("history stores no destination path",
              str(destination) not in json.dumps(raw["history"])
              and raw["destination"] == str(destination.resolve()))


def test_machine_local_backup_data_never_travels_in_archives():
    with backup_context(classification="may_contain_phi") as (root, destination, _s, project, _):
        slug = project["slug"]
        destination = root / "Ordinary"
        destination.mkdir()
        projects.set_backup_destination(slug, destination)
        box_sync.validate_backup_request(
            slug, confirm_sync_destination=True, independently_verified_box_health=True)
        # A project record written by an earlier version also carried the attestation.
        legacy = projects.load_project(slug)
        legacy["box_health_override"] = {
            "confirmed_at": "2026-01-01T00:00:00+00:00",
            "destination_root": str(destination),
            "independently_verified_box_health": True,
        }
        projects.write_json(projects.project_dir(slug) / projects.PROJECT_FILE, legacy)
        check("the attestation is stored in the local record",
              projects.load_backup_record(slug)["attestation"] is not None)

        archive = projects.export_project(slug, root)
        snapshot = projects.create_snapshot(slug)
        for label, path in (
            ("project export", archive),
            ("recovery snapshot", projects._snapshot_dir(slug) / snapshot["snapshot_id"]),
        ):
            with zipfile.ZipFile(path) as bundle:
                names = bundle.namelist()
                blob = b"".join(bundle.read(name) for name in names)
                check(f"{label} excludes the machine-local record",
                      not any(name.endswith(".local.json") for name in names))
                check(f"{label} carries no destination path or attestation",
                      str(destination).encode() not in blob
                      and b"box_health_override" not in blob
                      and b"destination_root" not in blob)

        # A current archive with a smuggled machine-local file fails its own manifest check.
        smuggled = root / "smuggled.lsproj"
        with zipfile.ZipFile(archive) as source, zipfile.ZipFile(smuggled, "w") as target:
            for item in source.infolist():
                target.writestr(item, source.read(item.filename))
            target.writestr(projects.BACKUP_FILE, json.dumps({"version": 1, "destination": str(destination)}))
        try:
            projects.import_project(smuggled, mode="rename")
        except projects.ProjectError:
            check("a version 2 archive with an extra local file is rejected", True)
        else:
            check("a version 2 archive with an extra local file is rejected", False)

        # A legacy (version 1) archive has no per-file integrity data, so the importer
        # itself must refuse to restore machine-local files or old attestations.
        crafted = root / "crafted.lsproj"
        with zipfile.ZipFile(crafted, "w") as target:
            target.writestr(projects.MANIFEST_FILE, json.dumps({
                "format": "localscribe-project", "format_version": 1,
                "slug": "crafted-legacy", "title": "Crafted legacy"}))
            target.writestr(projects.PROJECT_FILE, json.dumps({
                "slug": "crafted-legacy", "title": "Crafted legacy", "project_id": "crafted",
                "data_sensitivity": "no_phi",
                "box_health_override": legacy["box_health_override"]}))
            target.writestr(projects.BACKUP_FILE, json.dumps({
                "version": 1, "destination": str(destination), "selection": "manual",
                "selected_at": "", "attestation": {"destination_root": str(destination)},
                "history": []}))
        projects.import_project(crafted, mode="rename")
        imported_slug = "crafted-legacy"
        check("an imported project has no backup folder",
              projects.load_backup_record(imported_slug)["destination"] == "")
        check("an imported project has no attestation",
              projects.load_backup_record(imported_slug)["attestation"] is None
              and "box_health_override" not in projects.load_project(imported_slug))
        check("no machine-local file was extracted",
              not (projects.project_dir(imported_slug) / projects.BACKUP_FILE).exists())

        # Replace-mode keeps this computer's folder and history, never the attestation.
        projects.import_project(archive, mode="replace")
        kept = projects.load_backup_record(slug)
        check("replacing a project keeps this computer's backup folder",
              kept["destination"] == str(destination.resolve()))
        check("replacing a project never keeps an earlier attestation",
              kept["attestation"] is None
              and "box_health_override" not in projects.load_project(slug))


def test_backup_api_changes_leave_state_unchanged_on_error():
    with backup_context(configure=False) as (root, destination, _s, project, _):
        web = _web()

        async def exercise():
            saved = await web.api_set_backup_destination(
                project["slug"], _request({"destination": str(destination), "selection": "picker"}, "PUT"))
            check("the API saves a validated folder",
                  saved["backup"]["configured"] and saved["backup"]["available"]
                  and saved["backup"]["selection"] == "picker")
            for label, payload in (
                ("a missing folder", {"destination": str(root / "nope")}),
                ("an empty path", {"destination": ""}),
                ("no path at all", {}),
            ):
                try:
                    await web.api_set_backup_destination(project["slug"], _request(payload, "PUT"))
                except HTTPException as exc:
                    status = exc.status_code
                else:
                    status = None
                check(f"{label} is a 400",
                      status == 400)
            check("failed changes leave the saved folder untouched",
                  projects.load_backup_record(project["slug"])["destination"]
                  == str(destination.resolve()))
            try:
                await web.api_set_backup_destination("no-such-project", _request({"destination": str(destination)}, "PUT"))
            except HTTPException as exc:
                check("an unknown project is a 404", exc.status_code == 404)
            cleared = await web.api_clear_backup_destination(project["slug"])
            check("the API clears the folder", not cleared["backup"]["configured"])
            events = activity.recent(project_id=project["project_id"])
            check("changes are logged without the path",
                  [e["action"] for e in events] == ["network_settings_changed"] * 2
                  and str(destination) not in json.dumps(events))

        asyncio.run(exercise())


def main() -> int:
    for test in (
        test_explicit_destination_and_sensitivity_gate,
        test_copy_verification_and_retry_after_interruption,
        test_failure_messages_name_the_cause_without_paths,
        test_cancellation_cleans_partial_output,
        test_backup_api_and_project_ui,
        test_destinations_are_per_project_and_persist,
        test_previous_global_folder_is_offered_never_applied,
        test_backup_history_and_status_do_not_claim_remote_sync,
        test_machine_local_backup_data_never_travels_in_archives,
        test_backup_api_changes_leave_state_unchanged_on_error,
    ):
        print(f"\n{test.__name__}")
        test()
    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed: {FAILURES}")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
