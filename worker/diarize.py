import gc
import math
import os
import wave
from dataclasses import dataclass
from pathlib import Path

from app.logging_config import get_logger
from app.settings import settings

logger = get_logger(__name__)

_pipeline = None
_pyannote_import_error = None


def _strict() -> bool:
    # Identity avoids accidentally treating an unset MagicMock attribute as true
    # in callers that patch only the settings relevant to a legacy inline path.
    return settings.DIARIZATION_STRICT is True


def _get_pipeline():
    global _pipeline
    if _pipeline is None:
        if not settings.ENABLE_DIARIZATION:
            logger.info("Diarization disabled; returning Whisper segments")
            return None
        if not settings.HF_TOKEN:
            if _strict():
                raise RuntimeError("DIARIZATION_STRICT requires HF_TOKEN")
            logger.warning("ENABLE_DIARIZATION is true but HF_TOKEN is missing; skipping diarization")
            return None
        # Lazy import to avoid hard dependency when diarization isn't needed
        try:
            from pyannote.audio import Pipeline  # type: ignore
        except Exception as ie:
            global _pyannote_import_error
            _pyannote_import_error = ie
            logger.warning("pyannote.audio not available; skipping diarization", extra={"error": str(ie)})
            if _strict():
                raise RuntimeError("strict diarization pipeline import failed") from ie
            return None
        os.environ.setdefault("HUGGINGFACE_HUB_TOKEN", settings.HF_TOKEN)
        os.environ.setdefault("HF_TOKEN", settings.HF_TOKEN)
        try:
            _pipeline = Pipeline.from_pretrained(settings.DIARIZATION_MODEL, token=settings.HF_TOKEN)
        except Exception as e:
            logger.warning("Failed to initialize pyannote Pipeline; skipping diarization", extra={"error": str(e)})
            if _strict():
                raise RuntimeError("strict diarization primary model load failed") from e
            try:
                # Fallback to older model
                _pipeline = Pipeline.from_pretrained(settings.DIARIZATION_FALLBACK_MODEL, token=settings.HF_TOKEN)
            except Exception as e2:
                logger.warning("Fallback pipeline also failed; skipping diarization", extra={"error": str(e2)})
                _pipeline = None
        if _pipeline is not None:
            device = settings.DIARIZATION_DEVICE.strip().lower()
            if device and device != "auto":
                try:
                    import torch

                    if device == "cuda" and not torch.cuda.is_available():
                        logger.warning(
                            "DIARIZATION_DEVICE=cuda requested but CUDA is not available; "
                            "leaving pyannote device unchanged"
                        )
                        if _strict():
                            raise RuntimeError("strict diarization CUDA placement is unavailable")
                    elif hasattr(_pipeline, "to"):
                        _pipeline.to(torch.device(device))
                        logger.info("Diarization pipeline moved to device", extra={"device": device})
                    elif _strict():
                        raise RuntimeError("strict diarization pipeline cannot be placed on requested device")
                except Exception as e:
                    logger.warning(
                        "Failed to move diarization pipeline to requested device",
                        extra={"device": device, "error": str(e)},
                    )
                    if _strict():
                        raise RuntimeError("strict diarization device placement failed") from e
    return _pipeline


def _build_audio_input(wav_path):
    """Build pyannote input while avoiding torchcodec path decoding when possible.

    pyannote.audio 4.x may try to decode file paths through torchcodec. Passing
    a preloaded waveform avoids accelerator-specific decoder paths and keeps
    diarization behavior consistent across CPU, CUDA, and ROCm workers.
    """
    try:
        import torchaudio  # type: ignore

        waveform, sample_rate = torchaudio.load(str(wav_path))
        return {"waveform": waveform, "sample_rate": sample_rate}
    except Exception as e:
        logger.warning(
            "Failed to preload audio for diarization; falling back to path input",
            extra={"error": str(e)},
        )
        if _strict():
            raise RuntimeError("strict diarization audio preload failed") from e
        return {"audio": str(wav_path)}


def run_diarization(wav_path):
    """Run pyannote diarization and return raw diarization tracks.

    Unlike diarize_and_align, this does not swallow pyannote exceptions. Use it
    from the standalone worker where failures should be visible/retryable by DB
    state rather than hidden as unlabeled transcript segments.
    """
    pipe = _get_pipeline()
    if pipe is None:
        raise RuntimeError("Diarization pipeline not available")
    logger.info("Running diarization", extra={"wav_path": str(wav_path)})
    diar_input = _build_audio_input(wav_path)
    diar_output = pipe(diar_input) if callable(pipe) else pipe(str(wav_path))
    diar = (
        diar_output
        if callable(getattr(diar_output, "itertracks", None))
        else getattr(diar_output, "speaker_diarization", diar_output)
    )
    diar_list = []
    label_first_time = {}
    for seg, _track, label in diar.itertracks(yield_label=True):
        diar_list.append((seg.start, seg.end, label))
        if label not in label_first_time:
            label_first_time[label] = seg.start
    ordered_labels = sorted(label_first_time.items(), key=lambda kv: kv[1])
    friendly = {raw: f"Speaker {i + 1}" for i, (raw, _t) in enumerate(ordered_labels)}
    logger.info("Diarization produced speaker segments", extra={"segment_count": len(diar_list)})
    return diar_list, friendly


