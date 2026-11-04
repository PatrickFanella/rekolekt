#!/usr/bin/env python3
"""Refresh cached archive summary statistics.

This intentionally does the expensive transcript word-count pass out of band so
the public homepage can load from a tiny singleton table.
"""

import sys
from pathlib import Path

# Runnable from any working directory, like the sibling refresh scripts.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.archive.refresher import refresh_archive_summary_stats
from app.db import session_scope


def main() -> None:
    with session_scope() as db:
        stats = refresh_archive_summary_stats(db)
        print("archive_summary_stats refreshed: " f"videos={stats.video_count} " f"words={stats.transcript_word_count}")


if __name__ == "__main__":
    main()
