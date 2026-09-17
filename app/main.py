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
import io
import json
import os
import zipfile
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import catalog, exporters, media, proofread, projects
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
    return response


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

    status = ENGINE.status()
    for warning in status["warnings"]:
        CONSOLE.warn(warning)
    for note in status["notes"]:
        CONSOLE.info(note)

    if not status["model_downloaded"]:
        spec = catalog.model_spec(status["selected_model"])
        CONSOLE.warn(
            f"No Whisper model is downloaded yet. Open AI Settings and "
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
        base_context(request, projects=projects.list_projects()),
    )


@app.get("/project/{slug}", response_class=HTMLResponse)
async def page_project(request: Request, slug: str):
    try:
        project = projects.load_project(slug)
    except projects.ProjectError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return templates.TemplateResponse(
        request,
        "project.html",
        base_context(
            request,
            project=project,
            documents=projects.list_documents(slug),
            stats=projects.project_stats(slug),
            accepted=sorted(media.MEDIA_EXTENSIONS),
        ),
    )


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
            outputs=projects.list_outputs(slug, doc_id),
            revisions=projects.list_revisions(slug, doc_id),
            speakers=speaker_roster(doc) if doc else [],
            palette=list(PALETTE),
            pause_gap=float(SETTINGS.get("paragraph_gap") or 1.5),
            proofreading={
                "spelling": bool(SETTINGS.get("proofread_spelling", True)),
                "artefacts": bool(SETTINGS.get("proofread_artefacts", True)),
                "available": proofread.DICTIONARY.available,
            },
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
            advanced_raw=json.dumps(SETTINGS.get("advanced_raw") or {}, indent=2),
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
            **{k: v for k, v in changed.items() if not isinstance(v, (dict, list))},
        )
    if "offline_lock" in changed:
        CONSOLE.warn(
            "Offline lock engaged: outbound network access is now blocked."
            if changed["offline_lock"] else
            "Offline lock released: model downloads are possible again."
        )

    reload_keys = {"model", "device", "compute_type", "cpu_threads", "num_workers"}
    if reload_keys & set(changed):
        ENGINE.unload()
        CONSOLE.info("Engine settings changed; the model will reload on next use.")

    return {"values": values, "engine": ENGINE.status(), "changed": sorted(changed)}


@app.post("/api/settings/reset")
async def api_reset_settings():
    values = SETTINGS.reset()
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
        return {"already": True, "job": None}
    if SETTINGS.get("offline_lock"):
        raise HTTPException(
            status_code=409,
            detail="The offline lock is engaged. Turn it off in AI Settings to "
                   "download a model, then turn it back on.",
        )
    job = QUEUE.submit_download(model_key)
    return {"already": False, "job": job.to_dict()}


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

    CONSOLE.step("Installing GPU support libraries (cuBLAS 12, cuDNN 9) via pip")
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-r", str(req)],
        capture_output=True, text=True,
    )
    tail = (proc.stdout or "").strip().splitlines()[-6:]
    for line in tail:
        CONSOLE.debug(f"  pip: {line}")

    if proc.returncode != 0:
        CONSOLE.error("GPU support installation failed.", returncode=proc.returncode)
        CONSOLE.debug("pip stderr", stderr=(proc.stderr or "")[-2000:])
        raise HTTPException(
            status_code=500,
            detail="pip could not install the GPU libraries. See the console "
                   "for details.",
        )

    hardware = ENGINE.hardware(refresh=True)
    ENGINE.unload()
    if hardware.get("cuda_usable"):
        CONSOLE.success("GPU support installed and CUDA is now usable.")
    else:
        CONSOLE.warn(
            "GPU libraries installed, but CUDA is still unavailable. "
            + " ".join(hardware.get("warnings") or [])
        )
    return {"engine": ENGINE.status(), "hardware": hardware}


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
    # `title` is passed positionally, so it must not also arrive as a keyword.
    record = projects.create_project(title, **{
        k: payload.get(k)
        for k in projects.EDITABLE_FIELDS
        if k in payload and k != "title"
    })
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
    try:
        record = projects.save_project(slug, payload)
    except projects.ProjectError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    CONSOLE.info(f"Saved metadata for '{record['title']}'", slug=slug)
    return {"project": record}


