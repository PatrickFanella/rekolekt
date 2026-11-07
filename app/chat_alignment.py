"""Map an original broadcast's timeline onto a re-upload by comparing their audio.

Both files are decoded by ffmpeg to 8 kHz mono and reduced to a loudness
envelope of 50 frames per second (one 20 ms frame is 160 samples), so a
6-hour stream is about a million float32 values. Each minute of the recording
is cross-correlated against the whole broadcast envelope with an FFT; minutes
that agree on one lag form a segment, and the boundary between two segments
(a cut) is placed at the frame that best separates them. Confidence is the
share of informative recording minutes that matched, weighted by how well
they matched.

Imports only from the worker runtime: numpy is not an API dependency.
"""

from __future__ import annotations

import subprocess  # nosec B404 - fixed ffmpeg argument list, no shell
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from app.chat_import import DEFAULT_MIN_CONFIDENCE, Alignment, Segment

SAMPLE_RATE = 8000
FRAME_RATE = 50
FRAME_MS = 1000 // FRAME_RATE
HOP = SAMPLE_RATE // FRAME_RATE
MAX_SECONDS = 12 * 3600
WINDOW_FRAMES = 60 * FRAME_RATE
MIN_WINDOW_SCORE = 0.5
LAG_TOLERANCE_FRAMES = 2
MIN_WINDOW_DEVIATION_DB = 1.0


class AlignmentError(ValueError):
    """The audio cannot be aligned confidently; fall back to a manual offset."""


