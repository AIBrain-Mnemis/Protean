"""Microphone recorder with energy-based VAD and (mock) ASR.

Runs in a background thread alongside ``InputMonitor``. The PortAudio input
stream feeds 16 kHz mono PCM into a small ring; a simple RMS-energy VAD
slices it into utterances. Each utterance is:

  1. Written to ``audio/<NNNN>.wav`` under the recording dir.
  2. Sent to ``transcribe()`` (mock — returns a placeholder string).
  3. Emitted as an ``InputEvent`` of type ``SPEECH`` via the same callback
     ``RecordingSession`` uses for keyboard / mouse events, so speech lands
     in ``events.json`` interleaved with everything else by ``timestamp``.

The recorder is best-effort: if no input device is available or PortAudio
fails, it logs a warning and stays silent rather than crashing the recording.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
import uuid
import wave
from collections.abc import Callable
from pathlib import Path
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

import numpy as np

from protean.recorder.events import EventType, InputEvent

log = logging.getLogger(__name__)

# Audio capture format
SAMPLE_RATE = 16_000
CHANNELS = 1
DTYPE = "int16"
BLOCK_MS = 30  # PortAudio callback granularity
BLOCK_SAMPLES = SAMPLE_RATE * BLOCK_MS // 1000

# VAD tuning
_RMS_START_THRESHOLD = 500   # int16 RMS that opens an utterance
_RMS_STOP_THRESHOLD = 300    # falling below this for _SILENCE_HANG_MS closes it
_SILENCE_HANG_MS = 600
_MIN_UTTERANCE_MS = 250
_MAX_UTTERANCE_MS = 30_000
# Pre-roll: keep this many ms before VAD trigger so we don't clip word onsets
_PREROLL_MS = 200

# ── ASR backend ────────────────────────────────────────────────────────────
# OpenAI-compatible /v1/audio/transcriptions endpoint. Override via env var
# PROTEAN_ASR_URL when the service moves. Empty string → fall back to mock.
DEFAULT_ASR_URL = "http://10.224.120.166:8000/v1/audio/transcriptions"
_ASR_TIMEOUT_S = 30.0


def _build_multipart(wav_path: Path, fields: dict[str, str]) -> tuple[bytes, str]:
    """Build a multipart/form-data body for the ASR request.

    Returns (body, content_type). Pure stdlib so we don't pull in `requests`
    just for one upload.
    """
    boundary = f"----proteanboundary{uuid.uuid4().hex}"
    crlf = b"\r\n"
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(f"--{boundary}".encode())
        parts.append(
            f'Content-Disposition: form-data; name="{name}"'.encode()
        )
        parts.append(b"")
        parts.append(value.encode("utf-8"))
    # File part
    parts.append(f"--{boundary}".encode())
    parts.append(
        (
            f'Content-Disposition: form-data; name="file"; '
            f'filename="{wav_path.name}"'
        ).encode()
    )
    parts.append(b"Content-Type: audio/wav")
    parts.append(b"")
    parts.append(wav_path.read_bytes())
    parts.append(f"--{boundary}--".encode())
    parts.append(b"")
    body = crlf.join(parts)
    return body, f"multipart/form-data; boundary={boundary}"


def _parse_asr_response(raw: bytes) -> str:
    """Pull the transcript text out of the ASR JSON response.

    OpenAI-compatible endpoints return ``{"text": "..."}``. We also accept a
    couple of common alternates so we don't break on a slightly different
    server.  As a last resort we return the raw body decoded as UTF-8.
    """
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except Exception:
        return raw.decode("utf-8", errors="replace").strip()
    if isinstance(data, dict):
        for key in ("text", "transcript", "transcription"):
            v = data.get(key)
            if isinstance(v, str):
                return v.strip()
        # Some servers nest the result.
        results = data.get("results")
        if isinstance(results, list) and results:
            first = results[0]
            if isinstance(first, dict) and isinstance(first.get("text"), str):
                return first["text"].strip()
    return ""


def transcribe(wav_path: Path) -> str:
    """Send the WAV to the ASR HTTP endpoint and return the transcript text.

    Endpoint: ``POST {PROTEAN_ASR_URL or DEFAULT_ASR_URL}`` as
    ``multipart/form-data`` with fields ``file`` (the WAV) and
    ``timestamps=false``. Mirrors:

        curl -F 'file=@x.wav' -F timestamps=false \\
             http://10.224.120.166:8000/v1/audio/transcriptions

    Returns the empty string (and logs a warning) on any network/parse
    failure so the SPEECH event is still emitted with audio_path/duration
    even when ASR is unreachable.
    """
    url = os.environ.get("PROTEAN_ASR_URL", DEFAULT_ASR_URL)
    if not url:
        return f"[mock transcript for {wav_path.name}]"

    try:
        body, content_type = _build_multipart(wav_path, {"timestamps": "false"})
    except Exception as e:
        log.warning("ASR: failed to build multipart body for %s: %s", wav_path, e)
        return ""

    req = urlrequest.Request(url, data=body, method="POST")
    req.add_header("Content-Type", content_type)
    req.add_header("Content-Length", str(len(body)))
    req.add_header("Accept", "application/json")

    try:
        with urlrequest.urlopen(req, timeout=_ASR_TIMEOUT_S) as resp:
            raw = resp.read()
    except HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        log.warning("ASR HTTP %s for %s: %s", e.code, wav_path.name, detail)
        return ""
    except (URLError, TimeoutError) as e:
        log.warning("ASR network error for %s: %s", wav_path.name, e)
        return ""
    except Exception as e:
        log.warning("ASR unexpected error for %s: %s", wav_path.name, e)
        return ""

    text = _parse_asr_response(raw)
    if not text:
        log.warning("ASR returned empty transcript for %s", wav_path.name)
    return text


class AudioRecorder:
    """Continuous mic capture + RMS VAD + utterance emission."""

    def __init__(
        self,
        on_event: Callable[[InputEvent], None],
        out_dir: Path,
        *,
        sample_rate: int = SAMPLE_RATE,
        device: int | str | None = None,
    ) -> None:
        self._on_event = on_event
        self._out_dir = out_dir
        self._sample_rate = sample_rate
        self._device = device
        self._start_time: float = 0
        self._running = False
        self._stream = None  # sounddevice.InputStream
        self._counter = 0
        self._counter_lock = threading.Lock()

        # VAD state (mutated only inside the audio callback thread)
        preroll_blocks = max(1, _PREROLL_MS // BLOCK_MS)
        self._preroll: list[np.ndarray] = []
        self._preroll_max = preroll_blocks
        self._in_speech = False
        self._utt_blocks: list[np.ndarray] = []
        self._utt_started_at: float = 0.0  # session-relative timestamp
        self._silence_run_ms = 0

        # Worker thread: drains finalised utterances, writes WAV, calls ASR,
        # emits SPEECH event. Keeps the PortAudio callback non-blocking.
        self._asr_queue: queue.Queue = queue.Queue()
        self._asr_worker: threading.Thread | None = None

        self._out_dir.mkdir(parents=True, exist_ok=True)

    # ── lifecycle ────────────────────────────────────────

    def start(self, start_time: float) -> None:
        self._start_time = start_time
        try:
            import sounddevice as sd
        except Exception as e:  # pragma: no cover - depends on env
            log.warning("sounddevice unavailable, audio recording disabled: %s", e)
            return

        try:
            self._stream = sd.InputStream(
                samplerate=self._sample_rate,
                channels=CHANNELS,
                dtype=DTYPE,
                blocksize=BLOCK_SAMPLES,
                device=self._device,
                callback=self._audio_callback,
            )
            self._stream.start()
        except Exception as e:
            log.warning("failed to open mic input stream, audio disabled: %s", e)
            self._stream = None
            return

        self._running = True
        self._asr_worker = threading.Thread(
            target=self._asr_loop, daemon=True, name="asr-worker"
        )
        self._asr_worker.start()
        log.info("audio recorder started (sr=%d, device=%s)", self._sample_rate, self._device)

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        try:
            if self._stream is not None:
                self._stream.stop()
                self._stream.close()
        except Exception as e:
            log.warning("error closing mic stream: %s", e)
        finally:
            self._stream = None

        # Flush any in-progress utterance.
        if self._in_speech and self._utt_blocks:
            self._finalize_utterance()

        # Drain the ASR worker so all SPEECH events are delivered before we
        # return — RecordingSession.stop() relies on this to write a complete
        # events.json.
        self._asr_queue.put(None)
        if self._asr_worker is not None and self._asr_worker.is_alive():
            self._asr_worker.join(timeout=_ASR_TIMEOUT_S + 5)

    # ── audio callback ───────────────────────────────────

    def _audio_callback(self, indata, frames, time_info, status) -> None:  # noqa: ANN001
        if status:
            # Overflows etc. — log once-ish and keep going.
            log.debug("audio status: %s", status)
        if not self._running:
            return
        # indata shape: (frames, channels) int16
        block = indata[:, 0].copy() if indata.ndim == 2 else indata.copy()
        rms = float(np.sqrt(np.mean(block.astype(np.float32) ** 2)))

        if not self._in_speech:
            # Maintain rolling pre-roll so we don't chop off word onsets.
            self._preroll.append(block)
            if len(self._preroll) > self._preroll_max:
                self._preroll.pop(0)

            if rms >= _RMS_START_THRESHOLD:
                self._in_speech = True
                self._utt_blocks = list(self._preroll)
                self._utt_blocks.append(block)
                self._preroll.clear()
                self._silence_run_ms = 0
                # Approximate utterance start from when preroll began.
                preroll_offset = (len(self._utt_blocks) - 1) * BLOCK_MS / 1000.0
                self._utt_started_at = (
                    time.monotonic() - self._start_time - preroll_offset
                )
            return

        # Inside an utterance: keep accumulating.
        self._utt_blocks.append(block)
        if rms < _RMS_STOP_THRESHOLD:
            self._silence_run_ms += BLOCK_MS
        else:
            self._silence_run_ms = 0

        duration_ms = len(self._utt_blocks) * BLOCK_MS
        if (
            self._silence_run_ms >= _SILENCE_HANG_MS
            or duration_ms >= _MAX_UTTERANCE_MS
        ):
            self._finalize_utterance()

    # ── utterance finalisation ───────────────────────────

    def _finalize_utterance(self) -> None:
        """Hand the buffered utterance off to the worker thread.

        Runs on the PortAudio callback thread, so it MUST stay non-blocking —
        no disk IO and no HTTP. The worker concatenates, writes the WAV,
        calls ASR, and emits the SPEECH event.
        """
        blocks = self._utt_blocks
        started_at = self._utt_started_at
        self._utt_blocks = []
        self._in_speech = False
        self._silence_run_ms = 0

        duration_ms = len(blocks) * BLOCK_MS
        if duration_ms < _MIN_UTTERANCE_MS or not blocks:
            return

        with self._counter_lock:
            self._counter += 1
            idx = self._counter

        self._asr_queue.put((idx, blocks, started_at, duration_ms))

    def _asr_loop(self) -> None:
        """Background worker: WAV write → ASR call → SPEECH event emit."""
        while True:
            item = self._asr_queue.get()
            if item is None:
                break
            idx, blocks, started_at, duration_ms = item

            rel_path = f"audio/{idx:04d}.wav"
            abs_path = self._out_dir / f"{idx:04d}.wav"

            try:
                audio = np.concatenate(blocks).astype(np.int16)
                with wave.open(str(abs_path), "wb") as wf:
                    wf.setnchannels(CHANNELS)
                    wf.setsampwidth(2)  # int16
                    wf.setframerate(self._sample_rate)
                    wf.writeframes(audio.tobytes())
            except Exception as e:
                log.warning("failed to write utterance wav: %s", e)
                continue

            try:
                text = transcribe(abs_path)
            except Exception as e:
                log.warning("transcribe failed: %s", e)
                text = ""

            event = InputEvent(
                timestamp=max(0.0, started_at),
                event_type=EventType.SPEECH,
                audio_path=rel_path,
                audio_duration=duration_ms / 1000.0,
                transcript=text,
            )
            try:
                self._on_event(event)
            except Exception as e:
                log.warning("on_event for SPEECH failed: %s", e)
