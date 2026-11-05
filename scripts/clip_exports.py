#!/usr/bin/env python3
"""Private clip rendering and automatic expiry cleanup."""

import argparse
import json
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.clip_exports import ClipError, ClipStore  # noqa: E402
from app.settings import settings  # noqa: E402


def run_next(store):
    store.cleanup(apply=True)
    queued = [json.loads(path.read_text()) for path in (store.spool / "jobs").glob("*.json")]
    queued = sorted(
        (job for job in queued if job["status"] == "queued" and job["expires_at"] > time.time()),
        key=lambda job: job["created_at"],
    )
    return store.run(queued[0]["id"]) if queued else {"status": "idle"}


def interrupt(signum, frame):
    raise KeyboardInterrupt


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version="clip-exports 1")
    parser.add_argument(
        "--spool", type=Path, default=settings.CLIP_SPOOL_DIR or None, help="Private spool, or CLIP_SPOOL_DIR"
    )
    parser.add_argument("--originals", type=Path, default=settings.CLIP_ORIGINALS_ROOT or None)
    parser.add_argument("--export-source", choices=("original", "youtube"), default=settings.CLIP_EXPORT_SOURCE)
    parser.add_argument(
        "--cache-ttl", type=int, default=settings.CLIP_CACHE_TTL_SECONDS, help="Seconds after completion"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    register = commands.add_parser("register", help="Register one authorized original; never downloads media")
    register.add_argument("--video-id", required=True)
    register.add_argument("--owner-id", required=True, help="Archive account UUID authorized to request exports")
    register.add_argument("--source", type=Path, required=True)
    register.add_argument(
        "--rights-note", required=True, help="Specific declared authorization basis; not independent proof"
    )
    register.add_argument(
        "--confirm-timeline-alignment",
        action="store_true",
        required=True,
        help="Confirm original and archive timestamps match exactly",
    )
    enqueue = commands.add_parser("enqueue", help="Queue a bounded passage from the configured source")
    enqueue.add_argument("--video-id", required=True)
    enqueue.add_argument("--owner-id", required=True)
    enqueue.add_argument("--start-ms", type=int, required=True)
    enqueue.add_argument("--end-ms", type=int, required=True)
    enqueue.add_argument("--youtube-id", help="Archive YouTube ID for the youtube source")
    run = commands.add_parser("run", help="Render one queued job with ffmpeg, then exit")
    run.add_argument("job_id")
    commands.add_parser("run-next", help="Render the oldest unexpired queued job, then exit")
    work = commands.add_parser("work", help="Continuously render queued clips and remove expired media")
    work.add_argument("--poll-seconds", type=int, choices=range(1, 61), default=5)
    status = commands.add_parser("status", help="Read an owned job; expired outputs are unavailable")
    status.add_argument("job_id")
    status.add_argument("--owner-id", required=True)
    revoke = commands.add_parser("revoke", help="Revoke a source and remove its rendered outputs")
    revoke.add_argument("video_id")
    cleanup = commands.add_parser("cleanup", help="Preview expired/failed output cleanup")
    cleanup.add_argument("--apply", action="store_true", help="Remove listed job metadata and generated media")
    args = parser.parse_args(argv)
    if not args.spool:
        parser.error("Set --spool or CLIP_SPOOL_DIR")
    signal.signal(signal.SIGTERM, interrupt)
    try:
        store = ClipStore(args.spool, args.originals, source=args.export_source, ttl_seconds=args.cache_ttl)
        if args.command == "register":
            result = store.register(
                args.video_id,
                args.owner_id,
                args.source,
                args.rights_note,
                timeline_aligned=args.confirm_timeline_alignment,
            )
        elif args.command == "enqueue":
            result = store.enqueue(args.video_id, args.owner_id, args.start_ms, args.end_ms, youtube_id=args.youtube_id)
        elif args.command == "run":
            result = store.run(args.job_id)
        elif args.command == "run-next":
            result = run_next(store)
        elif args.command == "work":
            while True:
                try:
                    result = run_next(store)
                    if result["status"] != "idle":
                        print(json.dumps(result), flush=True)
                        continue
                except ClipError as exc:
                    print(f"clip-exports: {exc}", file=sys.stderr, flush=True)
                time.sleep(args.poll_seconds)
        elif args.command == "status":
            result = store.status(args.job_id, args.owner_id)
        elif args.command == "revoke":
            result = store.revoke(args.video_id)
        else:
            result = store.cleanup(args.apply)
        print(json.dumps(result))
        return 1 if result.get("status") == "failed" else 0
    except (ClipError, OSError, ValueError) as exc:
        print(f"clip-exports: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("clip-exports: interrupted; inspect job status before retrying", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