def audio_envelope(path: Path, *, max_seconds: int = MAX_SECONDS) -> np.ndarray:
    """Return the per-frame loudness in dBFS, decoded in bounded chunks."""
    if not path.is_file():
        raise AlignmentError(f"audio file not found: {path}")
    max_frames = max_seconds * FRAME_RATE
    chunk_bytes = HOP * FRAME_RATE * 60 * 2
    frames: list[np.ndarray] = []
    total = 0
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(  # nosec B603 - fixed argument list, no shell
            ["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE)]
            + ["-f", "s16le", "-"],
            stdout=subprocess.PIPE,
            stderr=errors,
        )
        assert process.stdout is not None
        try:
            while chunk := process.stdout.read(chunk_bytes):
                samples = np.frombuffer(chunk[: len(chunk) // (HOP * 2) * HOP * 2], dtype="<i2")
                power = np.square(samples.astype(np.float32) / 32768).reshape(-1, HOP).mean(axis=1)
                frames.append((10 * np.log10(power + 1e-9)).astype(np.float32))
                total += len(frames[-1])
                if total > max_frames:
                    raise AlignmentError(f"{path.name} is longer than the {max_seconds}-second limit")
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
        if process.returncode:
            errors.seek(0)
            detail = errors.read(500).decode(errors="replace").strip()
            raise AlignmentError(f"ffmpeg could not decode {path.name}: {detail}")
    return np.concatenate(frames) if frames else np.zeros(0, dtype=np.float32)


@dataclass
class _Run:
    first: int
    last: int
    lag: int
    score: float
    start: int = 0
    end: int = 0


def align_envelopes(
    broadcast: np.ndarray,
    recording: np.ndarray,
    *,
    window: int = WINDOW_FRAMES,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> tuple[Alignment, dict]:
    """Align two envelopes; raise ``AlignmentError`` below ``min_confidence``."""
    if len(broadcast) < window or len(recording) < window:
        raise AlignmentError(f"both audio files must be at least {window // FRAME_RATE} seconds long")
    matches = _match_windows(broadcast.astype(np.float64), recording.astype(np.float64), window)
    informative = [match for match in matches if match is not None]
    runs: list[_Run] = []
    for index, match in enumerate(matches):
        if match is None or match[1] < MIN_WINDOW_SCORE:
            continue
        lag, score = match
        run = runs[-1] if runs else None
        if run and run.last == index - 1 and abs(lag - run.lag) <= LAG_TOLERANCE_FRAMES:
            run.last, run.score = index, run.score + score
        else:
            runs.append(_Run(index, index, lag, score))
    confidence = round(sum(run.score for run in runs) / len(informative), 3) if informative else 0.0
    report = {
        "confidence": confidence,
        "windows": len(matches),
        "informative_windows": len(informative),
        "matched_windows": sum(run.last - run.first + 1 for run in runs),
        "window_seconds": window // FRAME_RATE,
    }
    if not runs or confidence < min_confidence:
        raise AlignmentError(
            f"audio alignment confidence {confidence} is below {min_confidence}; "
            "check that both files are the same broadcast, or import with --offset-ms"
        )
    _place_boundaries(runs, broadcast, recording, window)
    segments = tuple(
        Segment(
            source_start_ms=(run.start + run.lag) * FRAME_MS,
            source_end_ms=(run.end + run.lag) * FRAME_MS,
            recording_start_ms=run.start * FRAME_MS,
        )
        for run in runs
        if run.end > run.start
    )
    return Alignment("audio", segments, confidence), report


def _match_windows(broadcast: np.ndarray, recording: np.ndarray, window: int) -> list[tuple[int, float] | None]:
    """Best broadcast lag and Pearson score for each whole recording window; None when flat."""
    broadcast = broadcast - broadcast.mean()
    size = 1 << int(len(broadcast) + window - 1).bit_length()
    spectrum = np.fft.rfft(broadcast, size)
    sums = np.concatenate(([0.0], np.cumsum(broadcast)))
    squares = np.concatenate(([0.0], np.cumsum(broadcast * broadcast)))
    window_sums = sums[window:] - sums[:-window]
    norms = np.sqrt(np.maximum(squares[window:] - squares[:-window] - window_sums**2 / window, 1e-9))
    del sums, squares, window_sums
    matches: list[tuple[int, float] | None] = []
    for start in range(0, len(recording) - window + 1, window):
        part = recording[start : start + window]
        part = part - part.mean()
        norm = float(np.linalg.norm(part))
        if norm / np.sqrt(window) < MIN_WINDOW_DEVIATION_DB:
            matches.append(None)
            continue
        correlation = np.fft.irfft(spectrum * np.conj(np.fft.rfft(part, size)), size)[: len(norms)]
        scores = correlation / (norms * norm)
        best = int(np.argmax(scores))
        matches.append((best - start, float(scores[best])))
    return matches


def _place_boundaries(runs: list[_Run], broadcast: np.ndarray, recording: np.ndarray, window: int) -> None:
    """Set each run's recording frame range, splitting adjacent runs at the cut."""
    for run in runs:
        run.start, run.end = run.first * window, (run.last + 1) * window
    if runs[-1].end + window > len(recording):
        runs[-1].end = len(recording)
    for previous, following in zip(runs, runs[1:], strict=False):
        if following.first - previous.last > 2:
            continue  # unmatched minutes between them stay unmapped
        low, high = previous.end - window, following.start + window
        before = _frame_error(broadcast, recording, previous.lag, low, high, gain=(low, previous.end))
        after = _frame_error(broadcast, recording, following.lag, low, high, gain=(following.start, high))
        cost = np.concatenate(([0.0], np.cumsum(before))) + np.concatenate(([0.0], np.cumsum(after[::-1])))[::-1]
        previous.end = following.start = low + int(np.argmin(cost))
    for run in runs:
        # A segment cannot map past either end of the broadcast.
        run.start = max(run.start, -run.lag)
        run.end = max(run.start, min(run.end, len(broadcast) - run.lag))


def _frame_error(
    broadcast: np.ndarray, recording: np.ndarray, lag: int, low: int, high: int, *, gain: tuple[int, int]
) -> np.ndarray:
    """Loudness difference per recording frame in ``[low, high)`` under ``lag``.

    The gain offset between the files is measured over ``gain``, a window the
    lag is known to match. Frames with no broadcast audio under the lag count
    as a large error.
    """
    offset = float(np.median(recording[gain[0] : gain[1]] - broadcast[gain[0] + lag : gain[1] + lag]))
    frames = np.arange(low, high)
    source = frames + lag
    valid = (source >= 0) & (source < len(broadcast))
    error = np.full(len(frames), 100.0)
    error[valid] = np.abs(recording[frames[valid]] - broadcast[source[valid]] - offset)
    return error


def align_audio(
    broadcast_path: Path, recording_path: Path, *, min_confidence: float = DEFAULT_MIN_CONFIDENCE
) -> tuple[Alignment, dict]:
    """Decode both files and align them; see ``align_envelopes``."""
    return align_envelopes(
        audio_envelope(broadcast_path), audio_envelope(recording_path), min_confidence=min_confidence
    )
