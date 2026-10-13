"""Local health checks and model download space estimates."""

from __future__ import annotations

import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import catalog, config, health

FAILURES: list[str] = []


def check(label: str, condition: bool) -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        FAILURES.append(label)


def temp_paths(root: Path):
    for name in ("models", "projects", "logs"):
        (root / name).mkdir()
    return replace(
        config.PATHS,
        models=root / "models",
        projects=root / "projects",
        logs=root / "logs",
        settings_file=root / "settings.json",
    )


def test_storage_and_access_checks_are_local():
    with tempfile.TemporaryDirectory() as tmp:
        paths = temp_paths(Path(tmp))
        with patch.object(
            health.shutil, "disk_usage",
            return_value=SimpleNamespace(free=500_000_000),
        ):
            report = health.run(paths, "tiny")
        by_name = {item["name"]: item for item in report["checks"]}
        check("low free storage is reported", by_name["storage"]["status"] == "warning")
        check("report includes local-only guarantee", report["local_only"] is True)
        with patch.object(health.os, "access", return_value=False):
            report = health.run(paths, "tiny")
        by_name = {item["name"]: item for item in report["checks"]}
        check("inaccessible data directory is reported",
              by_name["data_directory"]["status"] == "error")


def test_missing_and_corrupt_model_data():
    spec = catalog.model_spec("tiny")
    with tempfile.TemporaryDirectory() as tmp:
        paths = temp_paths(Path(tmp))
        missing = health._model_data_check(paths, spec.key)
        check("missing selected model is reported", missing["status"] == "warning")

        repo = paths.models / ("models--" + spec.repo.replace("/", "--"))
        snapshot = repo / "snapshots" / "synthetic-revision"
        snapshot.mkdir(parents=True)
        (snapshot / "model.bin").write_bytes(b"")
        (snapshot / "config.json").write_text("{", encoding="utf-8")
        corrupt = health._model_data_check(paths, spec.key)
        check("empty model weights are reported as corrupt",
              corrupt["status"] == "error")

        (snapshot / "model.bin").write_bytes(b"synthetic weights")
        (snapshot / "config.json").write_text("{}", encoding="utf-8")
        available = health._model_data_check(paths, spec.key)
        check("nonempty local model snapshot is healthy",
              available["status"] == "ok")


def test_runtime_and_download_estimates():
    check("unsupported Python runtime is reported",
          health._runtime_check((3, 14, 0))["status"] == "error")
    check("supported Python runtime passes",
          health._runtime_check((3, 13, 9))["status"] == "ok")
    estimate = catalog.disk_estimate("tiny")
    check("download estimate reserves filesystem overhead",
          estimate["download_bytes"] == 75_000_000
          and estimate["recommended_free_bytes"] == 82_500_000)


def main() -> int:
    for test in (
        test_storage_and_access_checks_are_local,
        test_missing_and_corrupt_model_data,
        test_runtime_and_download_estimates,
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