def diarize_and_align(wav_path, whisper_segments):
    try:
        pipe = _get_pipeline()
        if pipe is None:
            logger.info("Diarization pipeline not available; returning whisper segments as-is")
            return whisper_segments
        diar_list, friendly = run_diarization(wav_path)
        diar_segments = []
        speakers_assigned = 0
        for w in whisper_segments:
            w_mid = (w["start"] + w["end"]) / 2.0
            speaker = None
            for ds in diar_list:
                if w_mid >= ds[0] and w_mid <= ds[1]:
                    # map raw label (e.g., SPEAKER_00) to friendly name
                    speaker = friendly.get(ds[2], ds[2])
                    speakers_assigned += 1
                    break
            # Persist as both 'speaker' (raw) and 'speaker_label' (API/DB expected field)
            w["speaker"] = speaker
            w["speaker_label"] = speaker
            diar_segments.append(w)
        logger.info(
            "Diarization assigned speakers to segments",
            extra={"speakers_assigned": speakers_assigned, "total_segments": len(whisper_segments)},
        )
        return diar_segments
    except Exception as e:
        logger.warning("Diarization failed; returning Whisper segments as-is", extra={"error": str(e)})
        return whisper_segments


@dataclass(frozen=True)
class WindowDiarization:
    """One diarized window: tracks in recording time and one embedding per local label."""

    tracks: list[tuple[float, float, str]]
    embeddings: dict[str, list[float] | None]


@dataclass(frozen=True)
class RecordingDiarization:
    """Speaker tracks for a whole recording with labels consistent across windows.

    Labels are ``Speaker N`` in order of first appearance. ``centroids`` holds a
    unit-length speaker embedding for each label that had a usable one, and
    ``speech_seconds`` the total speaking time per label.
    """

    tracks: list[tuple[float, float, str]]
    centroids: dict[str, list[float]]
    speech_seconds: dict[str, float]


def _unit(vector) -> list[float] | None:
    if vector is None:
        return None
    values = [float(v) for v in vector]
    norm = math.sqrt(sum(v * v for v in values))
    if not values or not math.isfinite(norm) or norm == 0.0:
        return None
    return [v / norm for v in values]


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def link_window_speakers(windows: list[WindowDiarization], threshold: float) -> RecordingDiarization:
    """Link each window's local speakers to recording speakers by cosine similarity.

    Within a window, local speakers are matched one-to-one to recording speakers,
    best similarity first, when the similarity reaches ``threshold``. A local
    speaker without a match, or without a usable embedding, starts a new
    recording speaker. Recording centroids are speech-time-weighted means of the
    matched window embeddings.
    """
    sums: list[list[float] | None] = []
    seconds: list[float] = []
    first_start: list[float] = []
    tracks: list[tuple[float, float, int]] = []

    def centroid(index: int) -> list[float] | None:
        total = sums[index]
        return _unit(total) if total is not None else None

    for window in windows:
        local_seconds: dict[str, float] = {}
        local_first: dict[str, float] = {}
        for start, end, label in window.tracks:
            local_seconds[label] = local_seconds.get(label, 0.0) + max(0.0, end - start)
            local_first[label] = min(local_first.get(label, start), start)
        local_vectors = {label: _unit(window.embeddings.get(label)) for label in local_seconds}

        candidates = []
        for label, vector in local_vectors.items():
            if vector is None:
                continue
            for index in range(len(sums)):
                known = centroid(index)
                if known is None or len(known) != len(vector):
                    continue
                similarity = _dot(vector, known)
                if similarity >= threshold:
                    candidates.append((similarity, label, index))
        assignment: dict[str, int] = {}
        taken: set[int] = set()
        for _similarity, label, index in sorted(candidates, key=lambda c: (-c[0], c[1], c[2])):
            if label in assignment or index in taken:
                continue
            assignment[label] = index
            taken.add(index)

        for label in sorted(local_seconds, key=lambda name: (local_first[name], name)):
            vector = local_vectors[label]
            if label not in assignment:
                sums.append(None)
                seconds.append(0.0)
                first_start.append(local_first[label])
                assignment[label] = len(sums) - 1
            index = assignment[label]
            weight = local_seconds[label]
            if vector is not None and weight > 0:
                current = sums[index]
                weighted = [weight * v for v in vector]
                sums[index] = weighted if current is None else [a + b for a, b in zip(current, weighted, strict=True)]
            seconds[index] += weight
            first_start[index] = min(first_start[index], local_first[label])
        tracks.extend((start, end, assignment[label]) for start, end, label in window.tracks)

    order = sorted(range(len(sums)), key=lambda index: (first_start[index], index))
    names = {index: f"Speaker {position + 1}" for position, index in enumerate(order)}
    centroids = {}
    for index, name in names.items():
        vector = centroid(index)
        if vector is not None:
            centroids[name] = vector
    return RecordingDiarization(
        tracks=sorted(((start, end, names[index]) for start, end, index in tracks), key=lambda t: (t[0], t[1], t[2])),
        centroids=centroids,
        speech_seconds={names[index]: seconds[index] for index in order},
    )


