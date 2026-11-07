#!/usr/bin/env python3
"""Attribute diarized speakers to enrolled people by voice embedding.

Without --apply, or while SPEAKER_AUTO_MATCH_ENABLED is false, this only
prints each label's best match and score. With both, it writes ``auto``
mappings whose score reaches SPEAKER_MATCH_THRESHOLD, leaving labels that
already have a current mapping (including cleared ones) alone.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import text  # noqa: E402

from app import speakers  # noqa: E402
from app.db import session_scope  # noqa: E402
from app.settings import settings  # noqa: E402


def _videos_with_unmapped_labels(db, limit: int) -> list:
    return list(
        db.execute(
            text("""
                SELECT c.video_id FROM video_speaker_centroids c
                WHERE NOT EXISTS (
                    SELECT 1 FROM video_speaker_people m
                    WHERE m.video_id = c.video_id AND m.speaker_label = c.speaker_label AND m.invalidated_at IS NULL
                )
                GROUP BY c.video_id
                ORDER BY max(c.created_at) DESC, c.video_id
                LIMIT :limit
            """),
            {"limit": limit},
        ).scalars()
    )


def run(db, *, limit: int, apply: bool, out=sys.stdout) -> int:
    write = apply and settings.SPEAKER_AUTO_MATCH_ENABLED
    if apply and not write:
        print("SPEAKER_AUTO_MATCH_ENABLED is false; reporting only", file=out)
    threshold = settings.SPEAKER_MATCH_THRESHOLD
    written = 0
    for video_id in _videos_with_unmapped_labels(db, limit):
        if write:
            for match in speakers.auto_match_video(db, video_id, threshold):
                written += 1
                print(f"{video_id}\t{match['speaker_label']}\t{match['slug']}\t{match['score']:.3f}\twritten", file=out)
            continue
        for label, match in sorted(speakers.suggest_people(db, video_id).items()):
            verdict = "would write" if match["score"] >= threshold else "below threshold"
            print(f"{video_id}\t{label}\t{match['slug']}\t{match['score']:.3f}\t{verdict}", file=out)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=50, choices=range(1, 1001), metavar="1..1000")
    parser.add_argument("--apply", action="store_true", help="write auto mappings (needs SPEAKER_AUTO_MATCH_ENABLED)")
    args = parser.parse_args(argv)
    with session_scope() as db:
        written = run(db, limit=args.limit, apply=args.apply)
    print(f"auto mappings written: {written}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
