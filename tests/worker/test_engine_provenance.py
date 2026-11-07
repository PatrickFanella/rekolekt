"""The engine that actually ran is reported by the runner and stored with the transcript."""

import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest

from worker import whisper_runner
from worker.audio import Chunk
from worker.maintenance import ModelUpgradePolicy, model_upgrade_policy
from worker.native_pipeline import (
    NativePipelineDependencies,
    VideoPipelineContext,
    _persist_transcript,
    _transcribe_chunks,
)


@pytest.fixture(autouse=True)
def reset_runner(monkeypatch):
    for name in ("_model", "_model_engine", "_model_loaded_at", "_fallback_ct2_model", "_fallback_engine"):
        monkeypatch.setattr(whisper_runner, name, None)
    monkeypatch.setattr(whisper_runner.metadata, "version", lambda dist: {"faster-whisper": "1.1.1"}.get(dist, "0"))


def _ct2_model(device="cuda"):
    model = MagicMock()
    model.model.device = device
    segment = SimpleNamespace(start=0.0, end=1.0, text=" hi ", avg_logprob=-0.1, temperature=0.0, tokens=[1])
    model.transcribe.return_value = ([segment], SimpleNamespace(language="en", language_probability=0.9))
    return model


def _gpu_settings(**overrides):
    values = dict(
        WHISPER_BACKEND="faster-whisper",
        WHISPER_MODEL="large-v3",
        FORCE_GPU=True,
        GPU_DEVICE_PREFERENCE="cuda",
        GPU_COMPUTE_TYPES="float16",
        GPU_MODEL_FALLBACKS="large-v3,medium",
        WHISPER_BEAM_SIZE=5,
        WHISPER_TEMPERATURE=0.0,
        WHISPER_WORD_TIMESTAMPS=False,
        WHISPER_LANGUAGE=None,
        WHISPER_VAD_FILTER=False,
        WHISPER_INITIAL_PROMPT="",
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_gpu_model_fallback_reports_the_model_that_loaded():
    fallback = _ct2_model()
    with (
        patch("worker.whisper_runner.settings", _gpu_settings()),
        patch("worker.whisper_runner._try_load_ct2", side_effect=[RuntimeError("out of memory"), fallback]),
    ):
        _segments, lang_info = whisper_runner.transcribe_chunk("/tmp/chunk.wav")

    assert lang_info["engine"] == {
        "model": "medium",
        "backend": "faster-whisper",
        "device": "cuda",
        "compute_type": "float16",
        "version": "1.1.1",
    }
    loaded = whisper_runner.loaded_engine()
    assert loaded is not None and loaded["model"] == "medium" and loaded["loaded_at"] is not None


def test_auto_device_reports_float32_retry():
    model = _ct2_model(device="cpu")
    settings = _gpu_settings(FORCE_GPU=False, WHISPER_MODEL="small")
    with (
        patch("worker.whisper_runner.settings", settings),
        patch("worker.whisper_runner._try_load_ct2", side_effect=[RuntimeError("no fp16"), model]),
        patch("worker.metrics.whisper_model_load_seconds"),
    ):
        _segments, lang_info = whisper_runner.transcribe_chunk("/tmp/chunk.wav")

    assert lang_info["engine"]["model"] == "small"
    assert lang_info["engine"]["device"] == "cpu"
    assert lang_info["engine"]["compute_type"] == "float32"


def test_rocm_crash_reports_the_ct2_fallback_engine():
    torch_model = MagicMock()
    torch_model.transcribe.side_effect = RuntimeError("Memory access fault by GPU node-1")
    whisper_runner._model = torch_model
    whisper_runner._model_engine = {"model": "large-v3", "backend": "whisper"}
    settings = _gpu_settings(WHISPER_BACKEND="whisper")
    fallback = _ct2_model(device="cuda")
    with (
        patch("worker.whisper_runner.settings", settings),
        patch("worker.whisper_runner._lazy_imports"),
        patch("worker.whisper_runner._torch", MagicMock()),
        patch("worker.whisper_runner._try_load_ct2", side_effect=[RuntimeError("large-v3 failed"), fallback]),
    ):
        _segments, lang_info = whisper_runner.transcribe_chunk("/tmp/chunk.wav")

    assert lang_info["engine"]["model"] == "medium"
    assert lang_info["engine"]["backend"] == "faster-whisper"
    assert lang_info["engine"]["compute_type"] == "float32"


def test_unloaded_runner_reports_no_engine():
    assert whisper_runner.loaded_engine() is None


PRIMARY = {"model": "large-v3", "backend": "whisper", "device": "cuda", "compute_type": "float32", "version": "2"}
FALLBACK = {"model": "medium", "backend": "faster-whisper", "device": "cuda", "compute_type": "float32", "version": "1"}


def _pipeline_settings():
    return SimpleNamespace(
        CHUNK_SECONDS=900,
        WHISPER_MODEL="large-v3",
        WHISPER_LANGUAGE=None,
        WHISPER_BEAM_SIZE=5,
        WHISPER_TEMPERATURE=0.0,
        WHISPER_WORD_TIMESTAMPS=True,
        WHISPER_VAD_FILTER=False,
    )


def _transcribe(tmp_path: Path, engines, quality_settings=None):
    ctx = VideoPipelineContext(
        engine=Mock(),
        video={"id": uuid.uuid4(), "youtube_id": "abc123", "job_id": uuid.uuid4()},
        work_dir=tmp_path,
        wav_path=tmp_path / "audio.wav",
        quality_settings=quality_settings or {},
    )
    transcribe = Mock(
        side_effect=[
            ([{"start": 0.0, "end": 1.0, "text": "x"}], {"language": "en", "language_probability": 0.9, "engine": e})
            for e in engines
        ]
    )
    deps = NativePipelineDependencies(
        settings=_pipeline_settings(),
        logger=Mock(),
        chunk_audio=Mock(
            return_value=[Chunk(path=tmp_path / f"c{i}.wav", offset=i * 900.0) for i in range(len(engines))]
        ),
        transcribe_chunk=transcribe,
    )
    with patch.dict("sys.modules", {"worker.metrics": MagicMock()}):
        _transcribe_chunks(ctx, deps)
    return ctx, transcribe


def test_a_chunk_that_fell_back_marks_the_whole_transcript(tmp_path: Path):
    ctx, _ = _transcribe(tmp_path, [PRIMARY, FALLBACK, PRIMARY])

    assert ctx.transcription_engine == FALLBACK


def test_quality_preset_model_does_not_change_the_recorded_engine(tmp_path: Path):
    ctx, transcribe = _transcribe(tmp_path, [PRIMARY], quality_settings={"preset": "fast", "model": "base"})

    assert ctx.transcription_engine["model"] == "large-v3"
    assert "model" not in transcribe.call_args.kwargs


def _persisted_params(tmp_path: Path, engine):
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.execute.return_value.first.return_value = {"id": 1}
    conn.execute.return_value.rowcount = 1
    db = Mock()
    db.begin.return_value = conn
    ctx = VideoPipelineContext(
        engine=db,
        video={"id": uuid.uuid4(), "youtube_id": "abc123", "job_id": uuid.uuid4()},
        work_dir=tmp_path,
        diar_segments=[{"start": 0.0, "end": 1.0, "text": "x"}],
        transcription_engine=engine,
    )
    deps = NativePipelineDependencies(
        settings=_pipeline_settings(), logger=Mock(), replace_transcript_blocks=Mock(), refresh_job_state=Mock()
    )
    _persist_transcript(ctx, deps)
    return next(c.args[1] for c in conn.execute.call_args_list if "INSERT INTO transcripts" in str(c.args[0]))


def test_persist_stores_the_engine_that_ran_not_the_configured_model(tmp_path: Path):
    params = _persisted_params(tmp_path, FALLBACK)

    assert params["m"] == "medium"
    assert (params["em"], params["eb"], params["ed"], params["ec"], params["ev"]) == (
        "medium",
        "faster-whisper",
        "cuda",
        "float32",
        "1",
    )


def test_persist_without_a_reported_engine_leaves_provenance_null(tmp_path: Path):
    params = _persisted_params(tmp_path, None)

    assert params["m"] == "large-v3"
    assert all(params[key] is None for key in ("em", "eb", "ed", "ec", "ev"))


def test_empty_target_disables_the_upgrade_policy():
    assert model_upgrade_policy(SimpleNamespace(MODEL_UPGRADE_TARGET_MODEL=" ")) is None


def test_policy_compares_names_so_unknown_models_still_work():
    policy = model_upgrade_policy(
        SimpleNamespace(
            MODEL_UPGRADE_TARGET_MODEL="distil-large-v3.5",
            MODEL_UPGRADE_TARGET_BACKEND="faster-whisper",
            MODEL_UPGRADE_TARGET_VERSION="",
        )
    )

    assert policy == ModelUpgradePolicy(target_model="distil-large-v3.5", target_backend="faster-whisper")
    assert policy.matches({"model": "distil-large-v3.5", "backend": "faster-whisper", "version": "9"})
    assert not policy.matches({"model": "distil-large-v3.5", "backend": "whisper"})
    assert not policy.matches({"model": "large-v3", "backend": "faster-whisper"})
