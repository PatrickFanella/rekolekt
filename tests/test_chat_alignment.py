"""Audio alignment of a re-upload to its original broadcast, on synthetic audio."""

import json
import shutil
import wave

import numpy as np
import pytest

from app.chat_alignment import AlignmentError, align_envelopes, audio_envelope
from scripts import import_chat

RATE = 8000
pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required to decode audio fixtures")


def _speech(seconds, seed):
    """Noise shaped by a random syllable-and-pause loudness pattern, like speech."""
    rng = np.random.default_rng(seed)
    levels, total = [], 0
    while total < seconds * RATE:
        length = int(rng.uniform(0.08, 0.4) * RATE)
        levels.append(np.full(length, 0.0 if rng.random() < 0.2 else rng.uniform(0.05, 0.8)))
        total += length
    return np.concatenate(levels)[: seconds * RATE] * rng.standard_normal(seconds * RATE)


def _wav(path, samples):
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())
    return path


@pytest.fixture(scope="module")
def broadcast(tmp_path_factory):
    samples = _speech(720, seed=1)
    return samples, _wav(tmp_path_factory.mktemp("audio") / "broadcast.wav", samples)


def test_known_offset_is_found_to_the_frame(broadcast, tmp_path):
    samples, broadcast_path = broadcast
    start = 37 * RATE + 320  # the re-upload starts 37.04 s into the broadcast
    noise = 0.01 * np.random.default_rng(9).standard_normal(len(samples) - start)
    recording = _wav(tmp_path / "recording.wav", samples[start:] * 0.5 + noise)

    alignment, report = align_envelopes(audio_envelope(broadcast_path), audio_envelope(recording))

    assert alignment.method == "audio" and alignment.confidence > 0.9
    assert [segment.as_dict() for segment in alignment.segments] == [
        {"source_start_ms": 37_040, "source_end_ms": 720_000, "recording_start_ms": 0}
    ]
    assert alignment.map(100_000) == 100_000 - 37_040
    assert report["matched_windows"] == report["windows"]


def test_a_cut_produces_two_segments_split_at_the_cut(broadcast, tmp_path):
    samples, broadcast_path = broadcast
    # The re-upload keeps 1:00-5:20 and 7:20-12:00, so the cut sits 260 s into the recording.
    edited = np.concatenate([samples[60 * RATE : 320 * RATE], samples[440 * RATE :]]) * 0.7
    recording = _wav(tmp_path / "edited.wav", edited)

    alignment, _ = align_envelopes(audio_envelope(broadcast_path), audio_envelope(recording))

    first, second = alignment.segments
    assert first.source_start_ms == 60_000 and first.recording_start_ms == 0
    assert second.source_start_ms - second.recording_start_ms == 180_000  # 2 minutes removed
    assert abs(second.recording_start_ms - 260_000) <= 100
    assert first.source_end_ms - first.source_start_ms == second.recording_start_ms
    assert second.source_end_ms == 720_000
    assert alignment.map(200_000) == 140_000
    assert alignment.map(380_000) is None  # chat during the removed section has no place
    assert alignment.map(500_000) == 320_000


def test_unrelated_audio_is_refused(broadcast, tmp_path):
    _, broadcast_path = broadcast
    other = _wav(tmp_path / "other.wav", _speech(600, seed=2))
    with pytest.raises(AlignmentError, match="below 0.6.*--offset-ms"):
        align_envelopes(audio_envelope(broadcast_path), audio_envelope(other))


def test_short_long_and_undecodable_audio_is_refused(tmp_path):
    with pytest.raises(AlignmentError, match="at least 60 seconds"):
        align_envelopes(np.zeros(100, dtype=np.float32), np.zeros(5000, dtype=np.float32))
    short = _wav(tmp_path / "short.wav", _speech(3, seed=3))
    with pytest.raises(AlignmentError, match="longer than the 1-second limit"):
        audio_envelope(short, max_seconds=1)
    garbage = tmp_path / "garbage.wav"
    garbage.write_bytes(b"not audio at all")
    with pytest.raises(AlignmentError, match="could not decode"):
        audio_envelope(garbage)
    with pytest.raises(AlignmentError, match="not found"):
        audio_envelope(tmp_path / "missing.wav")


def test_silence_is_not_counted_as_a_match():
    rng = np.random.default_rng(4)
    broadcast = rng.uniform(-60, -10, 20_000).astype(np.float32)
    recording = np.full(9000, -90, dtype=np.float32)
    with pytest.raises(AlignmentError, match="confidence 0.0"):
        align_envelopes(broadcast, recording)


def test_cli_align_prints_the_mapping(broadcast, tmp_path, capsys):
    samples, broadcast_path = broadcast
    noise = 0.01 * np.random.default_rng(5).standard_normal(len(samples) - 90 * RATE)
    recording = _wav(tmp_path / "recording.wav", samples[90 * RATE :] * 0.5 + noise)
    args = ["align", "--broadcast-audio", str(broadcast_path), "--recording-audio", str(recording)]

    assert import_chat.main(args) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["mapping"] == [{"source_start_ms": 90_000, "source_end_ms": 720_000, "recording_start_ms": 0}]

    assert import_chat.main([*args, "--min-confidence", "1"]) == 2
    assert "is below 1.0" in capsys.readouterr().err
