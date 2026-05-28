/**
 * Main-process driver for the renderer-owned TRTCCloud instance.
 *
 * State machine (per the new TRTC contract):
 *
 *     ┌──────────┐  connect(asgmt)         ┌──────────┐
 *     │   IDLE   │ ───────────────────▶    │ CONNECT  │
 *     └──────────┘                         └────┬─────┘
 *           ▲                                   │
 *           │                                   │ onExitRoom (any reason)
 *           │ connect(asgmt) with NEW roomId    ▼
 *           │                              ┌─────────────┐
 *           └─────────────────────────────│ DISCONNECT  │
 *                                          └─────────────┘
 *
 * Rules:
 *   - connect(): if state === CONNECT or already connected to the same
 *     roomId, no-op. Otherwise: flip to CONNECT and ask preload to enter
 *     the room with the assignment.
 *   - On onExitRoom (network drop, kick, room dismissed, any reason):
 *     flip to DISCONNECT. We do NOT auto-rejoin.
 *   - The next heartbeat carrying a *new* RESERVED roomId will call
 *     connect() again, which is allowed because state is DISCONNECT.
 *     The same roomId is suppressed (we already tried that one).
 *
 * Audio bridge (Cut 2) is unchanged: remote PCM listeners + sendPcm.
 */

import { ipcMain, type BrowserWindow } from 'electron';

import { IPC, TrtcRemotePcmPayload } from './ipc.js';
import type { Assignment } from './presence/index.js';

export type TrtcConnectState = 'IDLE' | 'CONNECT' | 'DISCONNECT';

export interface TrtcEnterRoomResult {
  /** SDK return: ≥0 = elapsed ms, <0 = error code. */
  result: number;
}

export interface TrtcError {
  code: number;
  message: string;
  extra?: string;
}

/** Subscriber for remote audio frames (Cut 2). */
export type RemotePcmListener = (frame: TrtcRemotePcmPayload) => void;

/**
 * Fires when the controller leaves a room (any reason). The orchestrator
 * uses this to emit `room_state(ended)` to Python.
 */
export type ExitListener = (info: { roomId: string | null; reason: number }) => void;

export class TrtcController {
  private state: TrtcConnectState = 'IDLE';
  /** Room we are currently CONNECTed to, or last DISCONNECTed from. */
  private currentRoomId: string | null = null;
  private listenersInstalled = false;
  private remotePcmListeners: RemotePcmListener[] = [];
  private exitListeners: ExitListener[] = [];
  private remotePcmCount = 0;
  private sendPcmCount = 0;
  // Paced TTS drainer state. TRTC's sendCustomAudioData mandates
  // approximately even inter-frame intervals; Gemini delivers in bursts.
  // We accumulate bytes here and ship one fixed-size frame every 20 ms.
  private sendPcmQueue: Buffer[] = [];
  private sendPcmQueuedBytes = 0;
  private sendPcmRate = 0;
  private sendPcmTimer: NodeJS.Timeout | null = null;
  private sendPcmFramesShipped = 0;

  constructor(private readonly window: BrowserWindow) {}

  // ── Lifecycle ──────────────────────────────────────────────────────────

