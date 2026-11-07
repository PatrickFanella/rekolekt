#!/usr/bin/env python3
"""Measure automatic speaker matching against a hand-labeled sample.

The sample is a CSV with columns ``video_id,speaker_label,person_slug``.
Leave ``person_slug`` empty for a speaker who is not an enrolled person.
For each labeled speaker the matcher picks the closest enrolled voice, never
using references taken from the same recording, and the script prints
precision and recall at each threshold. It only reads the database.

    python scripts/evaluate_speaker_matching.py sample.csv --thresholds 0.5,0.6,0.7,0.8
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from uuid import UUID

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app import speakers  # noqa: E402
from app.db import session_scope  # noqa: E402

MAX_SAMPLE_ROWS = 5000


def load_sample(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = []
        for row in csv.DictReader(handle):
            rows.append(
                {
                    "video_id": str(UUID(row["video_id"].strip())),
                    "speaker_label": row["speaker_label"].strip(),
                    "person_slug": (row.get("person_slug") or "").strip(),
                }
            )
            if len(rows) > MAX_SAMPLE_ROWS:
                raise SystemExit(f"sample has more than {MAX_SAMPLE_ROWS} rows")
    return rows


def predict(db, labeled: list[dict]) -> list[dict]:
    predictions = []
    for video_id in sorted({row["video_id"] for row in labeled}):
        for label, match in speakers.suggest_people(db, UUID(video_id)).items():
            predictions.append({"video_id": video_id, "speaker_label": label, **match})
    return predictions


def report(results: list[dict], labeled: list[dict], out=sys.stdout) -> None:
    known = sum(1 for row in labeled if row["person_slug"])
    print(f"labeled speakers: {len(labeled)} (enrolled people: {known})", file=out)
    print("threshold\tpredicted\tcorrect\tprecision\trecall", file=out)
    for row in results:
        precision = "n/a" if row["precision"] is None else f"{row['precision']:.3f}"
        recall = "n/a" if row["recall"] is None else f"{row['recall']:.3f}"
        print(f"{row['threshold']:.2f}\t{row['predicted']}\t{row['correct']}\t{precision}\t{recall}", file=out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("sample", type=Path, help="CSV with video_id,speaker_label,person_slug")
    parser.add_argument("--thresholds", default="0.5,0.6,0.7,0.75,0.8,0.85,0.9")
    args = parser.parse_args(argv)
    thresholds = [float(value) for value in args.thresholds.split(",") if value.strip()]
    labeled = load_sample(args.sample)
    with session_scope() as db:
        predictions = predict(db, labeled)
    report(speakers.evaluate_thresholds(labeled, predictions, thresholds), labeled)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
