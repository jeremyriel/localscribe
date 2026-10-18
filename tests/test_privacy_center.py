"""Privacy Center history and diagnostics data-minimization tests."""

from __future__ import annotations

import json
import tempfile
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from app import activity, config, diagnostics

FAILURES: list[str] = []


class FakeSettings:
    values = {
        "offline_lock": True,
        "check_for_updates": False,
        "box_sync_destination": "/private/user/Box Sync",
    }

    def get(self, key, fallback=None):
        return self.values.get(key, fallback)


def check(label: str, condition: bool) -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        FAILURES.append(label)


def test_diagnostics_are_allowlisted_and_path_free():
    sensitive = {
        "selected_model": "/private/user/transcript-token-SECRET",
        "target_device": "/private/user/token-SECRET",
        "state": "ready",
        "model_downloaded": True,
    }
    with patch.object(config, "SETTINGS", FakeSettings()):
        report = diagnostics.build(sensitive)
    encoded = json.dumps(report)
    check("diagnostics omit model names and user paths",
          "selected_model" not in encoded and "/private/user" not in encoded)
    check("diagnostics omit token and transcript sentinels",
          "SECRET" not in encoded and "transcript" not in encoded.lower())
    check("diagnostics report only safe state and backup booleans",
          report["engine"]["state"] == "ready"
          and report["privacy"]["offline_lock"]
          and report["privacy"]["box_sync_backup_configured"])


def test_activity_records_network_actions_without_free_text():
    with tempfile.TemporaryDirectory() as tmp:
        paths = replace(config.PATHS, logs=Path(tmp) / "logs")
        with patch.object(config, "PATHS", paths):
            activity.record("update_check", outcome="started")
            activity.record(
                "snapshot_create",
                "/private/user/Project title",
                "transcript token SECRET",
            )
            events = activity.recent()
            stored = (paths.logs / "project-activity.json").read_text(
                encoding="utf-8"
            )
    check("history classifies network and recovery actions",
          [event["category"] for event in events] == ["network", "recovery"])
    check("history excludes arbitrary outcome text and user paths",
          "SECRET" not in stored and "/private/user" not in stored
          and events[-1]["outcome"] == "completed")


def test_history_rows_are_readable_and_merge_started_results():
    day = "2026-10-12T20:16:23-05:00"
    events = [
        {"timestamp": day, "action": "update_check", "outcome": "started"},
        {"timestamp": "2026-10-12T20:16:24-05:00", "action": "update_check",
         "outcome": "completed"},
        {"timestamp": "2026-10-12T20:17:46-05:00", "action": "document_delete",
         "outcome": "deleted_without_snapshot",
         "project_ref": activity._project_ref("synthetic-1")},
        {"timestamp": "2026-10-12T20:18:00-05:00", "action": "box_sync_backup",
         "outcome": "failed", "project_ref": activity._project_ref("gone")},
        {"timestamp": "2026-10-12T20:19:00-05:00", "action": "model_download",
         "outcome": "started"},
        {"timestamp": "2026-10-12T20:20:00-05:00", "action": "other", "outcome": "completed"},
    ]
    titles = activity.title_index([{"project_id": "synthetic-1", "title": "Synthetic study"}])
    rows = activity.history_rows(events, titles, now=datetime.fromisoformat(day))
    check("a started event and its result become one row",
          [r["summary"] for r in rows if "update" in r["summary"].lower()]
          == ["Checked for updates"])
    check("history is newest first and skips uncategorised events",
          len(rows) == 4 and rows[0]["summary"] == "Started downloading a speech model")
    check("an unfinished start is labelled, not shown as success",
          rows[0]["status"] == "started" and rows[0]["status_label"] == "No result recorded")
    by_summary = {r["summary"]: r for r in rows}
    deleted = by_summary["Removed a recording without a recovery snapshot"]
    check("project titles are resolved for existing projects",
          deleted["project"] == "Synthetic study")
    failed = by_summary["A Box Sync backup failed"]
    check("deleted projects are named generically and failures are flagged",
          failed["project"] == "A deleted or unknown project" and failed["status"] == "failed")
    check("no raw identifiers or ISO text in the summary",
          all("_" not in r["summary"] and "T20:" not in r["summary"] for r in rows))
    check("dates read as Today and times as 12-hour clock",
          all(r["day"] == "Today" and r["time"][-2:] in ("AM", "PM") for r in rows))


