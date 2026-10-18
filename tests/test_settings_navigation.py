"""Navigation, Settings sections, and the retired global Box folder setting.

Run with: python -m tests.test_settings_navigation
"""

from __future__ import annotations

import html
import json
import re
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app import config, diagnostics, projects

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        FAILURES.append(label)


def _client():
    from fastapi.testclient import TestClient

    from app import update_check
    update_check.refresh_state_async = lambda: None
    from app import main
    return main, TestClient(main.app)


def test_primary_navigation_and_names():
    _main, client = _client()
    page = client.get("/settings").text
    nav = re.search(r'<nav class="nav".*?</nav>', page, re.S).group(0)
    labels = [html.unescape(re.sub(r"\s+", " ", re.sub("<[^>]+>", "", item))).strip()
              for item in re.findall(r"<a [^>]*>.*?</a>", nav, re.S)]
    check("navigation is Projects, Settings, Activity & Diagnostics, About",
          labels == ["Projects", "Settings", "Activity & Diagnostics", "About"], str(labels))
    check("the Settings page is named Settings",
          "<h1>Settings</h1>" in page and "AI Settings" not in page)
    check("the current page is exposed to assistive technology",
          'href="/settings" class="on"' in page.replace("\n", " ")
          or 'aria-current="page"' in nav)
    activity_page = client.get("/privacy").text
    check("the activity page is named Activity & Diagnostics",
          "<h1>Activity &amp; Diagnostics</h1>" in activity_page
          and "Privacy Center" not in activity_page)


def test_settings_sections_keep_keys_and_defaults():
    _main, client = _client()
    page = client.get("/settings").text
    headings = re.findall(r'<h2 class="section-heading"[^>]*>(.*?)</h2>', page)
    check("Settings has the three sections in order",
          headings == ["AI &amp; Transcription", "Application", "Privacy &amp; Network"],
          str(headings))
    check("each section is reachable from an in-page navigation",
          all(f'href="#{anchor}"' in page
              for anchor in ("ai-transcription", "application", "privacy-network")))

    def section(name):
        start = page.index(f'id="{name}"')
        later = [page.index(f'id="{other}"') for other in
                 ("ai-transcription", "application", "privacy-network")
                 if page.index(f'id="{other}"') > start]
        return page[start:min(later)] if later else page[start:]

    check("transcription options are under AI & Transcription",
          'name="model"' in section("ai-transcription")
          and 'name="beam_size"' in section("ai-transcription"))
    check("application options are under Application",
          'name="autosave_revisions"' in section("application")
          and 'id="local-health"' in section("application")
          and 'id="updates"' in section("application"))
    check("privacy options are under Privacy & Network",
          'name="offline_lock"' in section("privacy-network")
          and 'name="check_for_updates"' in section("privacy-network"))
    check("the privacy anchor used by other pages still exists",
          'id="privacy-settings"' in page)

    keys = [field["key"] for group in config.SETTINGS_SCHEMA for field in group["fields"]]
    check("no setting key was lost or duplicated", len(keys) == len(set(keys))
          and {"offline_lock", "check_for_updates", "keep_preview_audio",
               "autosave_revisions", "box_sync_destination", "model"} <= set(keys))
    check("defaults of moved settings are unchanged",
          config.default_settings()["keep_preview_audio"] is True
          and config.default_settings()["autosave_revisions"] == 20
          and config.default_settings()["offline_lock"] is False
          and config.default_settings()["check_for_updates"] is True)


