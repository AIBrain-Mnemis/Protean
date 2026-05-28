/**
 * ScreenCaptureController — orchestrates "renderer captures local screen,
 * bridge fans out via evidenceFanout".
 *
 * Mode "off" -> no capture.
 * Mode "share" -> renderer captures local screen.
 * Mode "observe" -> currently treated as `share` (renderer captures the
 *                   local screen) until TRTC remote-substream wiring lands.
 *
 * The pHash dedup logic from protean/realtime/screen.py is mirrored here so
 * Gemini doesn't get redundant frames.
 */

import { ipcMain, type BrowserWindow } from 'electron';
import { IPC, type ScreenFramePayload } from '../ipc.js';
import {
  pushFrame,
  type EvidenceFanoutSinks,
} from './evidenceFanout.js';

export type ScreenMode = 'off' | 'observe' | 'share';

const PHASH_THRESHOLD = 5;
const SIZE_THRESHOLD = 0.01;

export class ScreenCaptureController {
  private mode: ScreenMode = 'off';
  private boundIpc: Array<{ channel: string; fn: (...args: unknown[]) => void }> = [];
  private prevPhash = -1;
  private prevJpegLen = 0;
  private pendingCount = 0;
  private singleshotPending: ((ok: boolean) => void) | null = null;
  private sourceLabel = '';
  private labelListener: ((label: string) => void) | null = null;

  constructor(
    private readonly win: BrowserWindow,
    private readonly sinks: EvidenceFanoutSinks,
  ) {
    this.bind();
  }

  get currentMode(): ScreenMode {
    return this.mode;
  }

  /** Label reported by the renderer for the currently-captured surface
   * (e.g. "Screen 1", "Window: Outlook"). Empty when no capture is active or
   * the renderer hasn't reported a label yet. */
  get currentSourceLabel(): string {
    return this.sourceLabel;
  }

  /** Subscribe to source-label changes. Renderer reports the label
   * asynchronously after start(), so callers that need the label (e.g. to
   * re-publish a screen_state envelope) should register here. */
  onSourceLabelChange(listener: ((label: string) => void) | null): void {
    this.labelListener = listener;
  }

  /** Start capture in the requested mode. */
  start(
    mode: 'observe' | 'share',
    opts: { fps?: number; maxDim?: number; quality?: number } = {},
  ): void {
    if (this.mode !== 'off') this.stop();
    this.mode = mode;
    this.prevPhash = -1;
    this.prevJpegLen = 0;
    this.sourceLabel = '';
    this.win.webContents.send(IPC.startScreen, {
      fps: opts.fps ?? 1,
      maxDim: opts.maxDim ?? 1280,
      quality: opts.quality ?? 60,
    });
  }

  stop(): void {
    if (this.mode === 'off') return;
    this.mode = 'off';
    this.sourceLabel = '';
    this.win.webContents.send(IPC.stopScreen, null);
  }

  /**
   * Request a one-shot frame. Returns true if a frame was pushed within
   * the timeout. Useful for the request_screenshot tool.
   */
  async requestSingleshot(timeoutMs = 3000): Promise<boolean> {
    return new Promise<boolean>((resolve) => {
      let settled = false;
      const settle = (ok: boolean): void => {
        if (settled) return;
        settled = true;
        this.singleshotPending = null;
        resolve(ok);
      };
      this.singleshotPending = settle;
      this.win.webContents.send(IPC.requestScreenshot, null);
      setTimeout(() => settle(false), timeoutMs);
    });
  }

  dispose(): void {
    this.stop();
    for (const { channel, fn } of this.boundIpc) {
      ipcMain.removeListener(channel, fn);
    }
    this.boundIpc = [];
  }