def test_history_filters_group_backups_with_both_views():
    stamp = "2026-10-12T20:16:23-05:00"
    now = datetime.fromisoformat(stamp)
    ref = activity._project_ref("synthetic-1")
    events = [
        {"timestamp": stamp, "action": "update_check", "outcome": "completed"},
        {"timestamp": stamp, "action": "box_sync_backup", "outcome": "completed", "project_ref": ref},
        {"timestamp": stamp, "action": "snapshot_create", "outcome": "completed", "project_ref": ref},
        {"timestamp": stamp, "action": "project_delete", "outcome": "deleted_with_snapshot"},
        {"timestamp": stamp, "action": "not_allow_listed", "outcome": "completed"},
    ]
    rows = activity.history_rows(events, now=now)
    by_action = {r["summary"]: r["groups"].split() for r in rows}
    check("an update check is only an online event",
          by_action["Checked for updates"] == ["network"])
    check("a Box Sync backup is both online and data safety",
          by_action["Backed up a project to your Box Sync folder"] == ["network", "recovery"])
    check("snapshots and deletions are data safety",
          by_action["Created a recovery snapshot"] == ["recovery"]
          and by_action["Deleted a project after saving a recovery snapshot"] == ["recovery"])
    check("events outside the allow-list are not shown", len(rows) == 4)
    counts = activity.filter_counts(rows)
    check("filter counts match what each view shows",
          counts == {"all": 4, "network": 2, "recovery": 3})
    check("technical details carry only a short one-way project reference",
          all(len(r["ref"]) in (0, 8) and ref not in r["detail"] for r in rows))
    check("every row states its outcome in words",
          all(r["status_label"] in activity.STATUS_LABELS.values() for r in rows))
    check("the filters can be told apart in text",
          counts["recovery"] != counts["network"])


def test_activity_page_renders_filters_and_stays_content_free():
    from fastapi.testclient import TestClient

    from app import projects, update_check

    update_check.refresh_state_async = lambda: None
    from app import main

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        paths = replace(
            config.PATHS, projects=root / "projects", logs=root / "logs",
            models=root / "models", settings_file=root / "settings.json",
        )
        with patch.object(projects, "PATHS", paths), patch.object(config, "PATHS", paths):
            project = projects.create_project("Synthetic Title Sentinel")
            pid = project["project_id"]
            activity.record("update_check", outcome="started")
            activity.record("update_check", outcome="completed")
            activity.record("box_sync_backup", pid, "failed")
            activity.record("snapshot_create", pid)
            stored = (paths.logs / "project-activity.json").read_text(encoding="utf-8")
            page = TestClient(main.app).get("/privacy").text
    check("the stored log never contains the project title or paths",
          "Synthetic Title Sentinel" not in stored and str(root) not in stored)
    check("the page names the project it resolved at render time",
          "Synthetic Title Sentinel" in page)
    check("the three filters are real radio inputs with counts",
          'name="history-type" value="all" checked> All (3)' in page
          and 'value="network"> Online (2)' in page
          and 'value="recovery"> Data safety (2)' in page)
    check("the live status line and per-filter empty explanations exist",
          'id="history-status" role="status" aria-live="polite"' in page
          and 'data-empty-network=' in page and 'data-empty-recovery=' in page)
    check("rows carry their filter groups and a text outcome",
          'data-groups="network recovery"' in page and ">Failed<" in page)
    check("the filter script is loaded before the page script",
          page.index("/static/js/history.js") < page.index("/static/js/privacy.js"))
    client = TestClient(main.app)
    asset = client.get("/static/js/privacy.js")
    check("scripts revalidate instead of being reused by heuristic freshness",
          asset.headers.get("cache-control") == "no-cache" and bool(asset.headers.get("etag")))
    check("a conditional request gets a cheap 304",
          client.get("/static/js/privacy.js",
                     headers={"If-None-Match": asset.headers["etag"]}).status_code == 304)


def main() -> int:
    for test in (
        test_diagnostics_are_allowlisted_and_path_free,
        test_activity_records_network_actions_without_free_text,
        test_history_rows_are_readable_and_merge_started_results,
        test_history_filters_group_backups_with_both_views,
        test_activity_page_renders_filters_and_stays_content_free,
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