  install(): void {
    if (this.listenersInstalled) return;
    this.listenersInstalled = true;

    ipcMain.on(IPC.trtcEntered, (_e, info: TrtcEnterRoomResult) => {
      const result = typeof info?.result === 'number' ? info.result : -1;
      process.stderr.write(`[bridge] trtc: onEnterRoom result=${result}\n`);
      if (result < 0) {
        // SDK refused to enter. Treat as a local exit so the orchestrator
        // can wind the call down and we accept the next assignment.
        this.transitionToDisconnect(result);
      }
    });

    ipcMain.on(IPC.trtcExited, (_e, info: { reason: number }) => {
      const reason = typeof info?.reason === 'number' ? info.reason : -1;
      process.stderr.write(`[bridge] trtc: onExitRoom reason=${reason}\n`);
      this.transitionToDisconnect(reason);
    });

    ipcMain.on(IPC.trtcError, (_e, err: TrtcError) => {
      process.stderr.write(
        `[bridge] trtc: onError code=${err?.code} msg=${err?.message}` +
          (err?.extra ? ` extra=${err.extra}` : '') +
          '\n',
      );
    });

    ipcMain.on(IPC.trtcRemotePcm, (_e, frame: TrtcRemotePcmPayload) => {
      this.remotePcmCount += 1;
      if (this.remotePcmCount === 1) {
        process.stderr.write(
          `[bridge] trtc: first remote-pcm rate=${frame.sampleRate} ch=${frame.channels} b64len=${frame.pcmB64.length}\n`,
        );
      } else if (this.remotePcmCount % 100 === 0) {
        process.stderr.write(
          `[bridge] trtc: remote-pcm count=${this.remotePcmCount}\n`,
        );
      }
      for (const cb of this.remotePcmListeners) {
        try {
          cb(frame);
        } catch (err) {
          process.stderr.write(
            `[bridge] trtc remote-pcm listener threw: ${String(err)}\n`,
          );
        }
      }
    });

    ipcMain.on(IPC.trtcScreenShareState, (_e, info: { state: string; detail?: string }) => {
      const detail = info?.detail ? ` detail=${info.detail}` : '';
      process.stderr.write(`[bridge] trtc: screen-share ${info?.state}${detail}\n`);
    });
  }

  uninstall(): void {
    ipcMain.removeAllListeners(IPC.trtcEntered);
    ipcMain.removeAllListeners(IPC.trtcExited);
    ipcMain.removeAllListeners(IPC.trtcError);
    ipcMain.removeAllListeners(IPC.trtcRemotePcm);
    ipcMain.removeAllListeners(IPC.trtcScreenShareState);
    this.listenersInstalled = false;
    this.remotePcmListeners = [];
    this.exitListeners = [];
    this.stopDrainer();
    this.sendPcmQueue = [];
    this.sendPcmQueuedBytes = 0;
  }

  // ── State machine ──────────────────────────────────────────────────────

  get connectState(): TrtcConnectState {
    return this.state;
  }

  get connectedRoomId(): string | null {
    return this.state === 'CONNECT' ? this.currentRoomId : null;
  }

  /**
   * Try to join the room described by `assignment`.
   *
   * No-op when state === CONNECT (we are already in a call). DISCONNECT
   * is permissive: per `docs/rtc-client-integration.md` §4.4 / §8, the
   * server flips BUSY → RESERVED → BUSY across network jitter while
   * keeping the same roomId, so we must accept the same-roomId rejoin.
   * Returns whether a connect attempt was actually started.
   */
  connect(assignment: Assignment): boolean {
    if (this.window.isDestroyed()) {
      process.stderr.write('[bridge] trtc.connect: renderer destroyed; ignoring\n');
      return false;
    }
    if (this.state === 'CONNECT') {
      // Already in a call. Same roomId or different — both ignored, per
      // the contract: only one active call at a time, no auto-switching.
      return false;
    }

    process.stderr.write(
      `[bridge] trtc.connect: ${this.state} -> CONNECT roomId=${assignment.roomId}` +
        (this.currentRoomId === assignment.roomId ? ' (rejoin after jitter)' : '') +
        '\n',
    );
    this.state = 'CONNECT';
    this.currentRoomId = assignment.roomId;

    this.window.webContents.send(IPC.trtcEnterRoom, {
      sdkAppId: assignment.sdkAppId,
      userId: assignment.userId,
      userSig: assignment.userSig,
      strRoomId: assignment.roomId,
    });
    return true;
  }

  /** Subscribe to "we left a room" events. */
  onExit(cb: ExitListener): () => void {
    this.exitListeners.push(cb);
    return () => {
      this.exitListeners = this.exitListeners.filter((x) => x !== cb);
    };
  }