def test_global_box_folder_is_hidden_but_preserved():
    _main, client = _client()
    page = client.get("/settings").text
    check("the global Box folder control is no longer shown",
          "box_sync_destination" not in page and "browse-box-folder" not in page)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        store = config.SettingsStore(Path(tmp) / "settings.json")
        store.update({"box_sync_destination": "/synthetic/earlier/folder", "offline_lock": True})
        store.update({"autosave_revisions": 10})        # a Save from the page omits the legacy key
        check("saving other settings leaves the earlier shared folder in place",
              store.get("box_sync_destination") == "/synthetic/earlier/folder")
        reset = store.reset(keep=("box_sync_destination",))
        check("Restore defaults keeps the earlier shared folder for migration",
              reset["box_sync_destination"] == "/synthetic/earlier/folder"
              and reset["offline_lock"] is False and reset["autosave_revisions"] == 20)
        reloaded = config.SettingsStore(Path(tmp) / "settings.json")
        check("it survives a restart",
              reloaded.get("box_sync_destination") == "/synthetic/earlier/folder")
        check("a full reset without keep still restores every default",
              store.reset()["box_sync_destination"] == "")


def test_diagnostics_fields_are_allow_listed_and_sanitized():
    class Settings:
        def get(self, key, fallback=None):
            return {"offline_lock": False, "check_for_updates": True,
                    "box_sync_destination": ""}.get(key, fallback)

    with patch.object(config, "SETTINGS", Settings()), patch.object(
        projects, "any_backup_destination", lambda: False
    ):
        normal = diagnostics.build({"state": "ready"})
        with patch("platform.release", lambda: r"C:\Users\someone\secret"), patch(
            "platform.machine", lambda: "/home/someone"
        ):
            hostile = diagnostics.build({"state": "ready"})
    runtime = normal["runtime"]
    check("diagnostics add the platform release and CPU architecture",
          set(runtime) == {"operating_system", "operating_system_release",
                           "architecture", "python_version"})
    check("those fields are short plain tokens",
          all(re.fullmatch(r"[0-9A-Za-z._+ -]{1,32}", str(runtime[key]))
              for key in ("operating_system_release", "architecture")))
    check("a value that looks like a path or name is reported as unknown",
          hostile["runtime"]["operating_system_release"] == "unknown"
          and hostile["runtime"]["architecture"] == "unknown"
          and "someone" not in json.dumps(hostile))
    check("the schema version records the added fields", normal["schema_version"] == 2)
    check("backup state is only a yes/no", normal["privacy"]["box_sync_backup_configured"] is False)


def test_activity_page_sections():
    main, client = _client()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        paths = replace(config.PATHS, projects=root / "projects", logs=root / "logs",
                        models=root / "models", settings_file=root / "settings.json")
        with patch.object(projects, "PATHS", paths), patch.object(config, "PATHS", paths):
            with_folder = projects.create_project("Synthetic with folder")
            projects.create_project("Synthetic without folder")
            folder = root / "Synthetic Box"
            folder.mkdir()
            projects.set_backup_destination(with_folder["slug"], folder)
            page = client.get("/privacy").text
    check("storage paths sit in a collapsed Storage locations section",
          re.search(r'<details class="card disclosure" id="storage-locations">', page)
          is not None and 'id="storage-locations" open' not in page)
    check("diagnostics sit in a collapsed Troubleshooting section",
          re.search(r'<details class="card disclosure" id="diagnostics">', page)
          is not None and "<summary><h2 style=\"display:inline\">Troubleshooting</h2>" in page
          and 'id="diagnostics" open' not in page)
    check("preview-before-save and the inspected confirmation remain",
          'id="preview-diagnostics"' in page and 'id="diagnostics-inspected"' in page
          and 'id="save-diagnostics"' in page and "disabled" in page)
    check("the backup summary counts per-project folders, not a global one",
          "1 of 2" in page and "Backup folders" in page)
    check("no storage path appears outside the collapsed section",
          str(root) not in page.split('id="storage-locations"')[0])


def main() -> int:
    for test in (
        test_primary_navigation_and_names,
        test_settings_sections_keep_keys_and_defaults,
        test_global_box_folder_is_hidden_but_preserved,
        test_diagnostics_fields_are_allow_listed_and_sanitized,
        test_activity_page_sections,
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