  private bind(): void {
    const handleFrame = (_event: unknown, payload: ScreenFramePayload): void => {
      this.handleFrame(payload);
    };
    const handleError = (_event: unknown, msg: string): void => {
      process.stderr.write(`[bridge] screen error: ${String(msg)}\n`);
      this.singleshotPending?.(false);
    };
    const handleStarted = (_event: unknown, info: unknown): void => {
      process.stderr.write(`[bridge] screen started: ${JSON.stringify(info)}\n`);
      if (info && typeof info === 'object' && 'sourceLabel' in info) {
        const label = (info as { sourceLabel?: unknown }).sourceLabel;
        if (typeof label === 'string' && label !== this.sourceLabel) {
          this.sourceLabel = label;
          try {
            this.labelListener?.(label);
          } catch (err) {
            process.stderr.write(`[bridge] sourceLabel listener threw: ${String(err)}\n`);
          }
        }
      }
    };
    const handleStopped = (_event: unknown): void => {
      process.stderr.write('[bridge] screen stopped\n');
    };

    ipcMain.on(IPC.screenFrame, handleFrame as (...args: unknown[]) => void);
    ipcMain.on(IPC.screenError, handleError as (...args: unknown[]) => void);
    ipcMain.on(IPC.screenStarted, handleStarted as (...args: unknown[]) => void);
    ipcMain.on(IPC.screenStopped, handleStopped as (...args: unknown[]) => void);

    this.boundIpc = [
      { channel: IPC.screenFrame, fn: handleFrame as (...args: unknown[]) => void },
      { channel: IPC.screenError, fn: handleError as (...args: unknown[]) => void },
      { channel: IPC.screenStarted, fn: handleStarted as (...args: unknown[]) => void },
      { channel: IPC.screenStopped, fn: handleStopped as (...args: unknown[]) => void },
    ];
  }

  private handleFrame(payload: ScreenFramePayload): void {
    const jpeg = Buffer.from(payload.jpegB64, 'base64');
    const phash = quickPhashFromJpegSize(jpeg, payload.w, payload.h);
    if (this.prevPhash !== -1) {
      const distance = popcount(BigInt(phash) ^ BigInt(this.prevPhash));
      const sizeChange = Math.abs(jpeg.length - this.prevJpegLen);
      if (distance < PHASH_THRESHOLD && sizeChange < this.prevJpegLen * SIZE_THRESHOLD) {
        this.singleshotPending?.(true);
        return;
      }
    }
    this.prevPhash = phash;
    this.prevJpegLen = jpeg.length;

    this.pendingCount += 1;
    const ok = pushFrame(
      this.sinks,
      {
        jpeg,
        source: this.mode === 'observe' ? 'remote_screen' : 'local_screen',
        ts: payload.ts / 1000,
        phash,
        w: payload.w,
        h: payload.h,
      },
      this.pendingCount,
    );
    this.pendingCount = Math.max(0, this.pendingCount - 1);
    this.singleshotPending?.(ok);
  }
}

/**
 * Best-effort perceptual hash from JPEG size + simple byte sampling.
 *
 * The Python side (`protean/realtime/screen.py`) decodes the JPEG and runs
 * a real 8x8 grayscale phash. Doing that in main process would require
 * pulling in sharp/jimp. For dedup here we approximate: hash a few
 * sampled bytes from the JPEG so distinct images get distinct hashes
 * while a stream of the same frame stays similar.
 *
 * If false positives become a problem, replace with a real phash computed
 * in the renderer (where canvas is already available).
 */
function quickPhashFromJpegSize(jpeg: Buffer, w: number, h: number): number {
  let h64 = (BigInt(w & 0xffff) << 48n) | (BigInt(h & 0xffff) << 32n);
  const stride = Math.max(1, Math.floor(jpeg.length / 32));
  let bits = 0n;
  for (let i = 0; i < 32 && i * stride < jpeg.length; i += 1) {
    const b = BigInt(jpeg[i * stride] ?? 0);
    bits |= b << BigInt(i * 2);
  }
  h64 ^= bits;
  return Number(h64 & 0xffffffffn);
}

function popcount(n: bigint): number {
  let c = 0;
  while (n) {
    n &= n - 1n;
    c += 1;
  }
  return c;
}