  /**
   * Ask TRTC to leave the current room. Used when Python's TeachSession
   * winds down before TRTC has gotten a remote-leave / kick. Does NOT
   * wait — the actual teardown is signalled via onExit when the SDK
   * fires onExitRoom.
   */
  leave(): void {
    if (this.state !== 'CONNECT') return;
    if (this.window.isDestroyed()) return;
    process.stderr.write(`[bridge] trtc.leave: requesting exit roomId=${this.currentRoomId}\n`);
    this.window.webContents.send(IPC.trtcExitRoom, null);
  }

  /**
   * Start publishing the bot's screen on TRTC's substream so the caller's
   * TRTC client renders it. No-op if not connected. The 1-based
   * `displayIndex` (primary-first ordering, null = primary) is forwarded
   * to the renderer to pick the matching TRTC source. Calling this
   * repeatedly is safe — the renderer re-targets without restarting if
   * the share is already active.
   */
  startScreenShare(displayIndex: number | null = null): void {
    if (this.window.isDestroyed()) return;
    if (this.state !== 'CONNECT') {
      process.stderr.write(
        `[bridge] trtc.startScreenShare ignored: state=${this.state}\n`,
      );
      return;
    }
    process.stderr.write(
      `[bridge] trtc.startScreenShare: requesting displayIndex=${displayIndex ?? 'primary'}\n`,
    );
    this.window.webContents.send(IPC.trtcStartScreenShare, { displayIndex });
  }

  /** Stop the substream screen share. Safe to call when nothing is shared. */
  stopScreenShare(): void {
    if (this.window.isDestroyed()) return;
    process.stderr.write('[bridge] trtc.stopScreenShare: requesting\n');
    this.window.webContents.send(IPC.trtcStopScreenShare, null);
  }

  // ── Audio bridge (Cut 2) ───────────────────────────────────────────────

  onRemotePcm(cb: RemotePcmListener): () => void {
    this.remotePcmListeners.push(cb);
    return () => {
      this.remotePcmListeners = this.remotePcmListeners.filter((x) => x !== cb);
    };
  }

  /**
   * Enqueue assistant PCM (int16 LE mono) for paced delivery into the TRTC
   * custom-audio path. Gemini emits TTS in bursts (sometimes 0 chunks for
   * 200 ms, then 5 chunks back-to-back); TRTC's `sendCustomAudioData`
   * explicitly warns that uneven inter-frame intervals trigger choppy
   * playback. So we maintain a byte queue and a 20 ms drainer that ships
   * one fixed-size frame per tick, regardless of arrival jitter.
   */
  sendPcm(pcm: Buffer, sampleRate: number, _timestampMs = 0): void {
    if (this.window.isDestroyed()) {
      process.stderr.write('[bridge] trtc.sendPcm dropped: window destroyed\n');
      return;
    }
    if (this.state !== 'CONNECT') {
      if (this.sendPcmCount === 0) {
        process.stderr.write(
          `[bridge] trtc.sendPcm dropped: state=${this.state} (room not joined)\n`,
        );
      }
      return;
    }
    if (pcm.length === 0) return;

    if (this.sendPcmRate !== sampleRate) {
      // Sample-rate change resets the pacer (rare; would only happen if
      // we swap realtime providers mid-call). Drop any leftover bytes from
      // the prior rate.
      this.sendPcmQueue = [];
      this.sendPcmQueuedBytes = 0;
      this.sendPcmRate = sampleRate;
    }

    this.sendPcmQueue.push(pcm);
    this.sendPcmQueuedBytes += pcm.length;
    this.sendPcmCount += 1;
    if (this.sendPcmCount === 1) {
      process.stderr.write(
        `[bridge] trtc: first send-pcm bytes=${pcm.length} rate=${sampleRate}\n`,
      );
    } else if (this.sendPcmCount % 200 === 0) {
      process.stderr.write(
        `[bridge] trtc: send-pcm enq=${this.sendPcmCount} sent=${this.sendPcmFramesShipped} qBytes=${this.sendPcmQueuedBytes}\n`,
      );
    }
    this.startDrainerIfNeeded();
  }