def _wav_frame_info(wav_path: Path) -> tuple[int, int]:
    with wave.open(str(wav_path), "rb") as reader:
        if reader.getsampwidth() != 2:
            raise ValueError("diarization expects 16-bit PCM WAV audio")
        return reader.getnframes(), reader.getframerate()


def _read_window(wav_path: Path, start_frame: int, frame_count: int):
    """Read one window of a 16-bit PCM WAV as mono float32, bounded by the window."""
    import numpy as np

    with wave.open(str(wav_path), "rb") as reader:
        channels = reader.getnchannels()
        sample_rate = reader.getframerate()
        reader.setpos(start_frame)
        raw = reader.readframes(frame_count)
    samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    return np.ascontiguousarray(samples), sample_rate


def _pipeline_input(samples, sample_rate: int) -> dict:
    import torch

    return {"waveform": torch.from_numpy(samples).unsqueeze(0), "sample_rate": sample_rate}


def _diarize_window(pipe, audio: dict, offset_seconds: float) -> WindowDiarization:
    output = pipe(audio)
    diar = output if callable(getattr(output, "itertracks", None)) else output.speaker_diarization
    tracks = [
        (offset_seconds + float(seg.start), offset_seconds + float(seg.end), str(label))
        for seg, _track, label in diar.itertracks(yield_label=True)
    ]
    raw_embeddings = getattr(output, "speaker_embeddings", None)
    labels = [str(label) for label in diar.labels()] if callable(getattr(diar, "labels", None)) else []
    embeddings: dict[str, list[float] | None] = {label: None for _s, _e, label in tracks}
    if raw_embeddings is not None:
        for position, label in enumerate(labels):
            if position < len(raw_embeddings):
                embeddings[label] = [float(v) for v in raw_embeddings[position]]
    return WindowDiarization(tracks=tracks, embeddings=embeddings)


def diarize_recording(wav_path) -> RecordingDiarization:
    """Diarize a whole recording in bounded windows with consistent speaker labels.

    Each window of ``DIARIZATION_CHUNK_SECONDS`` is read and diarized on its own,
    so memory depends on the window length rather than the recording length.
    Window speakers are linked by pyannote's per-speaker embeddings. Errors are
    raised so the caller can record a visible failure.
    """
    pipe = _get_pipeline()
    if pipe is None:
        raise RuntimeError("Diarization pipeline not available")
    wav_path = Path(wav_path)
    total_frames, sample_rate = _wav_frame_info(wav_path)
    chunk_seconds = int(settings.DIARIZATION_CHUNK_SECONDS)
    window_frames = total_frames if chunk_seconds <= 0 else chunk_seconds * sample_rate
    window_frames = max(1, window_frames)
    windows: list[WindowDiarization] = []
    start_frame = 0
    while start_frame < total_frames:
        frame_count = min(window_frames, total_frames - start_frame)
        logger.info(
            "Diarizing window",
            extra={"start_seconds": start_frame / sample_rate, "seconds": frame_count / sample_rate},
        )
        samples, rate = _read_window(wav_path, start_frame, frame_count)
        windows.append(_diarize_window(pipe, _pipeline_input(samples, rate), start_frame / sample_rate))
        del samples
        gc.collect()
        start_frame += frame_count
    if len(windows) > 1 and all(_unit(vector) is None for window in windows for vector in window.embeddings.values()):
        raise RuntimeError("chunked diarization requires pyannote speaker embeddings to link windows")
    result = link_window_speakers(windows, float(settings.DIARIZATION_LINK_THRESHOLD))
    logger.info(
        "Diarized recording",
        extra={"windows": len(windows), "speakers": len(result.speech_seconds), "tracks": len(result.tracks)},
    )
    return result