@app.delete("/api/projects/{slug}")
async def api_delete_project(slug: str):
    try:
        record = projects.load_project(slug)
        projects.delete_project(slug)
    except projects.ProjectError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
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
                status_detail=f"{media.describe(info)} - ready to transcribe",
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
async def api_delete_document(slug: str, doc_id: str):
    try:
        record = projects.load_document(slug, doc_id)
        projects.delete_document(slug, doc_id)
    except projects.ProjectError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
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

    try:
        projects.load_project(slug)
        documents = projects.list_documents(slug)
    except projects.ProjectError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    status = ENGINE.status()
    if not status["model_downloaded"]:
        spec = catalog.model_spec(status["selected_model"])
        raise HTTPException(
            status_code=409,
            detail=(
                f"{spec.label if spec else status['selected_model']} has not "
                "been downloaded yet. Open AI Settings and download it first."
            ),
        )

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
        if record["status"] == "transcribed" and not force and not wanted:
            skipped.append({"id": doc_id, "reason": "Already transcribed"})
            continue

        job = QUEUE.submit_transcription(slug, doc_id, record["filename"])
        projects.set_status(slug, doc_id, "queued", "Waiting in the queue")
        queued.append({"id": doc_id, "job": job.id, "filename": record["filename"]})

    if not queued:
        CONSOLE.info("Nothing to transcribe: every file is done or in progress.")
    else:
        CONSOLE.step(
            f"Queued {len(queued)} file(s) for transcription",
            files=[q["filename"] for q in queued],
        )

    return {
        "queued": queued,
        "skipped": skipped,
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
async def api_import_project(file: UploadFile, mode: str = Form("rename")):
    name = file.filename or "archive"
    staging = PATHS.logs / f"import-{os.getpid()}-{name}"
    try:
        staging.write_bytes(await file.read())
        record = projects.import_project(staging, mode=mode)
    except projects.ProjectError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        staging.unlink(missing_ok=True)

    CONSOLE.success(
        f"Imported project '{record['title']}' with "
        f"{record['stats']['documents']} document(s)",
        slug=record["slug"], source=name,
    )
    return {"project": record}


# ---------------------------------------------------------------------------
# Transcript editing
# ---------------------------------------------------------------------------

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
    extra = proofread.extra_words(
        project.get("glossary"),
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

    written = exporters.export_all(
        doc, projects.outputs_dir(slug, doc_id), SETTINGS.all(),
        {
            "title": Path(record.get("filename") or "transcript").stem,
            "filename": record.get("filename"),
            "project": project.get("title"),
            "principal_investigator": project.get("principal_investigator"),
            "irb_protocol": project.get("irb_protocol"),
            "generated": projects._now(),
        },
    )
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
    }


@app.post("/api/projects/{slug}/documents/{doc_id}/segment/{seg_id}/split")
async def api_split(slug: str, doc_id: str, seg_id: int, request: Request):
    payload = await request.json()
    word_index = int(payload.get("word_index") or 0)
    doc = _load_doc_or_404(slug, doc_id)
    split_segment(doc, seg_id, word_index)
    projects.save_transcript(slug, doc_id, doc, keep=int(SETTINGS.get("autosave_revisions") or 20))
    CONSOLE.info(f"Split segment #{seg_id + 1}", document=doc_id)
    return {"transcript": doc, "summary": summary(doc)}


@app.post("/api/projects/{slug}/documents/{doc_id}/segment/{seg_id}/merge")
async def api_merge(slug: str, doc_id: str, seg_id: int):
    doc = _load_doc_or_404(slug, doc_id)
    merge_segment(doc, seg_id)
    projects.save_transcript(slug, doc_id, doc, keep=int(SETTINGS.get("autosave_revisions") or 20))
    CONSOLE.info(f"Merged segment #{seg_id + 1} with the next one", document=doc_id)
    return {"transcript": doc, "summary": summary(doc)}


@app.post("/api/projects/{slug}/documents/{doc_id}/export")
async def api_reexport(slug: str, doc_id: str):
    doc = _load_doc_or_404(slug, doc_id)
    record = projects.load_document(slug, doc_id)
    project = projects.load_project(slug)
    written = exporters.export_all(
        doc, projects.outputs_dir(slug, doc_id), SETTINGS.all(),
        {
            "title": Path(record.get("filename") or "transcript").stem,
            "filename": record.get("filename"),
            "project": project.get("title"),
            "principal_investigator": project.get("principal_investigator"),
            "irb_protocol": project.get("irb_protocol"),
            "generated": projects._now(),
        },
    )
    ok = [f for f, v in written.items() if not str(v).startswith("error")]
    CONSOLE.success(f"Re-exported {len(ok)} file(s): {', '.join('.' + f for f in ok)}")
    return {"written": written, "outputs": projects.list_outputs(slug, doc_id)}


@app.get("/api/projects/{slug}/documents/{doc_id}/revisions")
async def api_revisions(slug: str, doc_id: str):
    return {"revisions": projects.list_revisions(slug, doc_id)}


@app.post("/api/projects/{slug}/documents/{doc_id}/revisions/{revision_id}/restore")
async def api_restore_revision(slug: str, doc_id: str, revision_id: str):
    previous = projects.load_revision(slug, doc_id, revision_id)
    if previous is None:
        raise HTTPException(status_code=404, detail="That revision no longer exists.")
    normalise(previous)
    projects.save_transcript(slug, doc_id, previous, keep=int(SETTINGS.get("autosave_revisions") or 20))
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
    size = path.stat().st_size
    range_header = request.headers.get("range")

    if not range_header or not range_header.startswith("bytes="):
        return FileResponse(path, media_type=media_type, headers={
            "Accept-Ranges": "bytes",
            "Cache-Control": "no-store",
        })

    spec = range_header.split("=", 1)[1].split(",")[0].strip()
    start_text, _, end_text = spec.partition("-")
    try:
        start = int(start_text) if start_text else 0
        end = int(end_text) if end_text else size - 1
    except ValueError:
        start, end = 0, size - 1

    start = max(0, min(start, size - 1))
    end = max(start, min(end, size - 1))
    length = end - start + 1

    def iterator():
        with open(path, "rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(
        iterator(), status_code=206, media_type=media_type,
        headers={
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(length),
            "Cache-Control": "no-store",
        },
    )


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
            archive.write(path, f"{stem}/{path.name}")
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
