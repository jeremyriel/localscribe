"""The Local Scribe web application.

Served on 127.0.0.1:43707 only. Three guards keep it isolated from the other
local Python services that may be running alongside it:

1. The listener binds loopback, so nothing off-machine can reach it.
2. Mutating routes and the console WebSocket validate the request's Origin and
   Host against this instance's own address, and carry an instance token, so a
   page belonging to another localhost project cannot drive this one.
3. No CORS headers are ever emitted, so a browser refuses cross-origin reads.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import zipfile
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import (
    activity,
    box_sync,
    catalog,
    diagnostics,
    deidentification,
    exporters,
    folder_picker,
    health,
    handoff,
    languages,
    media,
    proofread,
    projects,
    ranged,
    review,
    update_check,
    validation,
    vocabulary,
    winlibs,
)
from .config import (
    APP_NAME,
    HOST,
    PATHS,
    SETTINGS,
    SETTINGS_SCHEMA,
    app_version,
    install_offline_guard,
    instance_identity,
    new_instance_token,
    resolve_port,
)
from .console import CONSOLE, fmt_bytes
from .engine import ENGINE, EngineError
from .jobs import QUEUE
from .sensitivity import SENSITIVITY_LABELS
from .realign import describe_report, retimestamp
from .transcript import (
    LOW_CONFIDENCE,
    PALETTE,
    apply_edits,
    assign_speaker,
    merge_segment,
    normalise,
    set_roster,
    split_segment,
    speaker_roster,
    summary,
    turns,
)

PORT = resolve_port()
INSTANCE_TOKEN = new_instance_token()

PATHS.ensure()
install_offline_guard()
update_check.refresh_state_async()

app = FastAPI(title=APP_NAME, version=app_version(), docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(PATHS.static)), name="static")
templates = Jinja2Templates(directory=str(PATHS.templates))

ALLOWED_ORIGINS = {
    f"http://{HOST}:{PORT}",
    f"http://localhost:{PORT}",
}


# ---------------------------------------------------------------------------
# Isolation guards
# ---------------------------------------------------------------------------

@app.middleware("http")
async def same_origin_guard(request: Request, call_next):
    """Reject cross-origin state changes from other local services.

    Several Python web apps may be running on this machine at once. A page
    served by one of them must not be able to POST to this one, so any request
    that changes state has to carry an Origin this instance recognises.
    """
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and origin not in ALLOWED_ORIGINS:
            CONSOLE.warn(
                "Rejected a cross-origin request from another local service.",
                origin=origin, path=request.url.path,
            )
            return JSONResponse(
                {"error": "Cross-origin requests are not accepted."},
                status_code=403,
            )
    response = await call_next(request)
    # Never advertise cross-origin access, and keep the app out of caches.
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    # Static files carry Last-Modified but no freshness lifetime, so browsers reuse
    # an old copy by heuristic and a fixed template runs with stale JS/CSS.
    # no-cache still allows cheap ETag revalidation (a 304) on every load.
    response.headers.setdefault("Cache-Control", "no-cache")
    return response


@app.exception_handler(Exception)
async def unexpected_error(request: Request, exc: Exception):
    """Give the page a readable message; the exception type is logged, never its content."""
    CONSOLE.error(
        f"Unexpected error handling {request.method} {request.url.path}: {type(exc).__name__}"
    )
    if request.url.path.startswith("/api/"):
        return JSONResponse(
            {"detail": "Local Scribe hit an unexpected problem. See the console "
                       "for the error type."},
            status_code=500,
        )
    return Response("Internal Server Error", status_code=500, media_type="text/plain")


def require_same_origin_ws(websocket: WebSocket) -> bool:
    origin = websocket.headers.get("origin")
    if origin is None:
        return True  # non-browser client on loopback
    return origin in ALLOWED_ORIGINS


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def on_startup() -> None:
    CONSOLE.bind_loop(asyncio.get_running_loop())
    CONSOLE.register_state_provider("engine", lambda: {
        "state": ENGINE.status()["state"],
        "ready": ENGINE.status()["ready"],
        "model": ENGINE.status()["loaded_model"] or ENGINE.status()["selected_model"],
        "device": ENGINE.status()["loaded_device"] or ENGINE.status()["target_device"],
    })
    CONSOLE.register_state_provider("worker", QUEUE.heartbeat_state)
    CONSOLE.start_heartbeat()
    QUEUE.start()

    CONSOLE.success(f"{APP_NAME} {app_version()} started", port=PORT, host=HOST)
    CONSOLE.info(f"Interface available at http://{HOST}:{PORT}")

    if winlibs.IS_WINDOWS:
        CONSOLE.debug(
            "Native library check",
            loadable=winlibs.probe(),
            search_dirs=winlibs.search_dirs(),
        )

    status = ENGINE.status()
    for warning in status["warnings"]:
        CONSOLE.warn(warning)
    for note in status["notes"]:
        CONSOLE.info(note)

    if not status["model_downloaded"]:
        spec = catalog.model_spec(status["selected_model"])
        CONSOLE.warn(
            f"No Whisper model is downloaded yet. Open Settings and "
            f"download {spec.label if spec else status['selected_model']} "
            f"({spec.download_mb if spec else '?'} MB) before transcribing.",
        )
    else:
        CONSOLE.info(
            f"Model {status['selected_model']} is on disk and will load on "
            "the first transcription."
        )
    if status["offline_lock"]:
        CONSOLE.info("Offline lock is engaged: all outbound network access is blocked.")


@app.on_event("shutdown")
async def on_shutdown() -> None:
    CONSOLE.info("Shutting down")
    await CONSOLE.stop_heartbeat()
    QUEUE.stop()


# ---------------------------------------------------------------------------
# Shared template context
# ---------------------------------------------------------------------------

def base_context(request: Request, **extra) -> dict:
    context = {
        "request": request,
        "app_name": APP_NAME,
        "version": app_version(),
        "port": PORT,
        "instance_token": INSTANCE_TOKEN,
        "engine": ENGINE.status(),
        "low_confidence": LOW_CONFIDENCE,
        "update": update_check.state(),
        "sensitivity_labels": SENSITIVITY_LABELS,
    }
    context.update(extra)
    return context


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def page_index(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        base_context(
            request,
            projects=projects.list_projects(),
            snapshots=projects.list_all_snapshots(),
            activities=activity.history_rows([
                event for event in activity.recent(limit=activity.HISTORY_LIMIT)
                if "recovery" in activity.groups(event.get("action"))
            ])[:10],
        ),
    )


@app.get("/project/{slug}", response_class=HTMLResponse)
async def page_project(request: Request, slug: str):
    try:
        project = projects.load_project(slug)
    except projects.ProjectError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    profiles = vocabulary.list_profiles()
    return templates.TemplateResponse(
        request,
        "project.html",
        base_context(
            request,
            project=project,
            documents=projects.list_documents(slug),
            stats=projects.project_stats(slug),
            snapshots=projects.list_snapshots(slug),
            activities=activity.history_rows([
                event for event in activity.recent(
                    project.get("project_id") or project["slug"],
                    limit=activity.HISTORY_LIMIT,
                )
                if "recovery" in activity.groups(event.get("action"))
            ])[:10],
            backup=box_sync.describe(slug),
            vocabulary_profiles=profiles,
            vocabulary_profile_available=any(
                item["id"] == project.get("vocabulary_profile_id")
                for item in profiles
            ),
            accepted=sorted(media.MEDIA_EXTENSIONS),
            language_choices=languages.choices(),
            default_language=languages.coerce(
                project.get("transcription_language") or SETTINGS.get("language")
            ),
            operation=projects.project_operation(project),
            terms=languages.terms(projects.project_operation(project)),
            operation_info=operation_info(),
        ),
    )


def operation_info() -> dict:
    """What the project page needs to explain each operation immediately.

    Computed once on the server (so the rules live in one place) and handed to
    the page, which switches between the two entries without a round trip.
    """
    status = ENGINE.status()
    hardware = ENGINE.hardware()
    key = status["selected_model"]
    spec = catalog.model_spec(key)
    compat = {}
    for operation in languages.OPERATIONS:
        problem = catalog.operation_problem(key, operation, hardware)
        support = (
            catalog.translation_support(spec)
            if operation == languages.TRANSLATE else {"level": catalog.OK, "reason": ""}
        )
        compat[operation] = {
            "ok": problem is None,
            "level": support["level"],
            "message": problem or support["reason"],
        }
    return {
        "model": {
            "key": key,
            "label": status["selected_model_label"],
            "downloaded": bool(status["model_downloaded"]),
        },
        "compat": compat,
        "suggestion": catalog.suggest_translation_model(hardware),
        "terms": languages.TERMS,
    }


@app.get("/project/{slug}/document/{doc_id}", response_class=HTMLResponse)
async def page_document(request: Request, slug: str, doc_id: str):
    try:
        project = projects.load_project(slug)
        record = projects.load_document(slug, doc_id)
    except projects.ProjectError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    doc = projects.load_transcript(slug, doc_id)
    return templates.TemplateResponse(
        request,
        "document.html",
        base_context(
            request,
            project=project,
            document=record,
            transcript=doc,
            has_transcript=doc is not None,
            info=summary(doc) if doc else None,
            doc_terms=languages.terms(
                languages.operation_of(doc) if doc else (
                    record.get("pending_operation") or projects.project_operation(project)
                )
            ),
            outputs=projects.list_outputs(slug, doc_id),
            revisions=projects.list_revisions(slug, doc_id),
            speakers=speaker_roster(doc) if doc else [],
            workflow=validation.snapshot(doc),
            palette=list(PALETTE),
            pause_gap=float(SETTINGS.get("paragraph_gap") or 1.5),
            proofreading={
                "spelling": bool(SETTINGS.get("proofread_spelling", True)),
                "artefacts": bool(SETTINGS.get("proofread_artefacts", True)),
                "available": proofread.DICTIONARY.available,
                # The checks are English-only; the editor switches them off
                # and says why for any other transcript language.
                "language_supported": proofread.supports_document(doc),
                "language": languages.output_language(doc or {}),
                "language_name": languages.name(languages.output_language(doc or {})),
            },
            spaceless=languages.doc_is_spaceless(doc or {}),
        ),
    )


@app.get("/settings", response_class=HTMLResponse)
async def page_settings(request: Request):
    hardware = ENGINE.hardware(refresh=True)
    return templates.TemplateResponse(
        request,
        "settings.html",
        base_context(
            request,
            schema=SETTINGS_SCHEMA,
            values=SETTINGS.all(),
            catalog=catalog.catalog_payload(hardware),
            health=health.run(model_key=SETTINGS.get("model")),
            advanced_raw=json.dumps(SETTINGS.get("advanced_raw") or {}, indent=2),
            is_git_checkout=update_check.is_git_checkout(),
        ),
    )


@app.get("/privacy", response_class=HTMLResponse)
async def page_privacy(request: Request):
    events = activity.recent(limit=activity.HISTORY_LIMIT)
    listing = projects.list_projects()
    rows = activity.history_rows(events, activity.title_index(listing))
    backup_summary = {
        "total": len(listing),
        "with_folder": sum(
            bool(projects.load_backup_record(p["slug"])["destination"]) for p in listing
        ),
    }
    return templates.TemplateResponse(
        request,
        "privacy.html",
        base_context(
            request,
            values=SETTINGS.all(),
            backup_summary=backup_summary,
            storage_paths={
                "projects": str(PATHS.projects),
                "models": str(PATHS.models),
                "logs": str(PATHS.logs),
                "settings": str(PATHS.settings_file),
            },
            activities=rows,
            activity_counts=activity.filter_counts(rows),
            activity_labels=activity.LABELS,
        ),
    )


@app.get("/about", response_class=HTMLResponse)
async def page_about(request: Request):
    return templates.TemplateResponse(
        request,
        "about.html",
        base_context(
            request,
            hardware=ENGINE.hardware(),
            models=catalog.downloaded_models(),
            paths={
                "projects": str(PATHS.projects),
                "models": str(PATHS.models),
                "logs": str(PATHS.logs),
                "settings": str(PATHS.settings_file),
            },
        ),
    )


# ---------------------------------------------------------------------------
# Instance identity and status
# ---------------------------------------------------------------------------

@app.get("/api/instance")
async def api_instance():
    """Identity probe used by the launcher to recognise a stale instance.

    The token is deliberately included: the launcher only needs the signature,
    but a same-origin page uses the token to prove it belongs to this instance.
    """
    return instance_identity(PORT, INSTANCE_TOKEN)


@app.get("/api/status")
async def api_status():
    return {
        "engine": ENGINE.status(),
        "queue": QUEUE.snapshot(limit=20),
        "uptime": round(CONSOLE.uptime, 1),
        "version": app_version(),
        "offline_lock": bool(SETTINGS.get("offline_lock")),
    }


@app.get("/api/console")
async def api_console(since: int = 0, limit: int = 500):
    return {"events": CONSOLE.history(since=since, limit=limit)}


@app.get("/api/diagnostics")
async def api_diagnostics():
    """Preview an allow-listed diagnostics document; never persists it."""
    return diagnostics.build(ENGINE.status())


@app.get("/api/health")
async def api_health():
    """Inspect local storage and runtime state without making network requests."""
    return health.run(model_key=SETTINGS.get("model"))


@app.websocket("/ws/console")
async def ws_console(websocket: WebSocket):
    if not require_same_origin_ws(websocket):
        await websocket.close(code=1008)
        CONSOLE.warn("Rejected a console WebSocket from a foreign origin.",
                     origin=websocket.headers.get("origin"))
        return

    await websocket.accept()
    token = websocket.query_params.get("token")
    if token != INSTANCE_TOKEN:
        # A page from a different Local Scribe instance (or another app) tried
        # to attach to this console.
        await websocket.send_json({
            "seq": 0, "level": "error", "ts": "", "t": 0.0,
            "message": "This console belongs to a different Local Scribe "
                       "instance. Reload the page.",
            "data": {},
        })
        await websocket.close(code=1008)
        return

    queue_ = CONSOLE.subscribe()
    try:
        await websocket.send_json({
            "type": "hello",
            "history": CONSOLE.history(limit=400),
            "heartbeat": CONSOLE.heartbeat_payload(),
        })
        while True:
            event = await queue_.get()
            await websocket.send_json(event)
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    except Exception:
        pass
    finally:
        CONSOLE.unsubscribe(queue_)


# ---------------------------------------------------------------------------
# Settings API
# ---------------------------------------------------------------------------

@app.get("/api/settings")
async def api_get_settings():
    return {"values": SETTINGS.all(), "schema": SETTINGS_SCHEMA}


@app.post("/api/folder-picker")
async def api_folder_picker(request: Request):
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    initial_path = payload.get("initial_path", "") if isinstance(payload, dict) else ""
    try:
        selected = await asyncio.to_thread(
            folder_picker.choose_folder, str(initial_path or "")
        )
    except folder_picker.FolderPickerUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="A desktop folder picker is unavailable. Enter the folder path manually.",
        ) from exc
    return {"path": selected}


@app.post("/api/settings")
async def api_save_settings(request: Request):
    payload = await request.json()
    incoming = payload.get("values") if isinstance(payload, dict) else None
    if not isinstance(incoming, dict):
        raise HTTPException(status_code=400, detail="Expected a 'values' object.")

    if "advanced_raw" in incoming and isinstance(incoming["advanced_raw"], str):
        text = incoming["advanced_raw"].strip()
        if not text:
            incoming["advanced_raw"] = {}
        else:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"Advanced parameters are not valid JSON: {exc}",
                ) from exc
            if not isinstance(parsed, dict):
                raise HTTPException(
                    status_code=400,
                    detail="Advanced parameters must be a JSON object.",
                )
            incoming["advanced_raw"] = parsed

    before = SETTINGS.all()
    values = SETTINGS.update(incoming)

    changed = {k: v for k, v in values.items() if before.get(k) != v}
    if changed:
        CONSOLE.info(
            f"Settings updated: {', '.join(sorted(changed))}",
            **{
                k: v for k, v in changed.items()
                if k != "box_sync_destination" and not isinstance(v, (dict, list))
            },
        )
        if "box_sync_destination" in changed:
            CONSOLE.info(
                "Local Box Sync folder configuration updated.",
                configured=bool(changed["box_sync_destination"]),
            )
    if "offline_lock" in changed:
        activity.record(
            "offline_lock_changed",
            outcome="engaged" if changed["offline_lock"] else "released",
        )
        CONSOLE.warn(
            "Offline lock engaged: outbound network access is now blocked."
            if changed["offline_lock"] else
            "Offline lock released: model downloads are possible again."
        )
    if {"check_for_updates", "box_sync_destination"} & set(changed):
        activity.record("network_settings_changed", outcome="completed")

    reload_keys = {"model", "device", "compute_type", "cpu_threads", "num_workers"}
    if reload_keys & set(changed):
        ENGINE.unload()
        CONSOLE.info("Engine settings changed; the model will reload on next use.")

    return {"values": values, "engine": ENGINE.status(), "changed": sorted(changed)}


@app.post("/api/settings/reset")
async def api_reset_settings():
    before = SETTINGS.all()
    values = SETTINGS.reset(keep=("box_sync_destination",))
    if before.get("offline_lock") != values.get("offline_lock"):
        activity.record(
            "offline_lock_changed",
            outcome="engaged" if values["offline_lock"] else "released",
        )
    if any(before.get(key) != values.get(key) for key in (
        "check_for_updates", "box_sync_destination",
    )):
        activity.record("network_settings_changed", outcome="completed")
    ENGINE.unload()
    CONSOLE.warn("Settings restored to defaults.")
    return {"values": values, "engine": ENGINE.status()}


@app.get("/api/models")
async def api_models(refresh: bool = False):
    return catalog.catalog_payload(ENGINE.hardware(refresh=refresh))


@app.post("/api/models/{model_key}/download")
async def api_download_model(model_key: str):
    spec = catalog.model_spec(model_key)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"Unknown model '{model_key}'.")
    if catalog.is_downloaded(model_key):
        return {
            "already": True,
            "job": None,
            "disk_estimate": catalog.disk_estimate(model_key),
        }
    if SETTINGS.get("offline_lock"):
        raise HTTPException(
            status_code=409,
            detail="The offline lock is engaged. Turn it off in Settings to "
                   "download a model, then turn it back on.",
        )
    job = QUEUE.submit_download(model_key)
    return {
        "already": False,
        "job": job.to_dict(),
        "disk_estimate": catalog.disk_estimate(model_key),
    }


@app.delete("/api/models/{model_key}")
async def api_delete_model(model_key: str):
    spec = catalog.model_spec(model_key)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"Unknown model '{model_key}'.")
    freed = catalog.model_disk_bytes(model_key)
    if catalog.delete_model(model_key):
        if ENGINE.status()["loaded_model"] == model_key:
            ENGINE.unload()
        CONSOLE.warn(f"Deleted model {spec.label}, freeing {fmt_bytes(freed)}")
        return {"deleted": True, "freed_bytes": freed}
    return {"deleted": False}


@app.post("/api/engine/load")
async def api_engine_load():
    try:
        ENGINE.ensure_loaded()
    except EngineError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"engine": ENGINE.status()}


@app.post("/api/engine/unload")
async def api_engine_unload():
    ENGINE.unload()
    CONSOLE.info("Model unloaded at the user's request; memory released.")
    return {"engine": ENGINE.status()}


@app.post("/api/engine/gpu-support")
async def api_install_gpu_support():
    """Install the CUDA runtime libraries CTranslate2 needs, via pip."""
    import subprocess
    import sys

    if SETTINGS.get("offline_lock"):
        raise HTTPException(
            status_code=409,
            detail="The offline lock is engaged, so packages cannot be "
                   "downloaded. Turn it off first.",
        )

    req = PATHS.root / "requirements-gpu.txt"
    if not req.exists():
        raise HTTPException(status_code=500, detail="requirements-gpu.txt is missing.")

    activity.record("gpu_support_install", outcome="started")
    CONSOLE.step("Installing GPU support libraries (cuBLAS 12, cuDNN 9) via pip")
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", str(req)],
            capture_output=True, text=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        activity.record("gpu_support_install", outcome="failed")
        CONSOLE.error("GPU support installation could not start.")
        raise HTTPException(
            status_code=500,
            detail="GPU support installation could not start. See the console.",
        ) from exc
    tail = (proc.stdout or "").strip().splitlines()[-6:]
    for line in tail:
        CONSOLE.debug(f"  pip: {line}")

    if proc.returncode != 0:
        activity.record("gpu_support_install", outcome="failed")
        CONSOLE.error("GPU support installation failed.", returncode=proc.returncode)
        CONSOLE.debug("pip stderr", stderr=(proc.stderr or "")[-2000:])
        raise HTTPException(
            status_code=500,
            detail="pip could not install the GPU libraries. See the console "
                   "for details.",
        )

    activity.record("gpu_support_install", outcome="completed")
    winlibs.prepare()  # put the new site-packages/nvidia/*/bin on the path
    hardware = ENGINE.hardware(refresh=True)
    ENGINE.unload()
    restart_required = ENGINE.cuda_runtime_failed
    if restart_required:
        CONSOLE.warn(
            "GPU support installed. Restart Local Scribe to start using the "
            "GPU; this session already fell back to CPU."
        )
    elif hardware.get("cuda_usable"):
        CONSOLE.success("GPU support installed and CUDA is now usable.")
    else:
        CONSOLE.warn(
            "GPU libraries installed, but CUDA is still unavailable. "
            + " ".join(hardware.get("warnings") or [])
        )
    return {
        "engine": ENGINE.status(),
        "hardware": hardware,
        "restart_required": restart_required,
    }


@app.get("/api/update-check")
async def api_update_check():
    return {
        "update": update_check.state(),
        "is_git_checkout": update_check.is_git_checkout(),
    }


@app.post("/api/update-check/refresh")
async def api_update_check_refresh():
    if SETTINGS.get("offline_lock"):
        raise HTTPException(
            status_code=409,
            detail="The offline lock is engaged, so Local Scribe cannot check "
                   "for updates. Turn it off first.",
        )
    state = update_check.refresh_state()
    return {"update": state, "is_git_checkout": update_check.is_git_checkout()}


@app.post("/api/update/apply")
async def api_update_apply():
    if not update_check.is_git_checkout():
        raise HTTPException(
            status_code=409,
            detail="This is a packaged install, not a git checkout - download "
                   "the new version instead of updating in place.",
        )
    if SETTINGS.get("offline_lock"):
        raise HTTPException(
            status_code=409,
            detail="The offline lock is engaged, so Local Scribe cannot pull "
                   "an update. Turn it off first.",
        )
    activity.record("update_apply", outcome="started")
    try:
        result = update_check.apply_git_update()
    except Exception:
        activity.record("update_apply", outcome="failed")
        raise
    activity.record(
        "update_apply",
        outcome="completed" if result["ok"] else "failed",
    )
    if not result["ok"]:
        raise HTTPException(status_code=500, detail=result["message"])
    return result


# ---------------------------------------------------------------------------
# Projects API
# ---------------------------------------------------------------------------

@app.get("/api/projects")
async def api_projects():
    return {"projects": projects.list_projects()}


@app.post("/api/projects")
async def api_create_project(request: Request):
    payload = await request.json()
    title = (payload.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="A project title is required.")
    profile_id = payload.get("vocabulary_profile_id")
    if profile_id and not vocabulary.get_profile(str(profile_id)):
        raise HTTPException(status_code=400, detail="The selected vocabulary profile was not found.")
    # `title` is passed positionally, so it must not also arrive as a keyword.
    try:
        record = projects.create_project(title, **{
            k: payload.get(k)
            for k in projects.EDITABLE_FIELDS
            if k in payload and k != "title"
        })
    except projects.ProjectValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    CONSOLE.success(f"Created project '{record['title']}'", slug=record["slug"])
    return {"project": record}


@app.get("/api/projects/{slug}")
async def api_project(slug: str):
    try:
        return {
            "project": projects.load_project(slug),
            "documents": projects.list_documents(slug),
            "stats": projects.project_stats(slug),
        }
    except projects.ProjectError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.put("/api/projects/{slug}")
async def api_update_project(slug: str, request: Request):
    payload = await request.json()
    profile_id = payload.get("vocabulary_profile_id")
    if profile_id and not vocabulary.get_profile(str(profile_id)):
        raise HTTPException(status_code=400, detail="The selected vocabulary profile was not found.")
    try:
        record = projects.save_project(slug, payload)
    except projects.ProjectValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except projects.ProjectError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    CONSOLE.info(f"Saved metadata for '{record['title']}'", slug=slug)
    return {"project": record}


@app.get("/api/vocabulary-profiles")
async def api_vocabulary_profiles():
    return {"profiles": vocabulary.list_profiles()}


@app.post("/api/vocabulary-profiles")
async def api_create_vocabulary_profile(request: Request):
    payload = await request.json()
    try:
        profile = vocabulary.create_profile(
            payload.get("name"),
            payload.get("terms"),
            target=payload.get("target", "initial_prompt"),
            settings=payload.get("settings"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"profile": profile}


@app.post("/api/vocabulary-profiles/import")
async def api_import_vocabulary_profile(
    file: UploadFile,
    name: str = Form(""),
    target: str = Form("initial_prompt"),
):
    filename = file.filename or ""
    try:
        profile = vocabulary.import_profile(
            name or Path(filename).stem,
            filename,
            await file.read(vocabulary.MAX_PROFILE_BYTES + 1),
            target=target,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"profile": profile}


@app.delete("/api/vocabulary-profiles/{profile_id}")
async def api_delete_vocabulary_profile(profile_id: str):
    assigned = [
        project["slug"] for project in projects.list_projects()
        if project.get("vocabulary_profile_id") == profile_id
    ]
    if assigned:
        raise HTTPException(
            status_code=409,
            detail="Unassign this profile from every project before deleting it.",
        )
    if not vocabulary.delete_profile(profile_id):
        raise HTTPException(status_code=404, detail="Vocabulary profile was not found.")
    return {"deleted": True}


@app.get("/api/projects/{slug}/uncertain-terms.csv")
async def api_uncertain_terms_report(slug: str):
    try:
        projects.load_project(slug)
        documents = projects.list_documents(slug)
    except projects.ProjectError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["term", "confidence", "file", "start_seconds"])
    for record in documents:
        doc = projects.load_transcript(slug, record["id"])
        if not isinstance(doc, dict):
            continue
        for segment in doc.get("segments") or []:
            if not isinstance(segment, dict):
                continue
            for word in segment.get("words") or []:
                if not isinstance(word, dict):
                    continue
                try:
                    confidence = float(word.get("prob", 1.0))
                except (TypeError, ValueError):
                    confidence = 1.0
                term = str(word.get("w") or "").strip()
                if not term or confidence >= LOW_CONFIDENCE:
                    continue
                writer.writerow([
                    term, f"{confidence:.4f}", record.get("filename") or "",
                    word.get("start", segment.get("start", "")),
                ])
    filename = f"{projects.slugify(slug)}-uncertain-terms.csv"
    return Response(
        output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _create_snapshot_or_fail(slug: str, project: dict, **options) -> dict:
    """Create a recovery snapshot; on failure say so and leave the project untouched."""
    try:
        return projects.create_snapshot(slug, **options)
    except projects.ProjectError:
        raise
    except Exception as exc:
        activity.record(
            "snapshot_create", project.get("project_id") or project["slug"], "failed",
        )
        CONSOLE.error(
            f"Could not create a recovery snapshot ({type(exc).__name__}); "
            "nothing was changed.", slug=slug,
        )
        raise HTTPException(
            status_code=500,
            detail="The recovery snapshot could not be created, so nothing was "
                   "changed. Check free disk space and try again, or continue "
                   "without a snapshot.",
        ) from exc


@app.delete("/api/projects/{slug}")
async def api_delete_project(slug: str, snapshot: bool | None = None):
    if snapshot is None:
        raise HTTPException(
            status_code=400,
            detail="Choose whether to create a recovery snapshot before deleting.",
        )
    try:
        record = projects.load_project(slug)
        if snapshot:
            _create_snapshot_or_fail(slug, record)
        projects.delete_project(slug)
    except projects.ProjectError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    activity.record(
        "project_delete",
        record.get("project_id") or record["slug"],
        "deleted_with_snapshot" if snapshot else "deleted_without_snapshot",
    )
    CONSOLE.warn(f"Deleted project '{record.get('title')}' and all its files.", slug=slug)
    return {"deleted": True}


@app.post("/api/projects/{slug}/documents")
async def api_upload(slug: str, files: list[UploadFile]):
    try:
        projects.load_project(slug)
    except projects.ProjectError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    added, rejected = [], []
    for upload in files:
        name = upload.filename or "file"
        if not media.is_supported_extension(name):
            rejected.append({
                "filename": name,
                "reason": f"'{Path(name).suffix or 'no extension'}' is not a "
                          "supported audio or video format.",
            })
            CONSOLE.warn(f"Rejected {name}: unsupported format")
            continue

        data = await upload.read()
        if not data:
            rejected.append({"filename": name, "reason": "The file was empty."})
            continue

        record = projects.add_document(slug, name, data=data)

        # Probe immediately so the file table can show duration and so an
        # unusable file is reported now rather than at transcription time.
        try:
            info = media.probe(
                projects.media_file(slug, record["id"]),
                display_name=record["filename"],
            )
            record = projects.update_document(
                slug, record["id"],
                duration=round(info.duration or 0.0, 3) or None,
                media=info.to_dict(),
                status_detail=media.describe(info),
            )
            CONSOLE.success(
                f"Added {name} ({fmt_bytes(record.get('size_bytes'))}) - "
                f"{media.describe(info)}",
                document=record["id"],
            )
        except media.MediaError as exc:
            record = projects.set_status(
                slug, record["id"], "unusable", str(exc), error=str(exc)
            )
            CONSOLE.error(f"{name} cannot be transcribed: {exc}")

        added.append(record)

    return {
        "added": added,
        "rejected": rejected,
        "documents": projects.list_documents(slug),
        "stats": projects.project_stats(slug),
    }


@app.delete("/api/projects/{slug}/documents/{doc_id}")
async def api_delete_document(slug: str, doc_id: str, snapshot: bool | None = None):
    if snapshot is None:
        raise HTTPException(
            status_code=400,
            detail="Choose whether to create a recovery snapshot before deleting.",
        )
    try:
        project = projects.load_project(slug)
        record = projects.load_document(slug, doc_id)
        if snapshot:
            _create_snapshot_or_fail(slug, project)
        projects.delete_document(slug, doc_id)
    except projects.ProjectError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    activity.record(
        "document_delete",
        project.get("project_id") or project["slug"],
        "deleted_with_snapshot" if snapshot else "deleted_without_snapshot",
    )
    CONSOLE.warn(f"Deleted {record.get('filename')} and its transcript.", document=doc_id)
    return {"deleted": True, "stats": projects.project_stats(slug)}


@app.post("/api/projects/{slug}/transcribe")
async def api_transcribe(slug: str, request: Request):
    """Queue transcription for specific documents, or for everything pending."""
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    wanted = payload.get("documents") if isinstance(payload, dict) else None
    force = bool(payload.get("force")) if isinstance(payload, dict) else False
    # Replacing an existing result is an explicit, separate consent.
    replace = bool(payload.get("replace")) if isinstance(payload, dict) else False

    # What to do: an explicit choice for this run, otherwise the project's.
    explicit_operation = None
    if isinstance(payload, dict) and payload.get("operation") not in (None, ""):
        explicit_operation = languages.parse_operation(payload.get("operation"))
        if explicit_operation is None:
            raise HTTPException(
                status_code=400,
                detail=f"'{payload.get('operation')}' is not a supported operation. "
                       "Use 'transcribe' or 'translate'.",
            )

    # The spoken language: an explicit choice for this run, then the
    # project's saved choice, then the Settings default. "auto" means
    # detect it from the audio.
    explicit = None
    if isinstance(payload, dict) and payload.get("language") not in (None, ""):
        explicit = languages.parse(payload.get("language"))
        if explicit is None:
            raise HTTPException(
                status_code=400,
                detail=f"'{payload.get('language')}' is not a supported language.",
            )

    try:
        project = projects.load_project(slug)
        documents = projects.list_documents(slug)
    except projects.ProjectError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    language = explicit or languages.coerce(
        project.get("transcription_language") or SETTINGS.get("language")
    )
    operation = explicit_operation or projects.project_operation(project)
    terms = languages.terms(operation)

    status = ENGINE.status()
    spec = catalog.model_spec(status["selected_model"])
    if not status["model_downloaded"]:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{spec.label if spec else status['selected_model']} has not "
                "been downloaded yet. Open Settings and download it first."
            ),
        )

    # Enforced here, not just in the page: an unsuitable model never enters
    # the queue, whichever client sent the request.
    problem = catalog.operation_problem(
        status["selected_model"], operation, ENGINE.hardware()
    )
    if problem:
        raise HTTPException(status_code=409, detail=problem)

    problem = languages.unsupported_reason(
        language,
        model_key=status["selected_model"],
        english_only=bool(spec and spec.languages == "English only"),
    )
    if problem:
        raise HTTPException(status_code=409, detail=problem)

    queued, skipped = [], []
    for record in documents:
        doc_id = record["id"]
        if wanted and doc_id not in wanted:
            continue
        if record["status"] == "unusable":
            skipped.append({"id": doc_id, "reason": record.get("status_detail") or "Unusable file"})
            continue
        if record["status"] in ("queued", "probing", "decoding", "transcribing"):
            skipped.append({"id": doc_id, "reason": "Already in progress"})
            continue

        existing = languages.terms(record.get("operation"))
        has_result = bool(record.get("has_transcript")) or record["status"] == "transcribed"
        if has_result and not force and not wanted:
            skipped.append({"id": doc_id, "reason": f"Already {existing['done'].lower()}"})
            continue
        if has_result and not replace:
            # Never overwrite a finished result without being told to. The
            # earlier version is kept in Edit history either way.
            skipped.append({
                "id": doc_id,
                "needs_confirmation": True,
                "reason": (
                    f"This recording already has a {existing['result']}. Confirm to "
                    f"replace it with a new {terms['result']}; the earlier one is "
                    "kept in Edit history."
                ),
            })
            continue

        job = QUEUE.submit_transcription(
            slug, doc_id, record["filename"], language=language, operation=operation,
        )
        projects.set_status(slug, doc_id, "queued", "Waiting in the queue")
        projects.update_document(slug, doc_id, pending_operation=operation)
        queued.append({"id": doc_id, "job": job.id, "filename": record["filename"]})

    if not queued:
        CONSOLE.info(
            f"Nothing to {terms['action'].lower()}: every file is done, in progress, "
            "or waiting for confirmation to be replaced."
        )
    else:
        CONSOLE.step(
            terms["queued"].format(n=f"{len(queued)} file(s)"),
            files=[q["filename"] for q in queued],
            language=language,
            operation=operation,
        )

    return {
        "queued": queued,
        "skipped": skipped,
        "operation": operation,
        "documents": projects.list_documents(slug),
        "queue": QUEUE.snapshot(limit=10),
    }


@app.get("/api/jobs")
async def api_jobs():
    return QUEUE.snapshot()


@app.post("/api/jobs/{job_id}/cancel")
async def api_cancel_job(job_id: str):
    if not QUEUE.cancel(job_id):
        raise HTTPException(status_code=404, detail="No such job, or it already finished.")
    return {"cancelled": True, "queue": QUEUE.snapshot(limit=10)}


@app.post("/api/jobs/cancel-all")
async def api_cancel_all():
    count = QUEUE.cancel_all()
    return {"cancelled": count, "queue": QUEUE.snapshot(limit=10)}


# ---------------------------------------------------------------------------
# Import / export
# ---------------------------------------------------------------------------

@app.get("/api/projects/{slug}/backup")
async def api_get_backup(slug: str):
    try:
        return {"backup": box_sync.describe(slug)}
    except projects.ProjectError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.put("/api/projects/{slug}/backup")
async def api_set_backup_destination(slug: str, request: Request):
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    destination = payload.get("destination") if isinstance(payload, dict) else None
    selection = payload.get("selection") if isinstance(payload, dict) else None
    if not isinstance(destination, str):
        raise HTTPException(status_code=400, detail="Expected a 'destination' folder path.")
    try:
        box_sync.select_destination(
            slug, destination, selection if selection in ("manual", "picker") else "manual"
        )
        state = box_sync.describe(slug)
    except box_sync.BackupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except projects.ProjectError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    activity.record("network_settings_changed", projects.load_project(slug).get("project_id") or slug)
    return {"backup": state}


@app.delete("/api/projects/{slug}/backup")
async def api_clear_backup_destination(slug: str):
    try:
        projects.clear_backup_destination(slug)
        state = box_sync.describe(slug)
    except projects.ProjectError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    activity.record("network_settings_changed", projects.load_project(slug).get("project_id") or slug)
    return {"backup": state}


@app.post("/api/projects/{slug}/backup/use-previous-folder")
async def api_use_previous_backup_folder(slug: str):
    """Explicitly reuse the folder an earlier version saved for every project."""
    previous = box_sync.legacy_destination()
    if not previous:
        raise HTTPException(status_code=409, detail="No earlier shared folder is saved.")
    try:
        box_sync.select_destination(slug, previous, "legacy_global")
        state = box_sync.describe(slug)
    except box_sync.BackupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except projects.ProjectError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"backup": state}


@app.post("/api/projects/{slug}/box-backup")
async def api_start_box_backup(slug: str, request: Request):
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object.")
    if not isinstance(payload.get("include_media", True), bool):
        raise HTTPException(status_code=400, detail="'include_media' must be a boolean.")
    try:
        job = box_sync.BACKUPS.submit(
            slug,
            confirm_sync_destination=payload.get("confirm_sync_destination") is True,
            independently_verified_box_health=(
                payload.get("independently_verified_box_health") is True
            ),
            include_media=payload.get("include_media", True),
        )
    except box_sync.BackupError as exc:
        status = 409 if "Offline Lock" in str(exc) else 400
        if "Box Health" in str(exc):
            status = 403
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    except projects.ProjectError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    CONSOLE.info("Local Box Sync backup started.", slug=slug, job=job["job_id"])
    return {"backup": job}


@app.get("/api/projects/{slug}/box-backup")
async def api_get_box_backup(slug: str, job_id: str | None = None):
    try:
        projects.load_project(slug)
    except projects.ProjectError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    job = box_sync.BACKUPS.get(slug, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No recent backup job was found.")
    return {"backup": job}


@app.post("/api/projects/{slug}/box-backup/{job_id}/cancel")
async def api_cancel_box_backup(slug: str, job_id: str):
    if not box_sync.BACKUPS.cancel(slug, job_id):
        raise HTTPException(status_code=409, detail="This backup is no longer cancellable.")
    return {"cancel_requested": True}


@app.post("/api/projects/{slug}/snapshots/open-folder")
async def api_open_snapshot_folder(slug: str):
    """Show this project's own snapshot folder in the file manager.

    The path is derived from the project, never taken from the request, so only
    that one folder can be opened. If the file manager cannot be launched the
    path is returned so it can be opened by hand.
    """
    try:
        project = projects.load_project(slug)
    except projects.ProjectError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    folder = projects._snapshot_dir(project["slug"])
    if not folder.is_dir() or not projects.list_snapshots(project["slug"]):
        raise HTTPException(
            status_code=409,
            detail="This project has no snapshots yet. Create one first.",
        )
    opened = await asyncio.to_thread(projects.reveal, folder)
    return {"opened": bool(opened), "path": str(folder)}


@app.get("/api/projects/{slug}/snapshots")
async def api_list_snapshots(slug: str):
    try:
        return {"snapshots": projects.list_snapshots(slug)}
    except projects.ProjectError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/projects/{slug}/snapshots")
async def api_create_snapshot(slug: str, request: Request):
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    include_media = payload.get("include_media", True) if isinstance(payload, dict) else True
    if not isinstance(include_media, bool):
        raise HTTPException(status_code=400, detail="'include_media' must be a boolean.")
    try:
        project = projects.load_project(slug)
        snapshot = _create_snapshot_or_fail(slug, project, include_media=include_media)
    except projects.ProjectError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    activity.record("snapshot_create", project.get("project_id") or project["slug"])
    CONSOLE.success("Created a local recovery snapshot.", slug=slug)
    return {"snapshot": snapshot, "snapshots": projects.list_snapshots(slug)}


@app.post("/api/projects/{slug}/snapshots/{snapshot_id}/restore")
async def api_restore_snapshot(slug: str, snapshot_id: str, request: Request):
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    mode = payload.get("mode", "abort") if isinstance(payload, dict) else "abort"
    try:
        restored = projects.restore_snapshot(slug, snapshot_id, mode=mode)
        if restored is None:
            activity.record("snapshot_restore", slug, "aborted")
            return {"aborted": True}
    except projects.ProjectConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except projects.ProjectValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except projects.ProjectError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    activity.record(
        "snapshot_restore",
        restored.get("project_id") or restored["slug"],
        f"restored_{mode}",
    )
    CONSOLE.success("Restored a local recovery snapshot.", slug=restored["slug"])
    return {"project": restored}


@app.post("/api/snapshots/{slug}/{snapshot_id}/restore")
async def api_restore_deleted_project_snapshot(
    slug: str, snapshot_id: str, request: Request
):
    return await api_restore_snapshot(slug, snapshot_id, request)


@app.get("/api/activity")
async def api_activity(project_id: str | None = None, limit: int = 50):
    return {"events": activity.recent(project_id=project_id, limit=limit)}


@app.get("/api/projects/{slug}/export")
async def api_export_project(slug: str, include_media: bool = True):
    try:
        archive = projects.export_project(slug, PATHS.logs, include_media=include_media)
    except projects.ProjectError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    CONSOLE.success(
        f"Exported project archive ({fmt_bytes(archive.stat().st_size)})",
        file=archive.name, include_media=include_media,
    )
    return FileResponse(
        archive, filename=archive.name, media_type="application/zip",
        background=None,
    )


@app.post("/api/projects/import")
async def api_import_project(
    file: UploadFile,
    mode: str = Form("rename"),
    snapshot_before_replace: str | None = Form(None),
):
    name = file.filename or "archive"
    staging = PATHS.logs / f"import-{os.getpid()}-{uuid4().hex}.lsproj"
    try:
        staging.parent.mkdir(parents=True, exist_ok=True)
        staging.write_bytes(await file.read())
        if mode == "replace" and snapshot_before_replace not in ("yes", "no"):
            raise HTTPException(
                status_code=400,
                detail="Choose whether to create a recovery snapshot before replacing.",
            )
        if mode == "replace":
            manifest = projects.inspect_archive(staging)
            incoming_slug = projects.slugify(
                manifest.get("slug") or manifest.get("title") or "imported"
            )
            if (
                snapshot_before_replace == "yes"
                and projects.project_dir(incoming_slug).exists()
            ):
                existing = projects.load_project(incoming_slug)
                projects.create_snapshot(incoming_slug)
                activity.record(
                    "import_replace_snapshot",
                    existing.get("project_id") or existing["slug"],
                )
        record = projects.import_project(staging, mode=mode)
    except HTTPException:
        raise
    except projects.ProjectError as exc:
        status = 409 if isinstance(exc, projects.ProjectConflictError) else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    finally:
        staging.unlink(missing_ok=True)

    if mode == "replace":
        activity.record(
            "import_replace",
            record.get("project_id") or record["slug"],
            f"replaced_{snapshot_before_replace}",
        )
    CONSOLE.success(
        f"Imported project '{record['title']}' with "
        f"{record['stats']['documents']} document(s)",
        slug=record["slug"], source=name,
    )
    return {"project": record}


# ---------------------------------------------------------------------------
# Transcript editing
# ---------------------------------------------------------------------------

def _export_outputs(slug: str, doc_id: str, doc: dict, record: dict, project: dict) -> dict:
    settings = SETTINGS.all()
    generated = projects._now()
    formats = [
        fmt for fmt in (settings.get("formats") or exporters.ALL_FORMATS)
        if fmt in exporters.WRITERS
    ]
    meta = {
        "title": Path(record.get("filename") or "transcript").stem,
        "filename": record.get("filename"),
        "project": project.get("title"),
        "principal_investigator": project.get("principal_investigator"),
        "irb_protocol": project.get("irb_protocol"),
        "generated": generated,
        "export_provenance": validation.provenance(doc, generated, formats),
    }
    out_dir = projects.outputs_dir(slug, doc_id)
    written = exporters.export_all(doc, out_dir, settings, meta)
    successful = [
        fmt for fmt, result in written.items()
        if not str(result).startswith("error")
    ]
    if successful:
        validation.record_export(doc, generated, successful)
        projects.save_transcript(slug, doc_id, doc, revision=False)
    if "json" in successful:
        meta["export_provenance"] = validation.provenance(
            doc, generated, successful,
        )
        try:
            exporters.write_json(
                doc, out_dir / f"{exporters.basename_for(doc)}.json", settings, meta
            )
        except Exception as exc:
            written["json"] = f"error: {exc}"
    return written


def _load_doc_or_404(slug: str, doc_id: str) -> dict:
    doc = projects.load_transcript(slug, doc_id)
    if doc is None:
        raise HTTPException(
            status_code=404,
            detail="This document has not been transcribed yet.",
        )
    return doc


@app.get("/api/projects/{slug}/documents/{doc_id}/transcript")
async def api_get_transcript(slug: str, doc_id: str):
    doc = _load_doc_or_404(slug, doc_id)
    return {
        "transcript": doc,
        "summary": summary(doc),
        "outputs": projects.list_outputs(slug, doc_id),
    }


@app.put("/api/projects/{slug}/documents/{doc_id}/transcript")
async def api_save_transcript(slug: str, doc_id: str, request: Request):
    """Autosave endpoint: applies segment edits and persists."""
    payload = await request.json()
    edits = payload.get("edits") or []
    doc = _load_doc_or_404(slug, doc_id)
    _require_reopened(doc)

    apply_edits(doc, edits)
    keep = int(SETTINGS.get("autosave_revisions") or 20)
    projects.save_transcript(slug, doc_id, doc, revision=bool(payload.get("revision", True)), keep=keep)

    stale = sum(1 for s in doc["segments"] if s.get("stale_timings"))
    if edits:
        CONSOLE.debug(
            f"Autosaved {len(edits)} segment edit(s)",
            document=doc_id, stale_timings=stale,
        )
    return {
        "saved": True,
        "summary": summary(doc),
        "stale_timings": stale,
        "updated": doc["updated"],
    }


@app.put("/api/projects/{slug}/documents/{doc_id}/speakers")
async def api_save_speakers(slug: str, doc_id: str, request: Request):
    """Replace the speaker roster for one document."""
    payload = await request.json()
    roster = payload.get("speakers")
    if not isinstance(roster, list):
        raise HTTPException(status_code=400, detail="Expected a 'speakers' list.")

    doc = _load_doc_or_404(slug, doc_id)
    _require_reopened(doc)
    report = set_roster(doc, roster)
    projects.save_transcript(
        slug, doc_id, doc, revision=False,
        keep=int(SETTINGS.get("autosave_revisions") or 20),
    )

    names = ", ".join(s["name"] for s in report["speakers"]) or "none"
    CONSOLE.info(f"Speakers for {doc_id}: {names}", document=doc_id)
    if report["unassigned_segments"]:
        CONSOLE.warn(
            f"{report['unassigned_segments']} segment(s) lost their speaker "
            f"because {', '.join(report['removed_speakers'])} was removed.",
            document=doc_id,
        )
    return {
        "speakers": report["speakers"],
        "unassigned_segments": report["unassigned_segments"],
        "removed_speakers": report["removed_speakers"],
        "transcript": doc,
        "summary": summary(doc),
    }


@app.post("/api/projects/{slug}/documents/{doc_id}/assign")
async def api_assign_speaker(slug: str, doc_id: str, request: Request):
    """Tag a run of segments with a speaker. This is the one-click turn tag."""
    payload = await request.json()
    segment_ids = payload.get("segments") or []
    speaker_id = str(payload.get("speaker_id") or "")

    doc = _load_doc_or_404(slug, doc_id)
    _require_reopened(doc)
    try:
        changed = assign_speaker(doc, segment_ids, speaker_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if changed:
        projects.save_transcript(
            slug, doc_id, doc, revision=False,
            keep=int(SETTINGS.get("autosave_revisions") or 20),
        )
    return {
        "changed": changed,
        "segments": [
            {"id": s["id"], "speaker_id": s.get("speaker_id", ""),
             "speaker": s.get("speaker", "")}
            for s in doc["segments"]
        ],
        "summary": summary(doc),
    }


@app.post("/api/projects/{slug}/documents/{doc_id}/review")
async def api_review_action(slug: str, doc_id: str, request: Request):
    """Persist a queue resolution or a manual segment flag in the transcript."""
    payload = await request.json()
    doc = _load_doc_or_404(slug, doc_id)
    _require_reopened(doc)
    try:
        state = review.apply_action(
            doc,
            payload.get("segment_id"),
            str(payload.get("category") or ""),
            str(payload.get("action") or ""),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    projects.save_transcript(
        slug, doc_id, doc, revision=False,
        keep=int(SETTINGS.get("autosave_revisions") or 20),
    )
    return {"review": state, "updated": doc.get("updated")}


@app.get("/api/projects/{slug}/documents/{doc_id}/deidentification")
async def api_deidentification_report(slug: str, doc_id: str):
    _load_doc_or_404(slug, doc_id)
    return deidentification.get_report(slug, doc_id)


@app.post("/api/projects/{slug}/documents/{doc_id}/deidentification/scan")
async def api_deidentification_scan(slug: str, doc_id: str, request: Request):
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Expected scan options.")
    doc = _load_doc_or_404(slug, doc_id)
    try:
        return deidentification.scan(
            slug, doc_id, doc,
            flags=payload.get("flags"),
            custom_patterns=payload.get("custom_patterns"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/projects/{slug}/documents/{doc_id}/deidentification/{action}")
async def api_deidentification_action(
    slug: str, doc_id: str, action: str, request: Request,
):
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Expected a review item.")
    doc = _load_doc_or_404(slug, doc_id)
    _require_reopened(doc)
    item_id = str(payload.get("item_id") or "")
    try:
        if action == "skip":
            return deidentification.skip(slug, doc_id, item_id)
        if action == "replace":
            doc, report = deidentification.replace(slug, doc_id, doc, item_id)
        elif action == "undo":
            doc, report = deidentification.undo(slug, doc_id, doc, item_id)
        else:
            raise HTTPException(status_code=404, detail="Unknown de-identification action.")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    projects.save_transcript(
        slug, doc_id, doc, revision=True,
        keep=int(SETTINGS.get("autosave_revisions") or 20),
    )
    return {"transcript": doc, **report}


@app.post("/api/projects/{slug}/documents/{doc_id}/annotations")
async def api_add_annotation(slug: str, doc_id: str, request: Request):
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Expected an annotation object.")
    doc = _load_doc_or_404(slug, doc_id)
    _require_reopened(doc)
    try:
        annotations = handoff.clean_annotations([{
            "id": uuid4().hex,
            "kind": payload.get("kind"),
            "text": payload.get("text"),
            "at": payload.get("at"),
            "segment_id": payload.get("segment_id"),
            "author": payload.get("author"),
            "created_at": projects._now(),
        }], doc.get("duration"))
    except handoff.HandoffError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    annotation = annotations[0]
    doc["annotations"] = handoff.merge_annotations(
        doc.get("annotations") or [], [annotation], duration=doc.get("duration")
    )
    projects.save_transcript(
        slug, doc_id, doc, revision=False,
        keep=int(SETTINGS.get("autosave_revisions") or 20),
    )
    return {"annotation": annotation, "annotations": doc["annotations"]}


@app.delete("/api/projects/{slug}/documents/{doc_id}/annotations/{annotation_id}")
async def api_delete_annotation(slug: str, doc_id: str, annotation_id: str):
    doc = _load_doc_or_404(slug, doc_id)
    _require_reopened(doc)
    annotations = doc.get("annotations") or []
    kept = [item for item in annotations if str(item.get("id")) != annotation_id]
    if len(kept) == len(annotations):
        raise HTTPException(status_code=404, detail="That annotation was not found.")
    doc["annotations"] = kept
    projects.save_transcript(
        slug, doc_id, doc, revision=False,
        keep=int(SETTINGS.get("autosave_revisions") or 20),
    )
    return {"annotations": kept}


@app.post("/api/projects/{slug}/documents/{doc_id}/handoff/export")
async def api_export_handoff(slug: str, doc_id: str, request: Request):
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Expected handoff options.")
    if not isinstance(payload.get("include_media", False), bool):
        raise HTTPException(status_code=400, detail="'include_media' must be a boolean.")
    try:
        package = handoff.create_package(
            slug, doc_id, PATHS.logs,
            include_media=payload.get("include_media", False),
            instructions=payload.get("instructions", ""),
        )
    except handoff.HandoffError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except projects.ProjectError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(package, filename=package.name, media_type="application/zip")


@app.post("/api/projects/{slug}/documents/{doc_id}/handoff/import")
async def api_import_handoff(slug: str, doc_id: str, file: UploadFile):
    _require_reopened(_load_doc_or_404(slug, doc_id))
    staging = PATHS.logs / f"handoff-{os.getpid()}-{uuid4().hex}{handoff.PACKAGE_SUFFIX}"
    try:
        staging.parent.mkdir(parents=True, exist_ok=True)
        size = 0
        with staging.open("xb") as output:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > handoff.MAX_PACKAGE_BYTES:
                    raise HTTPException(status_code=413, detail="Review package is larger than 4 GiB.")
                output.write(chunk)
        result = handoff.import_package(staging, slug, doc_id)
    except HTTPException:
        raise
    except handoff.HandoffError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except projects.ProjectError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        staging.unlink(missing_ok=True)
    return {
        **result,
        "conflicts": handoff.list_conflicts(slug, doc_id),
        "annotations": (projects.load_transcript(slug, doc_id) or {}).get("annotations", []),
    }


@app.get("/api/projects/{slug}/documents/{doc_id}/handoff/conflicts")
async def api_handoff_conflicts(slug: str, doc_id: str):
    _load_doc_or_404(slug, doc_id)
    return {"conflicts": handoff.list_conflicts(slug, doc_id)}


@app.get("/api/projects/{slug}/documents/{doc_id}/handoff/conflicts/{conflict_id}/{version}")
async def api_handoff_conflict_version(slug: str, doc_id: str, conflict_id: str, version: str):
    try:
        path = handoff.conflict_file(slug, doc_id, conflict_id, version)
    except projects.ProjectError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(path, filename=f"{version}-transcript.json", media_type="application/json")


@app.post("/api/projects/{slug}/documents/{doc_id}/handoff/conflicts/{conflict_id}/resolve")
async def api_resolve_handoff_conflict(slug: str, doc_id: str, conflict_id: str, request: Request):
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Expected a conflict resolution choice.")
    choice = str(payload.get("choice") or "")
    if choice == "incoming":
        _require_reopened(_load_doc_or_404(slug, doc_id))
    try:
        record = handoff.resolve_conflict(
            slug, doc_id, conflict_id, choice
        )
    except projects.ProjectError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"conflict": record}


def _require_reopened(doc: dict) -> None:
    try:
        validation.require_reopened(doc)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/projects/{slug}/documents/{doc_id}/validation")
async def api_validation_action(slug: str, doc_id: str, request: Request):
    payload = await request.json()
    doc = _load_doc_or_404(slug, doc_id)
    try:
        state = validation.transition(
            doc,
            str(payload.get("action") or ""),
            str(payload.get("reviewer") or ""),
            str(payload.get("notes") or ""),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    projects.save_transcript(
        slug, doc_id, doc, revision=False,
        keep=int(SETTINGS.get("autosave_revisions") or 20),
    )
    return {"validation": state, "updated": doc.get("updated")}


@app.post("/api/projects/{slug}/documents/{doc_id}/proofread")
async def api_proofread(slug: str, doc_id: str, request: Request):
    """Check segments for misspellings and transcription artefacts.

    The project glossary and the document's accepted words are folded in, so
    a study's own vocabulary stops being flagged the moment it is recorded on
    the project page.
    """
    payload = await request.json()
    segments = payload.get("segments")
    if not isinstance(segments, list):
        raise HTTPException(status_code=400, detail="Expected a 'segments' list.")

    settings = SETTINGS.all()
    if not settings.get("proofread_spelling") and not settings.get("proofread_artefacts"):
        return {"issues": {}, "enabled": False}

    try:
        project = projects.load_project(slug)
    except projects.ProjectError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    doc = projects.load_transcript(slug, doc_id) or {}
    if not proofread.supports_document(doc):
        code = languages.output_language(doc)
        return {
            "issues": {},
            "enabled": False,
            "reason": "language",
            "language": code,
            "language_name": languages.name(code),
        }
    profile = vocabulary.get_profile(project.get("vocabulary_profile_id"))
    extra = proofread.extra_words(
        project.get("glossary"),
        (profile or {}).get("terms"),
        settings.get("initial_prompt"),
        settings.get("hotwords"),
        doc.get("dictionary"),
        [s.get("name") for s in doc.get("speakers") or []],
        [s.get("short") for s in doc.get("speakers") or []],
        project.get("participant_scheme"),
    )

    issues = proofread.check_segments(
        segments,
        extra=extra,
        spelling=bool(settings.get("proofread_spelling", True)),
        artefacts=bool(settings.get("proofread_artefacts", True)),
        suggestions=int(settings.get("proofread_suggestions") or 0),
    )
    return {
        "issues": issues,
        "enabled": True,
        "dictionary": proofread.status(),
    }


@app.post("/api/projects/{slug}/documents/{doc_id}/dictionary")
async def api_add_to_dictionary(slug: str, doc_id: str, request: Request):
    """Accept a word so it stops being flagged.

    Scope 'document' keeps it to this transcript; 'project' writes it into the
    project glossary, where it also improves Whisper's own spelling of the term
    on any file transcribed afterwards.
    """
    payload = await request.json()
    word = str(payload.get("word") or "").strip()
    scope = str(payload.get("scope") or "document")
    if not word:
        raise HTTPException(status_code=400, detail="No word was supplied.")

    if scope == "project":
        project = projects.load_project(slug)
        glossary = (project.get("glossary") or "").strip()
        terms = [t.strip() for t in glossary.split(",") if t.strip()]
        if word not in terms:
            terms.append(word)
        projects.save_project(slug, {"glossary": ", ".join(terms)})
        CONSOLE.info(f"Added '{word}' to the project glossary.", project=slug)
    else:
        doc = _load_doc_or_404(slug, doc_id)
        _require_reopened(doc)
        accepted = doc.setdefault("dictionary", [])
        if word not in accepted:
            accepted.append(word)
            projects.save_transcript(
                slug, doc_id, doc, revision=False,
                keep=int(SETTINGS.get("autosave_revisions") or 20),
            )
        CONSOLE.info(f"Accepted '{word}' for this document.", document=doc_id)

    # The cached results were computed without this word, so drop them.
    proofread.CACHE.clear()
    return {"word": word, "scope": scope}


@app.post("/api/projects/{slug}/documents/{doc_id}/retimestamp")
async def api_retimestamp(slug: str, doc_id: str):
    doc = _load_doc_or_404(slug, doc_id)
    _require_reopened(doc)
    record = projects.load_document(slug, doc_id)
    project = projects.load_project(slug)

    CONSOLE.step(f"Re-timestamping {record.get('filename')}", document=doc_id)
    report = retimestamp(doc)
    CONSOLE.success(describe_report(report), **{
        k: v for k, v in report.items() if k != "review_segments"
    })
    if report["review_segments"]:
        CONSOLE.info(
            "Segments with large rewrites are worth spot-checking against the "
            f"audio: {', '.join('#' + str(i + 1) for i in report['review_segments'])}",
            segments=report["review_segments"],
        )

    keep = int(SETTINGS.get("autosave_revisions") or 20)
    projects.save_transcript(slug, doc_id, doc, revision=True, keep=keep)

    written = _export_outputs(slug, doc_id, doc, record, project)
    ok = [f for f, v in written.items() if not str(v).startswith("error")]
    CONSOLE.success(
        f"Regenerated {len(ok)} output file(s) including captions: "
        f"{', '.join('.' + f for f in ok)}"
    )

    return {
        "report": report,
        "message": describe_report(report),
        "transcript": doc,
        "summary": summary(doc),
        "outputs": projects.list_outputs(slug, doc_id),
        "workflow": validation.snapshot(doc),
    }


@app.post("/api/projects/{slug}/documents/{doc_id}/segment/{seg_id}/split")
async def api_split(slug: str, doc_id: str, seg_id: int, request: Request):
    payload = await request.json()
    word_index = int(payload.get("word_index") or 0)
    doc = _load_doc_or_404(slug, doc_id)
    _require_reopened(doc)
    split_segment(doc, seg_id, word_index)
    projects.save_transcript(slug, doc_id, doc, keep=int(SETTINGS.get("autosave_revisions") or 20))
    CONSOLE.info(f"Split segment #{seg_id + 1}", document=doc_id)
    return {"transcript": doc, "summary": summary(doc)}


@app.post("/api/projects/{slug}/documents/{doc_id}/segment/{seg_id}/merge")
async def api_merge(slug: str, doc_id: str, seg_id: int):
    doc = _load_doc_or_404(slug, doc_id)
    _require_reopened(doc)
    merge_segment(doc, seg_id)
    projects.save_transcript(slug, doc_id, doc, keep=int(SETTINGS.get("autosave_revisions") or 20))
    CONSOLE.info(f"Merged segment #{seg_id + 1} with the next one", document=doc_id)
    return {"transcript": doc, "summary": summary(doc)}


@app.post("/api/projects/{slug}/documents/{doc_id}/export")
async def api_reexport(slug: str, doc_id: str):
    doc = _load_doc_or_404(slug, doc_id)
    record = projects.load_document(slug, doc_id)
    project = projects.load_project(slug)
    written = _export_outputs(slug, doc_id, doc, record, project)
    ok = [f for f, v in written.items() if not str(v).startswith("error")]
    CONSOLE.success(f"Re-exported {len(ok)} file(s): {', '.join('.' + f for f in ok)}")
    return {
        "written": written,
        "outputs": projects.list_outputs(slug, doc_id),
        "workflow": validation.snapshot(doc),
    }


@app.get("/api/projects/{slug}/documents/{doc_id}/revisions")
async def api_revisions(slug: str, doc_id: str):
    return {"revisions": projects.list_revisions(slug, doc_id)}


@app.post("/api/projects/{slug}/documents/{doc_id}/revisions/{revision_id}/restore")
async def api_restore_revision(slug: str, doc_id: str, revision_id: str):
    current = _load_doc_or_404(slug, doc_id)
    _require_reopened(current)
    previous = projects.load_revision(slug, doc_id, revision_id)
    if previous is None:
        raise HTTPException(status_code=404, detail="That revision no longer exists.")
    previous["validation"] = current.get("validation")
    previous["export"] = current.get("export")
    normalise(previous)
    validation.record_restore(previous, revision_id)
    # Keep what is being replaced, so restoring an earlier result (say the
    # original-language transcript after a translation) can be undone too.
    projects.archive_transcript(slug, doc_id)
    projects.save_transcript(slug, doc_id, previous, keep=int(SETTINGS.get("autosave_revisions") or 20))
    record = projects.load_document(slug, doc_id)
    project = projects.load_project(slug)
    exporters.export_all(
        previous, projects.outputs_dir(slug, doc_id), SETTINGS.all(),
        {
            "title": Path(record.get("filename") or "transcript").stem,
            "filename": record.get("filename"),
            "project": project.get("title"),
            "principal_investigator": project.get("principal_investigator"),
            "irb_protocol": project.get("irb_protocol"),
            "generated": projects._now(),
        },
    )
    CONSOLE.warn(f"Restored transcript revision {revision_id}", document=doc_id)
    return {"transcript": previous, "summary": summary(previous)}


# ---------------------------------------------------------------------------
# Media and file serving
# ---------------------------------------------------------------------------

@app.get("/media/{slug}/{doc_id}")
async def serve_media(slug: str, doc_id: str, request: Request):
    """Serve audio for the editor, preferring the browser-playable preview."""
    try:
        record = projects.load_document(slug, doc_id)
    except projects.ProjectError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    preview = projects.document_dir(slug, doc_id) / "preview.m4a"
    if record.get("preview") and preview.exists():
        path, media_type = preview, "audio/mp4"
    else:
        try:
            path = projects.media_file(slug, doc_id)
        except projects.ProjectError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        media_type = _guess_media_type(path)

    return _ranged_file(path, media_type, request)


def _guess_media_type(path: Path) -> str:
    return {
        ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac",
        ".wav": "audio/wav", ".flac": "audio/flac", ".ogg": "audio/ogg",
        ".oga": "audio/ogg", ".opus": "audio/ogg", ".mp4": "video/mp4",
        ".m4v": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
    }.get(path.suffix.lower(), "application/octet-stream")


def _ranged_file(path: Path, media_type: str, request: Request) -> Response:
    """Serve a file with Range support, which audio scrubbing depends on.

    Without byte-range replies the browser cannot seek in a long recording, so
    clicking a word near the end of a two-hour interview would download the
    whole file first.
    """
    return ranged.ranged_file(path, media_type, request.headers.get("range"))


@app.get("/api/projects/{slug}/documents/{doc_id}/waveform")
async def api_waveform(slug: str, doc_id: str):
    path = projects.document_dir(slug, doc_id) / "waveform.json"
    if not path.exists():
        return {"duration": 0, "buckets": 0, "peaks": []}
    return JSONResponse(projects.read_json(path, {"peaks": []}))


@app.get("/outputs/{slug}/{doc_id}/{name}")
async def serve_output(slug: str, doc_id: str, name: str):
    safe = projects.safe_filename(name)
    path = projects.outputs_dir(slug, doc_id) / safe
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"{safe} has not been generated yet.")
    return FileResponse(
        path, filename=safe, media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{quote(safe)}"'},
    )


@app.get("/api/projects/{slug}/documents/{doc_id}/download-all")
async def api_download_all(slug: str, doc_id: str):
    """Zip every output for one document, built in memory."""
    record = projects.load_document(slug, doc_id)
    out_dir = projects.outputs_dir(slug, doc_id)
    files = [p for p in out_dir.iterdir() if p.is_file()] if out_dir.is_dir() else []
    if not files:
        raise HTTPException(status_code=404, detail="No output files have been generated yet.")

    stem = Path(record.get("filename") or "transcript").stem
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            projects.write_zip_file(archive, path, f"{stem}/{path.name}")
    buffer.seek(0)

    name = f"{projects.slugify(stem, 'transcript')}-transcripts.zip"
    CONSOLE.info(f"Packaged {len(files)} output file(s) for download", document=doc_id)
    return StreamingResponse(
        buffer, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@app.post("/api/projects/{slug}/documents/{doc_id}/reveal")
async def api_reveal_outputs(slug: str, doc_id: str):
    """Open the outputs folder in the platform file manager.

    A browser cannot write to an arbitrary desktop path, so revealing the
    folder is the reliable way to get files wherever the researcher wants them.
    """
    out_dir = projects.outputs_dir(slug, doc_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    opened = projects.reveal(out_dir)
    CONSOLE.info(
        f"Opened the outputs folder: {out_dir}" if opened
        else f"Could not open a file manager. The folder is at {out_dir}"
    )
    return {"opened": opened, "path": str(out_dir)}


@app.get("/favicon.ico")
async def favicon():
    path = PATHS.static / "icon" / "favicon.ico"
    if path.exists():
        return FileResponse(path, media_type="image/x-icon")
    raise HTTPException(status_code=404)
