"""Synthetic local snapshot, restore, and destructive-action tests.

Run with: python -m tests.test_recovery
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import zipfile
from contextlib import contextmanager
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from app import activity, config, projects

FAILURES: list = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        FAILURES.append(label)


@contextmanager
def project_root():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        paths = replace(
            config.PATHS, projects=root / "projects", logs=root / "logs",
            models=root / "models", settings_file=root / "settings.json",
        )
        with (
            patch.object(projects, "PATHS", paths),
            patch.object(config, "PATHS", paths),
        ):
            yield root


def test_snapshot_and_restore_choices():
    with project_root():
        project = projects.create_project(
            "Synthetic recovery", data_sensitivity="may_contain_phi"
        )
        slug = project["slug"]
        document = projects.add_document(slug, "synthetic.wav", data=b"synthetic media")
        transcript_path = (
            projects.document_dir(slug, document["id"]) / projects.TRANSCRIPT_FILE
        )
        transcript_path.write_text('{"text":"before snapshot"}', encoding="utf-8")

        snapshot = projects.create_snapshot(slug, include_media=False)
        snapshots = projects.list_snapshots(slug)
        archive = (
            config.PATHS.projects / ".snapshots" / slug / snapshot["snapshot_id"]
        )
        manifest = projects.inspect_archive(archive)
        check("snapshot stored outside project", archive.exists() and archive.parent.parent.name == ".snapshots")
        check("snapshot manifest records media mode", manifest["includes_media"] is False)
        check("snapshot has verified archive integrity", manifest["integrity_verified"])
        check("snapshot omitted media", not any(
            entry["path"].startswith("media/") for entry in manifest["files"]
        ))
        check("snapshot appears in project list", len(snapshots) == 1)

        transcript_path.write_text('{"text":"after snapshot"}', encoding="utf-8")
        check("abort leaves the project unchanged",
              projects.restore_snapshot(slug, snapshot["snapshot_id"], "abort") is None
              and "after snapshot" in transcript_path.read_text(encoding="utf-8"))
        renamed = projects.restore_snapshot(slug, snapshot["snapshot_id"], "rename")
        renamed_transcript = (
            projects.document_dir(renamed["slug"], document["id"])
            / projects.TRANSCRIPT_FILE
        )
        check("rename restore preserves existing project",
              renamed["slug"] != slug and "after snapshot" in transcript_path.read_text(encoding="utf-8"))
        check("rename restore restores archived transcript",
              "before snapshot" in renamed_transcript.read_text(encoding="utf-8"))

        replaced = projects.restore_snapshot(slug, snapshot["snapshot_id"], "replace")
        check("replace restore restores archived transcript",
              replaced["slug"] == slug
              and "before snapshot" in transcript_path.read_text(encoding="utf-8"))
        check("replace restore creates a safety snapshot",
              len(projects.list_snapshots(slug)) == 2)
        try:
            projects.restore_snapshot(slug, "../outside.lsproj", "replace")
        except projects.ProjectValidationError:
            check("unsafe snapshot identifiers are rejected", True)
        else:
            check("unsafe snapshot identifiers are rejected", False)


def test_api_destructive_actions_require_choice():
    with project_root():
        with patch("app.update_check.refresh_state_async"), patch.object(
            config, "install_offline_guard"
        ):
            from app import main as web
        project = projects.create_project("Synthetic API recovery")
        slug = project["slug"]
        document = projects.add_document(slug, "synthetic.wav", data=b"synthetic")

        async def exercise():
            from fastapi import HTTPException, Request

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

            try:
                await web.api_delete_document(slug, document["id"])
            except HTTPException as exc:
                check("document deletion requires an explicit choice", exc.status_code == 400)
            else:
                check("document deletion requires an explicit choice", False)
            check("rejected document deletion makes no changes",
                  projects.load_document(slug, document["id"])["id"] == document["id"])
            await web.api_delete_document(slug, document["id"], snapshot=True)
            check("document deletion can snapshot first",
                  not projects.list_documents(slug) and len(projects.list_snapshots(slug)) == 1)
            project_page = await web.page_project(request(), slug)
            check("project page exposes its local snapshots",
                  b"Recovery snapshots" in project_page.body
                  and projects.list_snapshots(slug)[0]["snapshot_id"].encode()
                  in project_page.body)

            try:
                await web.api_delete_project(slug)
            except HTTPException as exc:
                check("project deletion requires an explicit choice", exc.status_code == 400)
            else:
                check("project deletion requires an explicit choice", False)
            await web.api_delete_project(slug, snapshot=False)
            check("project deletion preserves existing snapshots",
                  not projects.project_dir(slug).exists()
                  and len(projects.list_all_snapshots()) == 1)
            events = activity.recent(project_id=project["project_id"])
            check("recovery activity records destructive outcomes",
                  [event["action"] for event in events] == ["document_delete", "project_delete"])
            check("activity excludes project titles and file paths",
                  all("Synthetic API recovery" not in str(event)
                      and str(config.PATHS.projects) not in str(event) for event in events))
            snapshot_id = projects.list_all_snapshots()[0]["snapshot_id"]
            index_page = await web.page_index(request())
            check("project list retains snapshots after project deletion",
                  snapshot_id.encode() in index_page.body)
            restored = await web.api_restore_deleted_project_snapshot(
                slug, snapshot_id, request({"mode": "rename"})
            )
            check("deleted-project snapshots restore from the project list",
                  restored["project"]["slug"] == slug
                  and projects.project_dir(slug).is_dir())

        asyncio.run(exercise())


def test_activity_is_bounded_and_filtered():
    with project_root():
        for index in range(activity.HISTORY_LIMIT + 5):
            activity.record("snapshot_create", f"synthetic-{index % 2}")
        recent = activity.recent(limit=activity.HISTORY_LIMIT)
        check("activity history is bounded", len(recent) == activity.HISTORY_LIMIT)
        check("activity filtering is project-specific",
              all(event["project_ref"] == activity._project_ref("synthetic-1")
                  for event in activity.recent(project_id="synthetic-1")))
        check("zero limit returns no events", activity.recent(limit=0) == [])


def test_deleted_project_can_be_restored():
    with project_root():
        project = projects.create_project("Synthetic deleted recovery")
        slug = project["slug"]
        snapshot = projects.create_snapshot(slug)
        projects.delete_project(slug)
        check("deleted project snapshot remains discoverable",
              len(projects.list_all_snapshots()) == 1)
        restored = projects.restore_snapshot(slug, snapshot["snapshot_id"], "rename")
        check("deleted project snapshot restores without an existing project",
              restored["slug"] == slug and projects.project_dir(slug).is_dir())


def test_overwrite_import_requires_snapshot_choice():
    with project_root():
        with patch("app.update_check.refresh_state_async"), patch.object(
            config, "install_offline_guard"
        ):
            from app import main as web
        source = projects.create_project("Synthetic import", description="archived")
        archive = projects.export_project(source["slug"], config.PATHS.logs)
        projects.delete_project(source["slug"])
        current = projects.create_project("Synthetic import", description="current")

        async def exercise():
            from fastapi import HTTPException, UploadFile

            try:
                await web.api_import_project(
                    UploadFile(filename="synthetic.lsproj", file=BytesIO(archive.read_bytes())),
                    mode="replace",
                )
            except HTTPException as exc:
                check("overwrite import requires a snapshot choice", exc.status_code == 400)
            else:
                check("overwrite import requires a snapshot choice", False)
            check("rejected overwrite import leaves project unchanged",
                  projects.load_project(current["slug"])["description"] == "current")

            with patch.object(web, "PATHS", config.PATHS):
                result = await web.api_import_project(
                    UploadFile(
                        filename="../../synthetic.lsproj",
                        file=BytesIO(archive.read_bytes()),
                    ),
                    mode="replace",
                    snapshot_before_replace="yes",
                )
            check("overwrite import creates a recovery snapshot",
                  len(projects.list_snapshots(current["slug"])) == 1)
            check("overwrite import restores archive contents",
                  result["project"]["description"] == "archived")
            check("upload filename cannot escape staging directory",
                  not (config.PATHS.logs.parent / "synthetic.lsproj").exists())

        asyncio.run(exercise())


def test_archive_entries_use_forward_slashes():
    with project_root():
        project = projects.create_project("Synthetic zip names")
        slug = project["slug"]
        projects.add_document(slug, "synthetic.wav", data=b"synthetic media")
        archive = projects.export_project(slug, config.PATHS.logs)
        with zipfile.ZipFile(archive) as bundle:
            stored = [info.orig_filename for info in bundle.infolist()]
            check("archive entries are stored with forward slashes",
                  stored and all("\\" not in name for name in stored), str(stored))
            check("media entries are present and readable",
                  any(name.startswith("media/") for name in stored)
                  and bundle.testzip() is None)


def test_failed_snapshot_blocks_deletion():
    with project_root():
        with patch("app.update_check.refresh_state_async"), patch.object(
            config, "install_offline_guard"
        ):
            from app import main as web
        project = projects.create_project("Synthetic failing snapshot")
        slug = project["slug"]
        document = projects.add_document(slug, "synthetic.wav", data=b"synthetic")

        async def exercise():
            from fastapi import HTTPException

            with patch.object(projects, "create_snapshot", side_effect=zipfile.BadZipFile("x")):
                for label, call in (
                    ("document", lambda: web.api_delete_document(slug, document["id"], snapshot=True)),
                    ("project", lambda: web.api_delete_project(slug, snapshot=True)),
                ):
                    try:
                        await call()
                    except HTTPException as exc:
                        check(f"{label} deletion reports a failed snapshot clearly",
                              exc.status_code == 500 and "snapshot" in exc.detail)
                    else:
                        check(f"{label} deletion reports a failed snapshot clearly", False)
            check("a failed snapshot deletes nothing",
                  projects.load_document(slug, document["id"])["id"] == document["id"])
            events = activity.recent(project_id=project["project_id"])
            check("the failed snapshot is recorded without content",
                  [e["outcome"] for e in events] == ["failed", "failed"]
                  and all(e["action"] == "snapshot_create" for e in events))

        asyncio.run(exercise())


def test_removal_dialog_has_explicit_outcomes():
    script = (Path(config.__file__).parent / "static" / "js" / "project.js").read_text(
        encoding="utf-8"
    )
    check("removal uses a dialog with three explicit outcomes",
          "function confirmRemoval" in script
          and "'Keep it'" in script
          and "'Remove without a snapshot'" in script
          and "'Create snapshot, then remove'" in script)
    check("cancelling the dialog never deletes", "if (!choice) return;" in script)
    check("the old two-step confirm() chain is gone",
          "Create a local recovery snapshot of the whole project first" not in script
          and "Choose Cancel to continue without" not in script)


def test_snapshot_media_status_and_open_folder():
    with project_root():
        with patch("app.update_check.refresh_state_async"), patch.object(
            config, "install_offline_guard"
        ):
            from app import main as web
        project = projects.create_project("Synthetic snapshot folder")
        slug = project["slug"]
        projects.add_document(slug, "synthetic.wav", data=b"synthetic media")
        empty = projects.create_project("Synthetic no recordings")

        projects.create_snapshot(slug, include_media=True)
        projects.create_snapshot(slug, include_media=False)
        projects.create_snapshot(empty["slug"], include_media=True)
        media = sorted(item["media"] for item in projects.list_snapshots(slug))
        check("snapshots say whether source media is included, from their manifest",
              media == ["included", "omitted"], str(media))
        check("a project without recordings is described as such",
              projects.list_snapshots(empty["slug"])[0]["media"] == "none")
        legacy = projects._snapshot_dir(slug) / "legacy-20200101-000000-aaaaaaaa.lsproj"
        with zipfile.ZipFile(legacy, "w") as bundle:
            bundle.writestr("project.json", "{}")
        check("an older or unreadable archive is reported as unknown, not guessed",
              next(s for s in projects.list_snapshots(slug)
                   if s["snapshot_id"] == legacy.name)["media"] == "unknown")
        legacy.unlink()

        opened = []

        async def exercise():
            from fastapi import HTTPException

            with patch.object(projects, "reveal", lambda path: opened.append(Path(path)) or True):
                result = await web.api_open_snapshot_folder(slug)
            check("the project's own snapshot folder is opened",
                  result["opened"] and opened == [projects._snapshot_dir(slug)])
            with patch.object(projects, "reveal", lambda path: False):
                result = await web.api_open_snapshot_folder(slug)
            check("if no file manager can open it, the path is returned as a fallback",
                  result["opened"] is False and result["path"] == str(projects._snapshot_dir(slug)))
            for label, target, status in (
                ("a project with no snapshots", projects.create_project("Synthetic bare")["slug"], 409),
                ("an unknown project", "no-such-project", 404),
                ("a traversal attempt", "..", 404),
            ):
                try:
                    await web.api_open_snapshot_folder(target)
                except HTTPException as exc:
                    check(f"{label} opens nothing ({status})", exc.status_code == status)
                else:
                    check(f"{label} opens nothing ({status})", False)
            check("opening never used another project's folder",
                  all(path == projects._snapshot_dir(slug) for path in opened))

        asyncio.run(exercise())


def main() -> int:
    for test in (
        test_snapshot_and_restore_choices,
        test_api_destructive_actions_require_choice,
        test_activity_is_bounded_and_filtered,
        test_deleted_project_can_be_restored,
        test_overwrite_import_requires_snapshot_choice,
        test_archive_entries_use_forward_slashes,
        test_failed_snapshot_blocks_deletion,
        test_removal_dialog_has_explicit_outcomes,
        test_snapshot_media_status_and_open_folder,
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
