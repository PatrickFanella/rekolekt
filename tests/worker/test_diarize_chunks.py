"""Chunked diarization and cross-window speaker linking with a mocked pyannote."""

import math
import wave
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

from worker import diarize
from worker.diarize import WindowDiarization, link_window_speakers


def _window(tracks, embeddings):
    return WindowDiarization(tracks=tracks, embeddings=embeddings)


def test_linking_keeps_one_label_for_a_speaker_heard_in_several_windows():
    windows = [
        _window([(0.0, 3.0, "SPEAKER_00")], {"SPEAKER_00": [1.0, 0.0, 0.0]}),
        # The host comes back as the window's second local label; a guest appears first.
        _window(
            [(4.0, 5.0, "SPEAKER_00"), (5.0, 8.0, "SPEAKER_01")],
            {"SPEAKER_00": [0.0, 1.0, 0.0], "SPEAKER_01": [0.9, 0.1, 0.0]},
        ),
    ]

    result = link_window_speakers(windows, threshold=0.5)

    assert result.tracks == [(0.0, 3.0, "Speaker 1"), (4.0, 5.0, "Speaker 2"), (5.0, 8.0, "Speaker 1")]
    assert result.speech_seconds == {"Speaker 1": 6.0, "Speaker 2": 1.0}
    assert set(result.centroids) == {"Speaker 1", "Speaker 2"}
    for vector in result.centroids.values():
        assert math.isclose(math.sqrt(sum(v * v for v in vector)), 1.0)
    # The host's centroid is the speech-weighted mean of both windows.
    assert result.centroids["Speaker 1"][0] > result.centroids["Speaker 1"][1] > 0


def test_speakers_below_the_threshold_stay_separate():
    windows = [
        _window([(0.0, 2.0, "A")], {"A": [1.0, 0.0]}),
        _window([(2.0, 4.0, "A")], {"A": [0.6, 0.8]}),  # cosine 0.6
    ]

    assert {label for _s, _e, label in link_window_speakers(windows, threshold=0.7).tracks} == {
        "Speaker 1",
        "Speaker 2",
    }
    assert {label for _s, _e, label in link_window_speakers(windows, threshold=0.6).tracks} == {"Speaker 1"}


def test_two_local_speakers_never_merge_into_one_recording_speaker():
    windows = [
        _window([(0.0, 2.0, "A")], {"A": [1.0, 0.0]}),
        _window([(2.0, 3.0, "A"), (3.0, 4.0, "B")], {"A": [0.95, 0.05], "B": [0.9, 0.1]}),
    ]

    result = link_window_speakers(windows, threshold=0.5)

    assert result.tracks == [(0.0, 2.0, "Speaker 1"), (2.0, 3.0, "Speaker 1"), (3.0, 4.0, "Speaker 2")]


def test_speaker_without_a_usable_embedding_gets_its_own_label_and_no_centroid():
    windows = [
        _window([(0.0, 2.0, "A")], {"A": [1.0, 0.0]}),
        _window([(2.0, 3.0, "A"), (3.0, 4.0, "B")], {"A": [1.0, 0.0], "B": [0.0, 0.0]}),
        _window([(4.0, 5.0, "A")], {"A": [float("nan"), 1.0]}),
    ]

    result = link_window_speakers(windows, threshold=0.5)

    assert [label for _s, _e, label in result.tracks] == ["Speaker 1", "Speaker 1", "Speaker 2", "Speaker 3"]
    assert set(result.centroids) == {"Speaker 1"}


def _write_wav(path, seconds, rate=1000):
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(np.zeros(seconds * rate, dtype="<i2").tobytes())


def _numpy_input():
    # Worker tests replace torch with a mock; feed the fake pipeline numpy arrays.
    return patch.object(
        diarize,
        "_pipeline_input",
        side_effect=lambda samples, rate: {"waveform": samples[None, :], "sample_rate": rate},
    )


class _FakePipeline:
    """Returns one speaker per window whose embedding identifies the true voice."""

    def __init__(self, voices, embeddings=True):
        self.voices = voices
        self.embeddings = embeddings
        self.window_samples = []

    def __call__(self, audio):
        index = len(self.window_samples)
        samples = audio["waveform"].shape[-1]
        self.window_samples.append(samples)
        seconds = samples / audio["sample_rate"]
        voice = self.voices[index]
        segment = SimpleNamespace(start=0.0, end=seconds)
        annotation = SimpleNamespace(
            itertracks=lambda yield_label: [(segment, "t", "SPEAKER_00")],
            labels=lambda: ["SPEAKER_00"],
        )
        vector = {"host": [1.0, 0.0, 0.0], "guest": [0.0, 1.0, 0.0]}[voice]
        return SimpleNamespace(
            speaker_diarization=annotation,
            speaker_embeddings=np.array([vector]) if self.embeddings else None,
        )


def test_long_recording_is_diarized_in_bounded_windows_with_consistent_labels(tmp_path, monkeypatch):
    wav = tmp_path / "audio.wav"
    _write_wav(wav, seconds=10)
    monkeypatch.setattr(diarize.settings, "DIARIZATION_CHUNK_SECONDS", 4)
    monkeypatch.setattr(diarize.settings, "DIARIZATION_LINK_THRESHOLD", 0.5)
    pipeline = _FakePipeline(["host", "guest", "host"])

    with _numpy_input(), patch.object(diarize, "_get_pipeline", return_value=pipeline):
        result = diarize.diarize_recording(wav)

    # No window exceeds the configured length: memory is bounded by the window.
    assert pipeline.window_samples == [4000, 4000, 2000]
    assert result.tracks == [(0.0, 4.0, "Speaker 1"), (4.0, 8.0, "Speaker 2"), (8.0, 10.0, "Speaker 1")]
    assert result.speech_seconds == {"Speaker 1": 6.0, "Speaker 2": 4.0}
    assert result.centroids["Speaker 1"] == pytest.approx([1.0, 0.0, 0.0])


def test_chunk_size_zero_diarizes_the_whole_recording_at_once(tmp_path, monkeypatch):
    wav = tmp_path / "audio.wav"
    _write_wav(wav, seconds=10)
    monkeypatch.setattr(diarize.settings, "DIARIZATION_CHUNK_SECONDS", 0)
    pipeline = _FakePipeline(["host"])

    with _numpy_input(), patch.object(diarize, "_get_pipeline", return_value=pipeline):
        result = diarize.diarize_recording(wav)

    assert pipeline.window_samples == [10000]
    assert result.tracks == [(0.0, 10.0, "Speaker 1")]


def test_chunked_recording_without_embeddings_fails_visibly(tmp_path, monkeypatch):
    wav = tmp_path / "audio.wav"
    _write_wav(wav, seconds=10)
    monkeypatch.setattr(diarize.settings, "DIARIZATION_CHUNK_SECONDS", 4)
    pipeline = _FakePipeline(["host", "guest", "host"], embeddings=False)

    with _numpy_input(), patch.object(diarize, "_get_pipeline", return_value=pipeline):
        with pytest.raises(RuntimeError, match="speaker embeddings"):
            diarize.diarize_recording(wav)


def test_unavailable_pipeline_raises(tmp_path):
    with patch.object(diarize, "_get_pipeline", return_value=None):
        with pytest.raises(RuntimeError, match="not available"):
            diarize.diarize_recording(tmp_path / "missing.wav")
