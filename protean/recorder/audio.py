"""Microphone recorder with energy-based VAD and ASR.

Runs in a background thread alongside ``InputMonitor``. The PortAudio input
stream feeds 16 kHz mono PCM into a small ring; a simple RMS-energy VAD
slices it into utterances. Each utterance is:

  1. Written to ``audio/<NNNN>.wav`` under the recording dir.
  2. Sent to ``transcribe()`` (OpenAI-compatible /v1/audio/transcriptions
     endpoint configured via ``PROTEAN_ASR_URL``; transcript is empty if
     the env var is unset).
  3. Emitted as an ``InputEvent`` of type ``SPEECH`` via the same callback
     ``RecordingSession`` uses for keyboard / mouse events, so speech lands
     in ``events.json`` interleaved with everything else by ``timestamp``.

The recorder is best-effort: if no input device is available or PortAudio
fails, it logs a warning and stays silent rather than crashing the recording.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
import wave
from collections.abc import Callable
from pathlib import Path

import numpy as np
from openai import OpenAI

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

# ── ASR backend ──────────────────────────────────────────────
# OpenAI-compatible ``/v1/audio/transcriptions`` endpoint. Set via env var
# ``PROTEAN_ASR_URL`` (full URL including ``/v1/audio/transcriptions``).
# No default — if unset, speech events are emitted without transcripts.
_ASR_TIMEOUT_S = 30.0
_ASR_MODEL = os.environ.get("PROTEAN_ASR_MODEL", "whisper-1")


def transcribe(wav_path: Path) -> str:
    """Transcribe a WAV via the OpenAI-compatible ASR endpoint.

    Reads ``PROTEAN_ASR_URL`` (full transcriptions URL); the OpenAI SDK
    needs the base ending in ``/v1``, so we strip the trailing path.
    Returns the empty string on any failure (and when the env var is
    unset) so the SPEECH event is still emitted with audio_path/duration.
    """
    url = os.environ.get("PROTEAN_ASR_URL", "").strip()
    if not url:
        return ""

    base_url = url.rsplit("/audio/transcriptions", 1)[0]
    api_key = os.environ.get("PROTEAN_ASR_API_KEY") or "none"

    try:
        client = OpenAI(base_url=base_url, api_key=api_key, timeout=_ASR_TIMEOUT_S)
        with open(wav_path, "rb") as f:
            result = client.audio.transcriptions.create(
                file=f,
                model=_ASR_MODEL,
                response_format="text",
            )
    except Exception as e:
        log.warning("ASR failed for %s: %s", wav_path.name, e)
        return ""

    text = str(result).strip()
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
