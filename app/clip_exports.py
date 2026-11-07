"""Private, expiring clip spool for registered originals or yt-dlp time ranges."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

from .clip_handoff import provenance_key

MAX_SOURCE_BYTES = 10 * 1024**3
MAX_OUTPUT_BYTES = 100 * 1024**2
MAX_DURATION_MS = 120_000
TTL_SECONDS = 3600
MIN_FREE_BYTES = 20 * 1024**3
DOWNLOAD_TIMEOUT_SECONDS = 600
RENDER_TIMEOUT_SECONDS = 180


class ClipError(ValueError):
    pass


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def probe(path: Path) -> dict:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-show_entries",
            "format=duration,format_name:stream=codec_type,width,height",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=20,
    )
    data = json.loads(result.stdout)
    duration = float(data.get("format", {}).get("duration", 0))
    formats = set(data.get("format", {}).get("format_name", "").split(","))
    if not formats.intersection({"mov", "mp4", "matroska", "webm"}) or not math.isfinite(duration) or duration <= 0:
        raise ClipError("Use a finite MP4/MOV/WebM/Matroska original")
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    if not video or not 0 < video.get("width", 0) <= 7680 or not 0 < video.get("height", 0) <= 4320:
        raise ClipError("Original must contain video no larger than 7680 × 4320")
    return {"duration_ms": round(duration * 1000), "format": "mov" if "mov" in formats else "matroska"}


class ClipStore:
    def __init__(self, spool: Path, originals: Path | None = None, *, source="original", ttl_seconds=TTL_SECONDS):
        self.spool = spool.resolve()
        if source not in ("original", "youtube") or not 60 <= ttl_seconds <= 86400:
            raise ClipError("Use an original or youtube source and a cache lifetime of 60–86400 seconds")
        self.source = source
        self.ttl_seconds = ttl_seconds
        self.originals = originals.resolve(strict=True) if originals else None
        if source == "original" and self.originals is None:
            raise ClipError("Configure an originals directory for local-original exports")
        if self.originals is not None and (
            not self.originals.is_dir()
            or self.spool == self.originals
            or self.spool.is_relative_to(self.originals)
            or self.originals.is_relative_to(self.spool)
        ):
            raise ClipError("Use separate originals and private spool directories")
        self.spool.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.spool.stat().st_mode & 0o077:
            raise ClipError("Spool directory must be private (chmod 700)")
        for name in ("sources", "jobs"):
            (self.spool / name).mkdir(exist_ok=True, mode=0o700)

    @contextmanager
    def lock(self, name=".lock"):
        with (self.spool / name).open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ClipError("The local renderer is busy; please retry shortly") from exc
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def record_path(self, kind: str, key: str) -> Path:
        return self.spool / kind / f"{UUID(str(key))}.json"

    def load(self, kind: str, key: str) -> dict:
        path = self.record_path(kind, key)
        if not path.is_file():
            raise ClipError("Clip or authorized original not found")
        return dict(json.loads(path.read_text()))

    def save(self, kind: str, key: str, data: dict):
        path = self.record_path(kind, key)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, indent=2) + "\n")
        temporary.chmod(0o600)
        temporary.replace(path)

    def original(self, source: dict) -> Path:
        if self.originals is None:
            raise ClipError("Local originals are not configured")
        if source.get("revoked"):
            raise ClipError("Original authorization has been revoked")
        path = (self.originals / str(source["relative_path"])).resolve(strict=True)
        if not path.is_relative_to(self.originals) or not path.is_file():
            raise ClipError("Original must remain inside the configured originals directory")
        if not 0 < path.stat().st_size <= MAX_SOURCE_BYTES:
            raise ClipError("Original exceeds the 10 GiB input limit")
        if "mtime_ns" in source and (
            path.stat().st_mtime_ns != source["mtime_ns"] or path.stat().st_size != source["bytes"]
        ):
            raise ClipError("Original changed after registration")
        return path

    def register(
        self, video_id: str, owner_id: str, source_path: Path, rights_note: str, *, timeline_aligned: bool
    ) -> dict:
        if not timeline_aligned:
            raise ClipError("Confirm that original timestamps align with the archive recording")
        video_id, owner_id = str(UUID(video_id)), str(UUID(owner_id))
        if not 10 <= len(rights_note.strip()) <= 2000:
            raise ClipError("Record a specific authorization basis (10–2000 characters)")
        path = source_path.resolve(strict=True)
        if self.originals is None or not path.is_relative_to(self.originals):
            raise ClipError("Original is outside the configured originals directory")
        source = {
            "video_id": video_id,
            "owner_id": owner_id,
            "relative_path": str(path.relative_to(self.originals)),
            "rights_note": rights_note.strip(),
            "registered_at": time.time(),
            "revoked": False,
            "timeline_aligned": True,
        }
        self.original(source)
        source.update(probe(path))
        source["sha256"] = digest(path)
        source["bytes"] = path.stat().st_size
        source["mtime_ns"] = path.stat().st_mtime_ns
        with self.lock():
            if self.record_path("sources", video_id).exists():
                raise ClipError(
                    "Video already has a source record; use a new video ID or review/revoke the existing registration"
                )
            self.save("sources", video_id, source)
        return source

    def enqueue(
        self,
        video_id: str,
        owner_id: str,
        start_ms: int,
        end_ms: int,
        *,
        youtube_id: str | None = None,
        provenance: dict | None = None,
    ) -> dict:
        """Queue or reuse an export. ``provenance`` is the request-time transcript snapshot."""
        owner_id = str(UUID(owner_id))
        if (
            type(start_ms) is not int
            or type(end_ms) is not int
            or start_ms < 0
            or not 0 < end_ms - start_ms <= MAX_DURATION_MS
        ):
            raise ClipError("Select between 1 millisecond and 120 seconds")
        with self.lock():
            if self.source == "youtube":
                if not youtube_id or not re.fullmatch(r"[A-Za-z0-9_-]{11}", youtube_id):
                    raise ClipError("Source recording must have a valid YouTube video ID")
                source_key = {"source_kind": "youtube", "youtube_id": youtube_id}
            else:
                source = self.load("sources", video_id)
                if source["owner_id"] != owner_id:
                    raise ClipError("Only the registered original owner may request this export")
                self.original(source)
                if end_ms > source["duration_ms"]:
                    raise ClipError("Selection ends after the authorized original")
                source_key = {"source_sha256": source["sha256"]}
            active: list[dict] = [json.loads(p.read_text()) for p in (self.spool / "jobs").glob("*.json")]
            for previous in active:
                if (
                    previous["owner_id"] == owner_id
                    and previous["video_id"] == str(UUID(video_id))
                    and all(previous.get(k) == v for k, v in source_key.items())
                    and previous["start_ms"] == start_ms
                    and previous["end_ms"] == end_ms
                    and provenance_key(previous.get("provenance")) == provenance_key(provenance)
                    and previous["status"] in ("queued", "running", "completed")
                    and previous["expires_at"] > time.time()
                    and (previous["status"] != "completed" or self.media_path(previous["id"]).is_file())
                ):
                    return previous
            if (
                sum(j["expires_at"] > time.time() and j["status"] in ("queued", "running", "completed") for j in active)
                >= 20
            ):
                raise ClipError("Local export capacity reached; clean up expired exports before retrying")
            now = time.time()
            job = {
                "id": str(uuid4()),
                "video_id": str(UUID(video_id)),
                "owner_id": owner_id,
                **source_key,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "status": "queued",
                "created_at": now,
                "expires_at": now + self.ttl_seconds,
            }
            if provenance is not None:
                job["provenance"] = provenance
            self.save("jobs", job["id"], job)
            return job

    def status(self, job_id: str, owner_id: str) -> dict:
        job = self.load("jobs", job_id)
        if job["owner_id"] != str(UUID(owner_id)):
            raise ClipError("Clip or authorized original not found")
        if job.get("source_kind") != "youtube":
            self.original(self.load("sources", job["video_id"]))
        if job["expires_at"] <= time.time():
            return {**job, "status": "expired"}
        return job

    def output(self, job_id: str, owner_id: str) -> Path:
        job = self.status(job_id, owner_id)
        if job["status"] != "completed":
            raise ClipError("Clip is not complete or has expired")
        path = self.media_path(job_id)
        if not path.is_file() or digest(path) != job["output_sha256"]:
            raise ClipError("Clip output is missing or failed integrity verification")
        return path

    def media_path(self, job_id: str) -> Path:
        return self.spool / "jobs" / f"{UUID(job_id)}.mp4"

    def acquire_youtube(self, job: dict, directory: Path) -> Path:
        """Only archive IDs reach this boundary; no caller-supplied URLs or options."""
        from .settings import settings

        video_id = job["youtube_id"]
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            raise ClipError("Invalid YouTube video ID")
        command = [
            sys.executable,
            "-m",
            "yt_dlp",
            "--ignore-config",
            "--no-playlist",
            "--no-progress",
            "--no-cache-dir",
            "--no-write-info-json",
            "--no-write-thumbnail",
            "--no-write-subs",
            "--match-filter",
            "!is_live",
            "--retries",
            "2",
            "--fragment-retries",
            "2",
            "--socket-timeout",
            "30",
            "--js-runtimes",
            settings.JS_RUNTIME_CMD,
            "--download-sections",
            f"*{job['start_ms'] / 1000:.3f}-{job['end_ms'] / 1000:.3f}",
            "--force-keyframes-at-cuts",
            "--format",
            "bv[height<=720]+ba/b[height<=720]",
            "--merge-output-format",
            "mp4",
            "--remux-video",
            "mp4",
            "--downloader-args",
            f"ffmpeg_o:-fs {MAX_OUTPUT_BYTES} -threads 2",
            "--output",
            str(directory / "section.%(ext)s"),
        ]
        if settings.YTDLP_COOKIES_PATH:
            command.extend(["--cookies", settings.YTDLP_COOKIES_PATH])
        command.extend(["--", f"https://www.youtube.com/watch?v={video_id}"])
        # Kill the whole process group on timeout, including yt-dlp's ffmpeg child.
        with subprocess.Popen(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True
        ) as proc:
            try:
                if proc.wait(timeout=DOWNLOAD_TIMEOUT_SECONDS):
                    raise ClipError(
                        "YouTube clip download failed; the source may be unavailable or require authentication"
                    )
            except BaseException:
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
                raise
        output = directory / "section.mp4"
        if not output.is_file() or not 0 < output.stat().st_size <= MAX_OUTPUT_BYTES:
            raise ClipError("YouTube clip download did not produce a bounded MP4")
        return output

    def run(self, job_id: str) -> dict:
        # One renderer per spool. A crashed/interrupted run is never silently retried.
        with self.lock(".render.lock"):
            job = self.load("jobs", job_id)
            if job["status"] != "queued" or job["expires_at"] <= time.time():
                raise ClipError("Only an unexpired queued export can run")
            job.update(
                status="running",
                started_at=time.time(),
                expires_at=time.time() + DOWNLOAD_TIMEOUT_SECONDS + RENDER_TIMEOUT_SECONDS + 30,
            )
            self.save("jobs", job_id, job)
            output = self.media_path(job_id)
            partial = output.with_suffix(".partial.mp4")
            workspace = None
            try:
                if shutil.disk_usage(self.spool).free < MIN_FREE_BYTES + 2 * MAX_OUTPUT_BYTES:
                    raise ClipError("Insufficient spool space")
                if job.get("source_kind") == "youtube":
                    workspace = tempfile.TemporaryDirectory(prefix=f"{job['id']}-", dir=self.spool / "jobs")
                    original = self.acquire_youtube(job, Path(workspace.name))
                    source = probe(original)
                    start_seconds = 0
                else:
                    source = self.load("sources", job["video_id"])
                    original = self.original(source)
                    if digest(original) != job["source_sha256"] or source["sha256"] != job["source_sha256"]:
                        raise ClipError("Original changed after registration")
                    start_seconds = job["start_ms"] / 1000
                duration = (job["end_ms"] - job["start_ms"]) / 1000
                subprocess.run(
                    [
                        "ffmpeg",
                        "-nostdin",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-y",
                        "-protocol_whitelist",
                        "file,pipe",
                        "-f",
                        source["format"],
                        "-ss",
                        str(start_seconds),
                        "-i",
                        str(original),
                        "-t",
                        str(duration),
                        "-map",
                        "0:v:0",
                        "-map",
                        "0:a:0?",
                        "-map_metadata",
                        "-1",
                        "-map_chapters",
                        "-1",
                        "-vf",
                        "scale=1280:720:force_original_aspect_ratio=decrease:force_divisible_by=2",
                        "-c:v",
                        "libx264",
                        "-preset",
                        "veryfast",
                        "-crf",
                        "23",
                        "-threads",
                        "2",
                        "-c:a",
                        "aac",
                        "-b:a",
                        "128k",
                        "-movflags",
                        "+faststart",
                        "-fs",
                        str(MAX_OUTPUT_BYTES),
                        str(partial),
                    ],
                    check=True,
                    capture_output=True,
                    timeout=RENDER_TIMEOUT_SECONDS,
                )
                if (
                    partial.stat().st_size > MAX_OUTPUT_BYTES
                    or abs(probe(partial)["duration_ms"] - round(duration * 1000)) > 250
                ):
                    raise ClipError("Rendered output failed size/duration checks")
                if job.get("source_kind") != "youtube" and digest(original) != job["source_sha256"]:
                    raise ClipError("Original changed while rendering")
                partial.chmod(0o600)
                partial.replace(output)
                job.update(
                    status="completed",
                    output_sha256=digest(output),
                    output_bytes=output.stat().st_size,
                    completed_at=time.time(),
                    expires_at=time.time() + self.ttl_seconds,
                )
            except (OSError, ValueError, subprocess.SubprocessError, KeyboardInterrupt) as exc:
                partial.unlink(missing_ok=True)
                output.unlink(missing_ok=True)
                job.update(
                    status="failed",
                    error=(
                        str(exc)
                        if isinstance(exc, ClipError)
                        else "Renderer failed or was interrupted; inspect the original and worker environment"
                    ),
                )
                self.save("jobs", job_id, job)
                if isinstance(exc, KeyboardInterrupt):
                    raise
                return job
            finally:
                if workspace is not None:
                    workspace.cleanup()
            self.save("jobs", job_id, job)
            return job

    def revoke(self, video_id: str):
        with self.lock(".render.lock"), self.lock():
            source = self.load("sources", video_id)
            source["revoked"] = True
            self.save("sources", video_id, source)
            for path in (self.spool / "jobs").glob("*.json"):
                job = json.loads(path.read_text())
                if job["video_id"] == str(UUID(video_id)):
                    path.with_suffix(".mp4").unlink(missing_ok=True)
                    path.with_suffix(".partial.mp4").unlink(missing_ok=True)
                    job["status"] = "revoked"
                    self.save("jobs", job["id"], job)
        return {"video_id": str(UUID(video_id)), "revoked": True}

    def cleanup(self, apply: bool = False) -> dict:
        removed = []
        with self.lock(".render.lock"), self.lock():
            for path in (self.spool / "jobs").glob("*.json"):
                job = json.loads(path.read_text())
                if job["expires_at"] <= time.time() or job["status"] in ("failed", "revoked"):
                    removed.append(job["id"])
                    if apply:
                        path.with_suffix(".mp4").unlink(missing_ok=True)
                        path.with_suffix(".partial.mp4").unlink(missing_ok=True)
                        for workspace in path.parent.glob(f"{job['id']}-*"):
                            if workspace.is_dir():
                                shutil.rmtree(workspace)
                        path.unlink()
        return {"apply": apply, "jobs": removed}
