/**
 * AudioPlaybackController — pushes assistant PCM (24 kHz mono int16) into
 * the renderer for playback through the system speaker.
 */

import type { BrowserWindow } from 'electron';
import { IPC } from '../ipc.js';

export class AudioPlaybackController {
  constructor(private readonly win: BrowserWindow) {}

  /** Play one chunk of 24 kHz mono int16 LE PCM. */
  play(pcm: Buffer): void {
    if (this.win.isDestroyed()) return;
    // Copy underlying ArrayBuffer slice; Buffer's buffer can be larger than the view.
    const ab = pcm.buffer.slice(
      pcm.byteOffset,
      pcm.byteOffset + pcm.byteLength,
    );
    this.win.webContents.send(IPC.playPcm, ab);
  }

  /** Drop any queued audio (e.g. on barge-in). */
  clear(): void {
    if (this.win.isDestroyed()) return;
    this.win.webContents.send(IPC.clearPlayback, null);
  }
}
