"""Synthetic, local-only classification tests.

Run with: python -m tests.test_sensitivity
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from app import config, projects
from app.sensitivity import DEFAULT_SENSITIVITY, SENSITIVITY_LABELS, future_backup_gate

FAILURES: list = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        FAILURES.append(label)


@contextmanager
def project_root():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        paths = replace(
            config.PATHS, projects=root / "projects", logs=root / "logs",
            models=root / "models", settings_file=root / "settings.json",
        )
        with patch.object(projects, "PATHS", paths), patch.object(config, "PATHS", paths):
            yield root


def rejects(label, operation):
    try:
        operation()
    except projects.ProjectValidationError:
        check(label, True)
    else:
        check(label, False)


def test_defaults_and_lazy_persistence():
    with project_root():
        record = projects.create_project("Synthetic study")
        slug = record["slug"]
        path = projects.project_dir(slug) / projects.PROJECT_FILE
        check("new default", record["data_sensitivity"] == DEFAULT_SENSITIVITY)
        check("new default persisted", projects.read_json(path)["data_sensitivity"] == DEFAULT_SENSITIVITY)
        del record["data_sensitivity"]
        projects.write_json(path, record)
        before = path.read_bytes()
        check("legacy load normalized", projects.load_project(slug)["data_sensitivity"] == DEFAULT_SENSITIVITY)
        check("legacy list normalized", projects.list_projects()[0]["data_sensitivity"] == DEFAULT_SENSITIVITY)
        check("reads leave disk unchanged", path.read_bytes() == before)
        projects.save_project(slug, {"description": "Synthetic notes", "tags": "one, two"})
        stored = projects.read_json(path)
        check("next metadata write migrates", stored["data_sensitivity"] == DEFAULT_SENSITIVITY)
        check("existing fields still editable", stored["tags"] == ["one", "two"])
        del stored["data_sensitivity"]
        projects.write_json(path, stored)
        projects.touch_project(slug)
        check("document activity write migrates", projects.read_json(path)["data_sensitivity"] == DEFAULT_SENSITIVITY)
        stored["data_sensitivity"] = ["invalid stored value"]
        projects.write_json(path, stored)
        check("malformed stored classification fails safe", projects.load_project(slug)["data_sensitivity"] == DEFAULT_SENSITIVITY)
        projects.save_project(slug, {})
        check("malformed value conservatively persisted", projects.read_json(path)["data_sensitivity"] == DEFAULT_SENSITIVITY)


def test_valid_and_invalid_updates():
    with project_root():
        for value in SENSITIVITY_LABELS:
            record = projects.create_project("Synthetic selection", data_sensitivity=value)
            slug = record["slug"]
            check(f"create {value}", record["data_sensitivity"] == value)
            updated = projects.save_project(slug, {"data_sensitivity": value})
            check(f"update {value}", updated["data_sensitivity"] == value)
            check(f"reload {value}", projects.load_project(slug)["data_sensitivity"] == value)
        path = projects.project_dir(slug) / projects.PROJECT_FILE
        before = path.read_bytes()
        for bad in ("ordinary_box", "", " no_phi ", "NO_PHI", None, True, 1, [], {}):
            rejects(f"reject update {bad!r}", lambda: projects.save_project(slug, {
                "title": "Must not persist", "data_sensitivity": bad,
            }))
            check("failed update is atomic", path.read_bytes() == before)
            count = len(projects.list_projects())
            rejects(f"reject create {bad!r}", lambda: projects.create_project(
                "Invalid synthetic project", data_sensitivity=bad,
            ))
            check("invalid create leaves no directory", len(projects.list_projects()) == count)


def test_backup_gate():
    recognized = "Synthetic study [Box Health - Research]"
    for value in SENSITIVITY_LABELS:
        requires = value != "no_phi"
        result = future_backup_gate(value, recognized)
        check(f"{value}: recognized allowed", result.allowed and result.recognized_box_health)
        check(f"{value}: requirement", result.requires_box_health == requires)
        check(f"{value}: recognized needs no override", not result.override_used)
        for root in ("Box", "Synthetic unrecognized root"):
            result = future_backup_gate(value, root)
            check(f"{value}: unrecognized root", result.allowed == (not requires))
            check(f"{value}: ordinary Box denied without attestation", not result.override_used)
        for confirmation in (False, None, 1, "true"):
            result = future_backup_gate(value, "Synthetic unrecognized root",
                                        independently_verified_box_health=confirmation)
            check(f"{value}: explicit boolean required {confirmation!r}", result.allowed == (not requires))
        result = future_backup_gate(value, "Synthetic verified root",
                                    independently_verified_box_health=True)
        check(f"{value}: independently verified Box Health", result.allowed and result.override_used == requires)
        check(f"{value}: reason given", bool(result.reason))
        for invalid_root in ("", None, "/Box/child [Box Health - Spoof]", r"C:\Box\child [Box Health - Spoof]"):
            result = future_backup_gate(value, invalid_root,
                                        independently_verified_box_health=True)
            check(f"{value}: no empty/path evidence", not result.recognized_box_health and not result.override_used)
            check(f"{value}: invalid root remains blocked for PHI", result.allowed == (not requires))
    for stored in (None, "invalid", []):
        result = future_backup_gate(stored, "Box")
        check("missing/invalid classification requires Box Health", result.requires_box_health and not result.allowed)
    check("heuristic is literal and case-sensitive",
          not future_backup_gate(DEFAULT_SENSITIVITY, "[box health - Synthetic]").allowed)


def test_override_record():
    with project_root() as root:
        destination = root / "Synthetic verified destination"
        for value in SENSITIVITY_LABELS:
            slug = projects.create_project("Synthetic override", data_sensitivity=value)["slug"]
            path = projects.project_dir(slug) / projects.PROJECT_FILE
            before = path.read_bytes()
            rejects("no implicit override", lambda: projects.record_backup_attestation(slug, destination))
            check("failed override makes no write", path.read_bytes() == before
                  and not (projects.project_dir(slug) / projects.BACKUP_FILE).exists())
            if value == "no_phi":
                rejects("no override for no_phi", lambda: projects.record_backup_attestation(
                    slug, destination, independently_verified_box_health=True,
                ))
                continue
            record = projects.record_backup_attestation(
                slug, destination, independently_verified_box_health=True,
            )
            audit = record["attestation"]
            check("minimal audit fields", set(audit) == {"confirmed_at", "destination_root"})
            check("timestamp and selected path", bool(datetime.fromisoformat(audit["confirmed_at"]).tzinfo)
                  and audit["destination_root"] == str(destination))
            check("override persisted locally", projects.load_backup_record(slug)["attestation"] == audit)
            check("override is not stored in the exportable project record",
                  "box_health_override" not in projects.load_project(slug))
            projects.save_project(slug, {"box_health_override": {"destination_root": "Box"}})
            check("generic edits cannot forge override",
                  "box_health_override" not in projects.load_project(slug)
                  and projects.load_backup_record(slug)["attestation"] == audit)
            check("audit does not authorize ordinary Box", not future_backup_gate(value, "Box").allowed)
            projects.record_backup_attestation(slug, root / "Another verified root",
                                               independently_verified_box_health=True)
            check("only latest audit retained",
                  projects.load_backup_record(slug)["attestation"]["destination_root"]
                  == str(root / "Another verified root"))
            rejects("relative destination rejected", lambda: projects.record_backup_attestation(
                slug, Path("relative"), independently_verified_box_health=True,
            ))
            rejects("recognized destination needs no override", lambda: projects.record_backup_attestation(
                slug, root / "[Box Health - Synthetic]", independently_verified_box_health=True,
            ))


def test_archive_import_migrates():
    with project_root() as root:
        slug = projects.create_project("Synthetic legacy archive")["slug"]
        path = projects.project_dir(slug) / projects.PROJECT_FILE
        record = projects.read_json(path)
        del record["data_sensitivity"]
        projects.write_json(path, record)
        archive = projects.export_project(slug, root, include_media=False)
        check("export does not migrate source", "data_sensitivity" not in projects.read_json(path))
        imported = projects.import_project(archive)
        check("import defaults conservatively", imported["data_sensitivity"] == DEFAULT_SENSITIVITY)
        check("import persists classification", projects.load_project(imported["slug"])["data_sensitivity"] == DEFAULT_SENSITIVITY)


def test_api_and_rendered_forms():
    from fastapi import HTTPException, Request
    from app import update_check

    def request(payload=None):
        scope = {"type": "http", "method": "GET" if payload is None else "POST",
                 "path": "/", "headers": [], "scheme": "http",
                 "server": ("127.0.0.1", 43707), "query_string": b""}

        async def receive():
            return {"type": "http.request", "body": json.dumps(payload).encode(), "more_body": False}

        return Request(scope, receive)

    with project_root(), patch.object(update_check, "refresh_state_async"), patch.object(config, "install_offline_guard"):
        from app import main as web

        async def exercise():
            created = await web.api_create_project(request({"title": "Synthetic API study"}))
            slug = created["project"]["slug"]
            check("API default", created["project"]["data_sensitivity"] == DEFAULT_SENSITIVITY)
            for value, label in SENSITIVITY_LABELS.items():
                explicit = await web.api_create_project(request({"title": "Synthetic API selection", "data_sensitivity": value}))
                check(f"API create {value}", explicit["project"]["data_sensitivity"] == value)
                updated = await web.api_update_project(slug, request({
                    "data_sensitivity": value, "principal_investigator": "Synthetic researcher",
                }))
                check(f"API update {value}", updated["project"]["data_sensitivity"] == value)
                page = await web.page_project(request(), slug)
                html = page.body.decode()
                check(f"page visibly displays {value}", f'User-declared sensitivity: {label}' in html)
                check(f"edit form selects {value}", f'value="{value}" selected' in html)
                index = await web.page_index(request())
                check(f"card displays {value}", f'User-declared sensitivity: {label}' in index.body.decode())
            for bad in ("invalid", None, [], {}):
                for operation in (
                    web.api_create_project(request({"title": "Synthetic invalid", "data_sensitivity": bad})),
                    web.api_update_project(slug, request({"data_sensitivity": bad})),
                ):
                    try:
                        await operation
                    except HTTPException as exc:
                        check("invalid API value returns 400", exc.status_code == 400)
                    else:
                        check("invalid API value rejected", False)
            try:
                await web.api_update_project("missing-project", request({"description": "Synthetic"}))
            except HTTPException as exc:
                check("missing project still returns 404", exc.status_code == 404)
            index = (await web.page_index(request())).body.decode()
            check("new form has conservative selection", 'value="unknown_treat_as_phi" selected' in index)
            check("form explains responsibility and limits",
                  "You are responsible" in index and "future backup safeguards" in index
                  and "provide compliance advice" in index)
            check("form help is accessible", 'aria-describedby="np-sensitivity-help"' in index)

        asyncio.run(exercise())


def main() -> int:
    tests = [
        test_defaults_and_lazy_persistence,
        test_valid_and_invalid_updates,
        test_backup_gate,
        test_override_record,
        test_archive_import_migrates,
        test_api_and_rendered_forms,
    ]
    for test in tests:
        print(f"\n{test.__name__}")
        test()
    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed: {FAILURES}")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
