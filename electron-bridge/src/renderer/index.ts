/**
 * Renderer entry — runs in the hidden BrowserWindow.
 *
 * Owns:
 *   - Mic capture via AudioWorklet → 16 kHz mono int16 LE PCM → IPC to main
 *   - Speaker playback of 24 kHz mono int16 LE PCM coming from main
 *   - Screen capture via getDisplayMedia → JPEG @ ≤1280px → IPC to main
 *
 * Uses `window.proteanIPC` (set up by preload.cts).
 */

declare global {
  interface Window {
    proteanIPC: {
      publish: {
        micPcm: (buf: ArrayBuffer) => void;
        micError: (msg: string) => void;
        micStarted: (info: { sampleRate: number }) => void;
        micStopped: () => void;
        screenFrame: (frame: { jpegB64: string; w: number; h: number; ts: number }) => void;
        screenError: (msg: string) => void;
        screenStarted: (info: { sourceLabel: string }) => void;
        screenStopped: () => void;
        rendererReady: () => void;
      };
      subscribe: {
        startMic: (h: (p: null) => void) => () => void;
        stopMic: (h: (p: null) => void) => () => void;
        playPcm: (h: (p: ArrayBuffer) => void) => () => void;
        clearPlayback: (h: (p: null) => void) => () => void;
        startScreen: (h: (p: { fps: number; maxDim: number; quality: number }) => void) => () => void;
        stopScreen: (h: (p: null) => void) => () => void;
        requestScreenshot: (h: (p: null) => void) => () => void;
      };
    };
  }
}

const ipc = window.proteanIPC;

// ─────────────────────────────────────────────────────────────────────────
// Mic capture
// ─────────────────────────────────────────────────────────────────────────

const MIC_TARGET_RATE = 16000;

const WORKLET_SRC = `
class PCMCaptureProcessor extends AudioWorkletProcessor {
  process(inputs) {
    const input = inputs[0];
    if (!input || input.length === 0) return true;
    const ch = input[0];
    if (!ch || ch.length === 0) return true;
    this.port.postMessage(ch.slice(0));
    return true;
  }
}
registerProcessor('pcm-capture', PCMCaptureProcessor);
`;

class MicSession {
  private ctx: AudioContext | null = null;
  private stream: MediaStream | null = null;
  private node: AudioWorkletNode | null = null;
  private srcNode: MediaStreamAudioSourceNode | null = null;
  private deviceRate = 48000;

  async start(): Promise<void> {
    if (this.ctx) return;
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 },
      video: false,
    });
    this.stream = stream;
    const ctx = new AudioContext({ sampleRate: 48000 });
    this.ctx = ctx;
    this.deviceRate = ctx.sampleRate;

    const url = URL.createObjectURL(
      new Blob([WORKLET_SRC], { type: 'application/javascript' }),
    );
    await ctx.audioWorklet.addModule(url);
    URL.revokeObjectURL(url);

    const src = ctx.createMediaStreamSource(stream);
    this.srcNode = src;
    const node = new AudioWorkletNode(ctx, 'pcm-capture');
    this.node = node;
    node.port.onmessage = (ev) => this.onChunk(ev.data as Float32Array);
    src.connect(node);
    // Don't connect to destination — would feed our own audio back into
    // the playback path (echo).
    ipc.publish.micStarted({ sampleRate: this.deviceRate });
  }

  async stop(): Promise<void> {
    if (!this.ctx) return;
    try {
      this.node?.disconnect();
      this.srcNode?.disconnect();
      this.stream?.getTracks().forEach((t) => t.stop());
      await this.ctx.close();
    } catch (err) {
      ipc.publish.micError(String(err));
    }
    this.ctx = null;
    this.stream = null;
    this.node = null;
    this.srcNode = null;
    ipc.publish.micStopped();
  }

  private onChunk(float32: Float32Array): void {
    const resampled = resampleFloat32(float32, this.deviceRate, MIC_TARGET_RATE);
    const int16 = floatToInt16(resampled);
    // .buffer can be ArrayBufferLike (SharedArrayBuffer); copy out into a
    // plain ArrayBuffer for the IPC structured-clone path.
    const ab = new ArrayBuffer(int16.byteLength);
    new Uint8Array(ab).set(new Uint8Array(int16.buffer, int16.byteOffset, int16.byteLength));
    ipc.publish.micPcm(ab);
  }
}

const mic = new MicSession();
ipc.subscribe.startMic(() => {
  mic.start().catch((err) => ipc.publish.micError(String(err)));
});
ipc.subscribe.stopMic(() => {
  mic.stop().catch((err) => ipc.publish.micError(String(err)));
});

// ─────────────────────────────────────────────────────────────────────────
// Playback
// ─────────────────────────────────────────────────────────────────────────

const PLAYBACK_RATE = 24000;

class PlaybackSession {
  private ctx: AudioContext | null = null;
  private nextStart = 0;

  ensureCtx(): AudioContext {
    if (!this.ctx) {
      this.ctx = new AudioContext({ sampleRate: PLAYBACK_RATE });
    }
    return this.ctx;
  }

  enqueue(int16Buf: ArrayBuffer): void {
    const ctx = this.ensureCtx();
    const view = new Int16Array(int16Buf);
    if (view.length === 0) return;
    const float32 = int16ToFloat32(view);
    const buffer = ctx.createBuffer(1, float32.length, PLAYBACK_RATE);
    // copyToChannel needs a Float32Array backed by a real ArrayBuffer.
    buffer.copyToChannel(new Float32Array(float32), 0);

    const src = ctx.createBufferSource();
    src.buffer = buffer;
    src.connect(ctx.destination);
    const startAt = Math.max(ctx.currentTime, this.nextStart);
    src.start(startAt);
    this.nextStart = startAt + buffer.duration;
  }

