"""The work queue: transcription jobs and model downloads.

One worker thread processes one job at a time. That serialisation is
deliberate, not a simplification: two Whisper models decoding at once compete
for the same VRAM or CPU cores, so the total finish time is no better and every
throughput estimate becomes meaningless. A researcher watching a console would
rather see one file finish than three crawl.

Every stage narrates itself to the console with timings, and each segment
updates the progress and ETA as it decodes.
"""

from __future__ import annotations

import queue
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from . import catalog, exporters, media, projects
from .config import SETTINGS
from .console import CONSOLE, fmt_bytes, fmt_duration, fmt_timestamp
from .engine import ENGINE, Cancelled
from .transcript import from_engine_segment, new_document, normalise, summary

# How often a long decode reports progress to the console, in segments.
CONSOLE_SEGMENT_STRIDE = 5


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class Job:
    """A unit of queued work, and the public view of its progress."""

    _counter = 0
    _counter_lock = threading.Lock()

    def __init__(self, kind: str, label: str, **payload):
        with Job._counter_lock:
            Job._counter += 1
            self.id = f"job-{Job._counter:04d}"
        self.kind = kind                # transcribe | download
        self.label = label
        self.payload = payload
        self.status = "queued"          # queued|running|done|failed|cancelled
        self.detail = "Waiting in the queue"
        self.progress = 0.0
        self.stats: dict = {}
        self.error: str | None = None
        self.queued_at = _now()
        self.started_at: str | None = None
        self.finished_at: str | None = None
        self._started_monotonic: float | None = None
        self.cancel_requested = False

    @property
    def elapsed(self) -> float:
        if self._started_monotonic is None:
            return 0.0
        return time.monotonic() - self._started_monotonic

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "status": self.status,
            "detail": self.detail,
            "progress": round(self.progress, 4),
            "stats": self.stats,
            "error": self.error,
            "queued_at": self.queued_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed": round(self.elapsed, 1),
            "project": self.payload.get("slug"),
            "document": self.payload.get("doc_id"),
            "cancel_requested": self.cancel_requested,
        }