  private startDrainerIfNeeded(): void {
    if (this.sendPcmTimer || this.sendPcmRate === 0) return;
    const frameMs = 20; // matches TRTC's recommended frame duration
    const bytesPerFrame = Math.floor((this.sendPcmRate * frameMs) / 1000) * 2;
    this.sendPcmTimer = setInterval(() => {
      if (this.state !== 'CONNECT') {
        // Drop anything we still have if we left the room.
        this.sendPcmQueue = [];
        this.sendPcmQueuedBytes = 0;
        this.stopDrainer();
        return;
      }
      if (this.sendPcmQueuedBytes < bytesPerFrame) {
        // Underrun — Gemini hasn't given us a full frame yet. Don't pad
        // with silence; that would inject unnatural pauses. Just skip
        // this tick and try again in 20 ms. Pacing resumes on the very
        // next frame's arrival.
        return;
      }
      const frame = this.dequeue(bytesPerFrame);
      if (!frame) return;
      this.sendPcmFramesShipped += 1;
      const ab = frame.buffer.slice(frame.byteOffset, frame.byteOffset + frame.byteLength);
      this.window.webContents.send(IPC.trtcSendPcm, {
        pcmB64: Buffer.from(ab).toString('base64'),
        sampleRate: this.sendPcmRate,
        timestampMs: 0, // preload calls generateCustomPTS
      });
    }, frameMs);
    if (this.sendPcmTimer.unref) this.sendPcmTimer.unref();
  }

  private dequeue(n: number): Buffer | null {
    if (this.sendPcmQueuedBytes < n) return null;
    // Fast path: head buffer alone covers it.
    const head = this.sendPcmQueue[0];
    if (head && head.length >= n) {
      const out = head.subarray(0, n);
      const rest = head.subarray(n);
      if (rest.length === 0) this.sendPcmQueue.shift();
      else this.sendPcmQueue[0] = rest;
      this.sendPcmQueuedBytes -= n;
      return Buffer.from(out); // copy so the slice doesn't pin the original
    }
    // Slow path: stitch across multiple chunks.
    const out = Buffer.allocUnsafe(n);
    let written = 0;
    while (written < n) {
      const next = this.sendPcmQueue[0];
      if (!next) break;
      const take = Math.min(next.length, n - written);
      next.copy(out, written, 0, take);
      written += take;
      if (take === next.length) this.sendPcmQueue.shift();
      else this.sendPcmQueue[0] = next.subarray(take);
    }
    this.sendPcmQueuedBytes -= written;
    return out;
  }

  private stopDrainer(): void {
    if (this.sendPcmTimer) {
      clearInterval(this.sendPcmTimer);
      this.sendPcmTimer = null;
    }
  }

  // ── Internals ──────────────────────────────────────────────────────────

  private transitionToDisconnect(reason: number): void {
    if (this.state === 'DISCONNECT') return;
    const prev = this.state;
    const roomId = this.currentRoomId;
    this.state = 'DISCONNECT';
    // currentRoomId is intentionally retained so connect() can suppress
    // the same-room reservation that may keep arriving until the server
    // realizes we left.
    process.stderr.write(
      `[bridge] trtc: ${prev} -> DISCONNECT (roomId=${roomId} reason=${reason})\n`,
    );
    for (const cb of this.exitListeners) {
      try {
        cb({ roomId, reason });
      } catch (err) {
        process.stderr.write(
          `[bridge] trtc exit listener threw: ${String(err)}\n`,
        );
      }
    }
    this.sendPcmCount = 0;
    this.remotePcmCount = 0;
    this.sendPcmFramesShipped = 0;
    this.stopDrainer();
    this.sendPcmQueue = [];
    this.sendPcmQueuedBytes = 0;
    this.sendPcmRate = 0;
  }
}