  clear(): void {
    if (!this.ctx) return;
    this.nextStart = this.ctx.currentTime;
  }
}

const playback = new PlaybackSession();
ipc.subscribe.playPcm((buf) => playback.enqueue(buf));
ipc.subscribe.clearPlayback(() => playback.clear());

// ─────────────────────────────────────────────────────────────────────────
// Screen capture
// ─────────────────────────────────────────────────────────────────────────

class ScreenSession {
  private stream: MediaStream | null = null;
  private timer: number | null = null;
  private fps = 1;
  private maxDim = 1280;
  private quality = 60;
  private video: HTMLVideoElement | null = null;
  private canvas: HTMLCanvasElement | null = null;

  async start(opts: { fps: number; maxDim: number; quality: number }): Promise<void> {
    if (this.stream) await this.stop();
    this.fps = Math.max(0.25, Math.min(30, opts.fps));
    this.maxDim = Math.max(320, Math.min(3840, opts.maxDim));
    this.quality = Math.max(10, Math.min(95, opts.quality));

    const stream = await navigator.mediaDevices.getDisplayMedia({
      video: { frameRate: this.fps },
      audio: false,
    });
    this.stream = stream;

    const video = document.createElement('video');
    video.srcObject = stream;
    video.muted = true;
    video.playsInline = true;
    await video.play();
    this.video = video;

    this.canvas = document.createElement('canvas');
    ipc.publish.screenStarted({
      sourceLabel: stream.getVideoTracks()[0]?.label ?? 'unknown',
    });
    this.timer = window.setInterval(() => this.captureOne(), Math.round(1000 / this.fps));
  }

  async stop(): Promise<void> {
    if (this.timer !== null) {
      window.clearInterval(this.timer);
      this.timer = null;
    }
    this.stream?.getTracks().forEach((t) => t.stop());
    this.stream = null;
    this.video?.remove();
    this.video = null;
    this.canvas = null;
    ipc.publish.screenStopped();
  }

  async snapshot(): Promise<void> {
    if (!this.video) return;
    await this.captureOne();
  }

  private async captureOne(): Promise<void> {
    const video = this.video;
    const canvas = this.canvas;
    if (!video || !canvas) return;
    const vw = video.videoWidth || 0;
    const vh = video.videoHeight || 0;
    if (vw === 0 || vh === 0) return;
    const scale = Math.min(1, this.maxDim / Math.max(vw, vh));
    const cw = Math.round(vw * scale);
    const ch = Math.round(vh * scale);
    canvas.width = cw;
    canvas.height = ch;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    ctx.drawImage(video, 0, 0, cw, ch);
    const blob: Blob | null = await new Promise((resolve) =>
      canvas.toBlob((b) => resolve(b), 'image/jpeg', this.quality / 100),
    );
    if (!blob) return;
    const buf = await blob.arrayBuffer();
    const jpegB64 = arrayBufferToBase64(buf);
    ipc.publish.screenFrame({ jpegB64, w: cw, h: ch, ts: performance.now() });
  }
}

const screenSession = new ScreenSession();
ipc.subscribe.startScreen((opts) => {
  screenSession.start(opts).catch((err) => ipc.publish.screenError(String(err)));
});
ipc.subscribe.stopScreen(() => {
  screenSession.stop().catch((err) => ipc.publish.screenError(String(err)));
});
ipc.subscribe.requestScreenshot(() => {
  screenSession.snapshot().catch((err) => ipc.publish.screenError(String(err)));
});

ipc.publish.rendererReady();

// ─────────────────────────────────────────────────────────────────────────
// Helpers
// ─────────────────────────────────────────────────────────────────────────

function floatToInt16(input: Float32Array): Int16Array {
  const out = new Int16Array(input.length);
  for (let i = 0; i < input.length; i += 1) {
    const x = Math.max(-1, Math.min(1, input[i] ?? 0));
    out[i] = x < 0 ? Math.round(x * 0x8000) : Math.round(x * 0x7fff);
  }
  return out;
}

function int16ToFloat32(input: Int16Array): Float32Array {
  const out = new Float32Array(input.length);
  for (let i = 0; i < input.length; i += 1) {
    const x = input[i] ?? 0;
    out[i] = x < 0 ? x / 0x8000 : x / 0x7fff;
  }
  return out;
}

function resampleFloat32(input: Float32Array, fromRate: number, toRate: number): Float32Array {
  if (fromRate === toRate) return input;
  const ratio = toRate / fromRate;
  const newLen = Math.max(1, Math.round(input.length * ratio));
  const out = new Float32Array(newLen);
  for (let i = 0; i < newLen; i += 1) {
    const t = (i / (newLen - 1 || 1)) * (input.length - 1);
    const i0 = Math.floor(t);
    const i1 = Math.min(input.length - 1, i0 + 1);
    const frac = t - i0;
    const a = input[i0] ?? 0;
    const b = input[i1] ?? 0;
    out[i] = a + (b - a) * frac;
  }
  return out;
}

function arrayBufferToBase64(buf: ArrayBuffer): string {
  const bytes = new Uint8Array(buf);
  let s = '';
  const CHUNK = 0x8000;
  for (let i = 0; i < bytes.length; i += CHUNK) {
    s += String.fromCharCode(...bytes.subarray(i, i + CHUNK));
  }
  return btoa(s);
}

export {};
