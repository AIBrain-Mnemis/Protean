/**
 * MicCaptureController — owns the renderer's mic stream from the main side.
 *
 * Receives 16 kHz mono int16 LE PCM chunks via IPC and forwards them to a
 * sink (typically the realtime provider). Tells the renderer when to
 * start/stop capture.
 */

import { ipcMain, type BrowserWindow } from 'electron';
import { IPC } from '../ipc.js';

export type PcmSink = (pcm: Buffer) => void;

export class MicCaptureController {
  private active = false;
  private boundIpc: Array<{ channel: string; fn: (...args: unknown[]) => void }> = [];

  constructor(
    private readonly win: BrowserWindow,
    private sink: PcmSink,
  ) {
    this.bind();
  }

  setSink(sink: PcmSink): void {
    this.sink = sink;
  }

  start(): void {
    if (this.active) return;
    this.active = true;
    this.send(IPC.startMic, null);
  }

  stop(): void {
    if (!this.active) return;
    this.active = false;
    this.send(IPC.stopMic, null);
  }

  dispose(): void {
    this.stop();
    for (const { channel, fn } of this.boundIpc) {
      ipcMain.removeListener(channel, fn);
    }
    this.boundIpc = [];
  }

  private bind(): void {
    const handlePcm = (_event: unknown, raw: ArrayBuffer): void => {
      if (!this.active) return;
      try {
        this.sink(Buffer.from(raw));
      } catch (err) {
        process.stderr.write(`[bridge] mic sink error: ${String(err)}\n`);
      }
    };
    const handleError = (_event: unknown, msg: string): void => {
      process.stderr.write(`[bridge] mic error: ${String(msg)}\n`);
    };
    const handleStarted = (_event: unknown, info: unknown): void => {
      process.stderr.write(`[bridge] mic started: ${JSON.stringify(info)}\n`);
    };
    const handleStopped = (_event: unknown): void => {
      process.stderr.write('[bridge] mic stopped\n');
    };

    ipcMain.on(IPC.micPcm, handlePcm as (...args: unknown[]) => void);
    ipcMain.on(IPC.micError, handleError as (...args: unknown[]) => void);
    ipcMain.on(IPC.micStarted, handleStarted as (...args: unknown[]) => void);
    ipcMain.on(IPC.micStopped, handleStopped as (...args: unknown[]) => void);

    this.boundIpc = [
      { channel: IPC.micPcm, fn: handlePcm as (...args: unknown[]) => void },
      { channel: IPC.micError, fn: handleError as (...args: unknown[]) => void },
      { channel: IPC.micStarted, fn: handleStarted as (...args: unknown[]) => void },
      { channel: IPC.micStopped, fn: handleStopped as (...args: unknown[]) => void },
    ];
  }

  private send(channel: string, payload: unknown): void {
    if (this.win.isDestroyed()) return;
    this.win.webContents.send(channel, payload);
  }
}