class JobQueue:
    def __init__(self) -> None:
        self._queue: queue.Queue = queue.Queue()
        self._lock = threading.RLock()
        self._jobs: dict = {}
        self._order: list = []
        self._current: Job | None = None
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._stop.clear()
        self._worker = threading.Thread(
            target=self._run, name="localscribe-worker", daemon=True
        )
        self._worker.start()
        CONSOLE.debug("Worker thread started")

    def stop(self) -> None:
        self._stop.set()
        self._queue.put(None)

    # -- submission --------------------------------------------------------

    def submit(self, job: Job) -> Job:
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            # Keep the history bounded; finished jobs are only needed briefly.
            if len(self._order) > 200:
                stale = self._order[:-200]
                self._order = self._order[-200:]
                for job_id in stale:
                    self._jobs.pop(job_id, None)
        self._queue.put(job.id)
        position = self.pending_count()
        job.detail = (
            "Waiting in the queue" if position > 1 else "Starting shortly"
        )
        CONSOLE.info(
            f"Queued: {job.label}",
            job=job.id, kind=job.kind, queue_depth=position,
        )
        return job

    def submit_transcription(self, slug: str, doc_id: str, label: str) -> Job:
        return self.submit(Job("transcribe", label, slug=slug, doc_id=doc_id))

    def submit_download(self, model_key: str) -> Job:
        spec = catalog.model_spec(model_key)
        label = f"Download {spec.label if spec else model_key}"
        return self.submit(Job("download", label, model=model_key))

    # -- queries -----------------------------------------------------------

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def current(self) -> Job | None:
        return self._current

    def pending_count(self) -> int:
        with self._lock:
            return sum(
                1 for j in self._jobs.values() if j.status in ("queued", "running")
            )

    def snapshot(self, limit: int = 50) -> dict:
        with self._lock:
            jobs = [self._jobs[i].to_dict() for i in self._order[-limit:] if i in self._jobs]
        current = self._current
        return {
            "current": current.to_dict() if current else None,
            "pending": self.pending_count(),
            "jobs": list(reversed(jobs)),
            "worker_alive": bool(self._worker and self._worker.is_alive()),
        }

    def heartbeat_state(self) -> dict:
        """Compact state for the console heartbeat."""
        current = self._current
        if current is None:
            return {
                "busy": False,
                "pending": self.pending_count(),
                "worker_alive": bool(self._worker and self._worker.is_alive()),
            }
        return {
            "busy": True,
            "pending": self.pending_count(),
            "worker_alive": bool(self._worker and self._worker.is_alive()),
            "job": current.id,
            "label": current.label,
            "progress": round(current.progress, 3),
            "detail": current.detail,
            "eta_text": current.stats.get("eta_text"),
            "elapsed": round(current.elapsed, 1),
        }

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job is None or job.status in ("done", "failed", "cancelled"):
            return False
        job.cancel_requested = True
        if job.status == "queued":
            job.status = "cancelled"
            job.detail = "Cancelled before it started"
            job.finished_at = _now()
            CONSOLE.warn(f"Cancelled before starting: {job.label}", job=job.id)
        else:
            CONSOLE.warn(f"Cancellation requested for {job.label}", job=job.id)
        return True

    def cancel_all(self) -> int:
        with self._lock:
            targets = [
                j for j in self._jobs.values() if j.status in ("queued", "running")
            ]
        for job in targets:
            self.cancel(job.id)
        return len(targets)

    # -- worker ------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                job_id = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if job_id is None:
                break

            job = self.get(job_id)
            if job is None or job.status == "cancelled":
                continue

            self._current = job
            job.status = "running"
            job.started_at = _now()
            job._started_monotonic = time.monotonic()

            try:
                if job.kind == "transcribe":
                    self._do_transcription(job)
                elif job.kind == "download":
                    self._do_download(job)
                else:
                    raise RuntimeError(f"Unknown job kind '{job.kind}'")

                if job.status == "running":
                    job.status = "done"
                    job.progress = 1.0

            except Cancelled:
                job.status = "cancelled"
                job.detail = "Cancelled"
                CONSOLE.warn(f"Cancelled: {job.label}", job=job.id)
                self._mark_document(job, "cancelled", "Cancelled by the user")

            except Exception as exc:
                job.status = "failed"
                job.error = str(exc)
                job.detail = f"Failed: {exc}"
                CONSOLE.error(f"{job.label} failed: {exc}", job=job.id)
                CONSOLE.debug("Traceback", traceback=traceback.format_exc())
                self._mark_document(job, "failed", str(exc), error=str(exc))

            finally:
                job.finished_at = _now()
                self._current = None

    def _mark_document(self, job: Job, status: str, detail: str, error: str | None = None) -> None:
        slug, doc_id = job.payload.get("slug"), job.payload.get("doc_id")
        if not slug or not doc_id:
            return
        try:
            projects.set_status(slug, doc_id, status, detail, error)
        except Exception:
            pass

    # -- transcription -----------------------------------------------------

    def _do_transcription(self, job: Job) -> None:
        slug = job.payload["slug"]
        doc_id = job.payload["doc_id"]
        settings = SETTINGS.all()

        project = projects.load_project(slug)
        record = projects.load_document(slug, doc_id)
        source = projects.media_file(slug, doc_id)

        CONSOLE.step(
            f"=== Transcribing {record['filename']} ===",
            job=job.id, project=project.get("title"), file=record["filename"],
            size=fmt_bytes(record.get("size_bytes")),
        )

        def should_cancel() -> bool:
            return job.cancel_requested

        def stage(detail: str, progress: float) -> None:
            job.detail = detail
            job.progress = progress

        # --- 1. probe ------------------------------------------------------
        stage("Inspecting the file", 0.01)
        projects.set_status(slug, doc_id, "probing", "Inspecting the file")
        t0 = time.monotonic()
        info = media.probe(source, display_name=record["filename"])
        CONSOLE.info(
            f"Media: {media.describe(info)}",
            container=info.container, audio_codec=info.audio_codec,
            sample_rate=info.sample_rate, channels=info.channels,
            duration=round(info.duration or 0.0, 2),
            kind=info.kind, browser_playable=info.browser_playable,
        )
        if info.has_video:
            CONSOLE.info(
                "This is a video file; its audio track will be extracted and "
                "the video itself ignored.",
                video_codec=info.video_codec,
                resolution=f"{info.width}x{info.height}" if info.width else None,
            )
        CONSOLE.debug(f"Probe took {time.monotonic() - t0:.2f}s")

        if should_cancel():
            raise Cancelled()

        # --- 2. browser preview + waveform ---------------------------------
        doc_dir = projects.document_dir(slug, doc_id)
        preview_rel = None
        if not info.browser_playable and bool(settings.get("keep_preview_audio", True)):
            stage("Preparing browser-playable audio", 0.04)
            CONSOLE.step(
                f"{Path(source).suffix or 'This format'} cannot play in a "
                "browser, so a compact .m4a companion is being created for the "
                "transcript editor. The original file is not modified."
            )
            t0 = time.monotonic()
            preview = media.make_preview(source, doc_dir / "preview.m4a")
            preview_rel = "preview.m4a"
            CONSOLE.success(
                f"Preview audio written in {time.monotonic() - t0:.1f}s "
                f"({fmt_bytes(preview.stat().st_size)})"
            )
        elif info.browser_playable:
            CONSOLE.debug("Source is browser-playable; no preview needed.")

        if should_cancel():
            raise Cancelled()

        # --- 3. decode -----------------------------------------------------
        stage("Decoding audio to 16 kHz mono", 0.08)
        projects.set_status(slug, doc_id, "decoding", "Decoding audio")
        CONSOLE.step("Decoding audio to 16 kHz mono PCM (the format Whisper expects)")
        t0 = time.monotonic()
        last_report = [0.0]

        def decode_progress(seconds: float) -> None:
            if seconds - last_report[0] >= 300:
                last_report[0] = seconds
                CONSOLE.debug(f"  decoded {fmt_duration(seconds)} of audio")

        pcm = media.decode_to_pcm(source, progress=decode_progress)
        decode_seconds = time.monotonic() - t0
        duration = len(pcm) / media.TARGET_RATE
        CONSOLE.success(
            f"Decoded {fmt_duration(duration)} of audio in "
            f"{decode_seconds:.1f}s ({duration / max(decode_seconds, 0.01):.0f}x realtime)",
            samples=len(pcm), duration=round(duration, 2),
        )

        stage("Generating waveform", 0.1)
        try:
            media.write_waveform_cache(source, doc_dir / "waveform.json", duration)
            CONSOLE.debug("Waveform envelope cached for the editor's time ribbon")
        except Exception as exc:
            CONSOLE.warn(f"Waveform could not be generated: {exc}")

        projects.update_document(
            slug, doc_id,
            duration=round(duration, 3),
            media=info.to_dict(),
            preview=preview_rel,
        )

        if should_cancel():
            raise Cancelled()

        # --- 4. transcribe -------------------------------------------------
        stage("Loading the model", 0.12)
        projects.set_status(slug, doc_id, "transcribing", "Transcribing")

        glossary = (project.get("glossary") or "").strip()
        overrides = {}
        if glossary and not (settings.get("initial_prompt") or "").strip():
            overrides["initial_prompt"] = glossary
            CONSOLE.info(
                "Using the project glossary as the initial prompt to improve "
                "spelling of names and jargon.",
                characters=len(glossary),
            )

        collected: list = []

        def on_segment(index, segment, tracker):
            if job.cancel_requested:
                return
            snapshot = tracker.snapshot()
            # 0.12..0.95 of the job bar is the decode itself.
            job.progress = 0.12 + 0.83 * snapshot["progress"]
            job.stats = snapshot
            job.detail = (
                f"Transcribing {int(snapshot['progress'] * 100)}% - "
                f"{snapshot['eta_text']} remaining"
            )

            text = (segment.text or "").strip()
            collected.append(from_engine_segment(index, segment))

            # A line per segment with its timestamp, so the console reads like
            # a transcript in progress rather than a progress bar.
            CONSOLE.info(
                f"[{fmt_timestamp(float(segment.start or 0.0), millis=False)}] {text}",
                segment=index, start=round(float(segment.start or 0.0), 2),
                end=round(float(segment.end or 0.0), 2),
            )

            if index and index % CONSOLE_SEGMENT_STRIDE == 0:
                CONSOLE.stat(
                    f"  {int(snapshot['progress'] * 100)}% - "
                    f"{snapshot['words_per_second']:.1f} words/s, "
                    f"{snapshot['tokens_per_second']:.1f} tokens/s, "
                    f"{snapshot['realtime_factor']:.2f}x realtime, "
                    f"ETA {snapshot['eta_text']}",
                    **snapshot,
                )

        result = ENGINE.transcribe(
            pcm,
            duration=duration,
            overrides=overrides,
            on_segment=on_segment,
            should_cancel=should_cancel,
        )

        # --- 5. persist ----------------------------------------------------
        stage("Saving the transcript", 0.96)
        doc = new_document(
            duration=duration,
            model=result["stats"].get("model") or str(settings.get("model")),
            language=result["language"],
            language_probability=result["language_probability"],
            engine=result["params"],
        )
        doc["segments"] = collected
        doc["stats"] = result["stats"]
        normalise(doc)
        projects.save_transcript(slug, doc_id, doc, revision=False)

        info_summary = summary(doc)
        stats = result["stats"]
        CONSOLE.success(
            f"Transcription complete: {info_summary['words']} words in "
            f"{info_summary['segments']} segments, "
            f"{fmt_duration(stats['wall_seconds'])} of processing for "
            f"{fmt_duration(duration)} of audio "
            f"({stats['realtime_factor']:.2f}x realtime)",
            **{k: v for k, v in stats.items() if k not in ("eta", "eta_text")},
        )
        if info_summary["low_confidence"]:
            CONSOLE.info(
                f"{info_summary['low_confidence']} words were transcribed with "
                "low confidence and are highlighted in the editor for review.",
                low_confidence=info_summary["low_confidence"],
            )

        # --- 6. export -----------------------------------------------------
        stage("Writing output files", 0.98)
        written = self._export(slug, doc_id, doc, project, record)

        job.stats = dict(stats)
        job.stats["outputs"] = written
        job.detail = "Complete"
        projects.set_status(
            slug, doc_id, "transcribed",
            f"{info_summary['words']} words, ready for review",
        )
        CONSOLE.success(f"=== {record['filename']} done ===", job=job.id)

    def _export(self, slug: str, doc_id: str, doc: dict, project: dict, record: dict) -> dict:
        settings = SETTINGS.all()
        out_dir = projects.outputs_dir(slug, doc_id)
        meta = {
            "title": Path(record.get("filename") or "transcript").stem,
            "filename": record.get("filename"),
            "project": project.get("title"),
            "description": project.get("description"),
            "principal_investigator": project.get("principal_investigator"),
            "irb_protocol": project.get("irb_protocol"),
            "notes": project.get("consent_notes"),
            "generated": _now(),
        }
        t0 = time.monotonic()
        written = exporters.export_all(doc, out_dir, settings, meta)

        ok = [f for f, v in written.items() if not str(v).startswith("error")]
        bad = {f: v for f, v in written.items() if str(v).startswith("error")}
        CONSOLE.success(
            f"Wrote {len(ok)} output file(s) in {time.monotonic() - t0:.2f}s: "
            f"{', '.join('.' + f for f in ok)}",
            outputs=ok, folder=str(out_dir),
        )
        for fmt, message in bad.items():
            CONSOLE.error(f"Could not write the .{fmt} output: {message}")
        return written

    # -- model download ----------------------------------------------------

    def _do_download(self, job: Job) -> None:
        model_key = job.payload["model"]
        spec = catalog.model_spec(model_key)
        if spec is None:
            raise RuntimeError(f"Unknown model '{model_key}'")

        if SETTINGS.get("offline_lock"):
            raise RuntimeError(
                "The offline lock is engaged, so no download can be made. Turn "
                "it off in AI Settings if you intend to download a model, then "
                "turn it back on afterwards."
            )

        from huggingface_hub import snapshot_download

        CONSOLE.step(
            f"Downloading {spec.label} ({spec.download_mb} MB) from "
            f"{spec.repo}",
            model=model_key, repo=spec.repo,
        )
        CONSOLE.info(
            "This is the only outbound network request Local Scribe makes. "
            "Once the model is on disk you can engage the offline lock and the "
            "application will never reach the network again."
        )

        job.detail = f"Downloading {spec.label}"
        job.progress = 0.02
        target = catalog.model_cache_dir()

        stop_watch = threading.Event()
        watcher = threading.Thread(
            target=self._watch_download,
            args=(job, spec, target, stop_watch),
            daemon=True,
        )
        watcher.start()

        t0 = time.monotonic()
        try:
            path = snapshot_download(
                repo_id=spec.repo,
                cache_dir=str(target),
                allow_patterns=[
                    # CTranslate2 weights, tokenizer/config files.
                    "*.bin", "*.json", "*.txt", "*.model", "preprocessor_config.json",
                    # MLX weights (mlx-community repos ship one or the other).
                    "*.safetensors", "*.npz",
                ],
            )
        finally:
            stop_watch.set()
            watcher.join(timeout=2.0)

        elapsed = time.monotonic() - t0
        size = catalog.model_disk_bytes(model_key)
        job.progress = 1.0
        job.detail = "Downloaded"
        job.stats = {
            "bytes": size,
            "seconds": round(elapsed, 1),
            "path": str(path),
        }
        CONSOLE.success(
            f"{spec.label} downloaded in {fmt_duration(elapsed)} "
            f"({fmt_bytes(size)} on disk)",
            model=model_key, path=str(path),
        )
        CONSOLE.info(
            "Tip: engage the offline lock in AI Settings now to guarantee no "
            "further network access."
        )

    def _watch_download(self, job: Job, spec, target: Path, stop: threading.Event) -> None:
        """Report download growth, since snapshot_download has no callback."""
        expected = max(1, spec.download_mb) * 1_000_000
        last_logged = 0.0
        while not stop.wait(2.0):
            try:
                size = catalog.model_disk_bytes(spec.key)
            except Exception:
                continue
            fraction = min(0.98, size / expected)
            job.progress = max(0.02, fraction)
            job.detail = (
                f"Downloading {spec.label} - {fmt_bytes(size)} of about "
                f"{spec.download_mb} MB"
            )
            if size - last_logged > expected * 0.1:
                last_logged = size
                CONSOLE.stat(
                    f"  {int(fraction * 100)}% - {fmt_bytes(size)} downloaded",
                    bytes=size, fraction=round(fraction, 3),
                )


QUEUE = JobQueue()
