#!/usr/bin/env python3
"""Import a broadcast's chat log onto a recording's timeline.

The log is a TwitchDownloader chat JSON file (timed from the start of the
Twitch VOD) or a rustlog JSON export plus the broadcast start time. Align it
with a manual --offset-ms, or with --broadcast-audio and --recording-audio to
compute a piecewise mapping from the two files' audio. Re-importing replaces
the previous import in one transaction.
"""

import argparse
import json
import sys
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.chat_import import (  # noqa: E402
    DEFAULT_MIN_CONFIDENCE,
    FORMATS,
    MAX_FILE_BYTES,
    ChatImportError,
    manual_alignment,
    read_chat_log,
    replace_chat,
)
from app.db import SessionLocal  # noqa: E402


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise argparse.ArgumentTypeError("use ISO 8601, for example 2026-11-07T18:02:11Z") from None
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("include a UTC offset, for example 2026-11-07T18:02:11Z")
    return parsed


def _audio_alignment(args):
    # numpy is a worker dependency; import only when audio alignment is requested.
    from app.chat_alignment import AlignmentError, align_audio

    try:
        return align_audio(args.broadcast_audio, args.recording_audio, min_confidence=args.min_confidence)
    except AlignmentError as exc:
        raise ChatImportError(str(exc)) from None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version="import-chat 1")
    commands = parser.add_subparsers(dest="command", required=True)
    align = commands.add_parser("align", help="Compute and print an audio alignment; writes nothing")
    align.add_argument("--broadcast-audio", type=Path, required=True, help="Audio of the original broadcast")
    imported = commands.add_parser("import", help="Validate, align and store a chat log for one recording")
    imported.add_argument("--video-id", type=uuid.UUID, required=True)
    imported.add_argument("--chat", type=Path, required=True, help="Chat log JSON file")
    imported.add_argument("--format", choices=FORMATS, required=True)
    imported.add_argument("--broadcast-start", type=_timestamp, help="Broadcast start time; rustlog only")
    imported.add_argument("--max-bytes", type=int, default=MAX_FILE_BYTES, help="Reject larger chat files")
    imported.add_argument("--dry-run", action="store_true", help="Validate and align, then roll back")
    method = imported.add_mutually_exclusive_group(required=True)
    method.add_argument("--offset-ms", type=int, help="Recording time minus chat time, in milliseconds")
    method.add_argument("--broadcast-audio", type=Path, help="Audio of the original broadcast")
    for command in (align, imported):
        command.add_argument("--recording-audio", type=Path, help="Audio of the archived recording")
        command.add_argument(
            "--min-confidence",
            type=float,
            default=DEFAULT_MIN_CONFIDENCE,
            help="Refuse audio alignments below this confidence, 0-1 (default %(default)s)",
        )
    args = parser.parse_args(argv)
    if (args.broadcast_audio is None) != (args.recording_audio is None):
        parser.error("--broadcast-audio and --recording-audio go together")
    if not 0 < args.min_confidence <= 1:
        parser.error("--min-confidence must be in (0, 1]")

    try:
        if args.command == "align":
            alignment, report = _audio_alignment(args)
            print(json.dumps({**report, "mapping": [segment.as_dict() for segment in alignment.segments]}))
            return 0
        log = read_chat_log(args.chat, args.format, broadcast_start=args.broadcast_start, max_bytes=args.max_bytes)
        if args.offset_ms is not None:
            alignment, report = manual_alignment(args.offset_ms), {}
        else:
            alignment, report = _audio_alignment(args)
        db = SessionLocal()
        try:
            result = replace_chat(db, str(args.video_id), log, alignment)
            if args.dry_run:
                db.rollback()
            else:
                db.commit()
        finally:
            db.close()
        print(json.dumps({**result, "alignment_report": report, "dry_run": args.dry_run}))
        return 0
    except (ChatImportError, OSError) as exc:
        print(f"import-chat: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
