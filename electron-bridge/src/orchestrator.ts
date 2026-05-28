/**
 * BridgeOrchestrator — wires presence, WS clients, and TRTC sessions.
 *
 * Lifetime: one instance per bridge process. Owns:
 *   - PresenceClient (heartbeat-only, reads RESERVED + assignment from server)
 *   - TrtcController (IDLE / CONNECT / DISCONNECT state machine — owns dedup)
 *   - Bridge-side hook into wsServer (push envelopes, observe client connect)
 *
 * Flow (per the new TRTC contract):
 *
 *   1. Heartbeat returns status=RESERVED + assignment.
 *      → orchestrator forwards the assignment to TrtcController.connect().
 *      → If the controller actually started a connect attempt (state was
 *        IDLE or DISCONNECT(other room)), emit room_state(ringing) to
 *        Python so TeachSession can spin up.
 *
 *   2. TRTC fires onEnterRoom (success).
 *      → No explicit signal upward; we already optimistically emitted
 *        ringing. Python's TeachSession sends session_start when ready;
 *        on session_start we emit room_state(connected) so the UI side
 *        knows we are in the call.
 *
 *   3. TRTC fires onExitRoom (any reason — kick, remote-left, dismissed,
 *      network drop after retries, local leave()).
 *      → orchestrator emits room_state(ended) to Python so TeachSession
 *        winds down.
 *
 *   4. Python sends session_end (TeachSession ended for any reason).
 *      → If still CONNECTed, ask TRTC to leave. The leave triggers (3)
 *        above which then propagates ended → no-op (already sent).
 *
 * Cached-assignment replay: if an assignment arrives before any Python
 * WS client is attached, we still call connect() (TRTC is independent
 * of Python's lifecycle); on the next client_connected we re-emit the
 * ringing event so Python doesn't miss the call.
 */

import {
  Assignment,
  PresenceClient,
  PresenceConfig,
} from './presence/index.js';
import { Envelope, RoomStatePayload } from './protocol.js';
import { makeEnvelope } from './envelope.js';
import type { TrtcController } from './trtcController.js';

/** Pushes an envelope to whoever is connected; returns false if no client. */
export type EnvelopePusher = (env: Envelope) => boolean;

export interface OrchestratorEgress {
  pushEnvelope: EnvelopePusher;
  /** Called when a new WS client attaches; orchestrator may flush queued state. */
  onClientConnected: (cb: () => void) => void;
  /** Called when a Session.start() succeeds. */
  onSessionStart: (cb: (callId: string) => void) => void;
  /** Called when a Session.end() runs. */
  onSessionEnd: (cb: (reason: string) => void) => void;
  /** TRTC driver. Required for any real call activity. */
  trtc?: TrtcController;
}

interface ActiveCall {
  assignment: Assignment;
  /** True after Python's session_start fired (we've sent connected upstream). */
  pythonAttached: boolean;
}

export class BridgeOrchestrator {
  private readonly presence: PresenceClient;
  private active: ActiveCall | null = null;
  private trtcExitUnsub: (() => void) | null = null;
  /**
   * RoomIds we have intentionally ended (Python session_end). The presence
   * server keeps RESERVED for the same roomId until its cron (~1 min) or
   * the caller hangs up, so without this set the bot would auto-rejoin
   * the room we just left and loop forever. Cleared when presence
   * transitions to IDLE.
   */
  private blockedRoomIds: Set<string> = new Set();

  constructor(
    presenceConfig: PresenceConfig,
    private readonly egress: OrchestratorEgress,
  ) {
    this.presence = new PresenceClient(presenceConfig, {
      onAssignment: (a) => this.handleAssignment(a),
      onIdle: () => this.handleServerIdle(),
      onServerStatus: (s) => {
        if (process.env['PROTEAN_LOG_PRESENCE']) {
          process.stderr.write(`[orchestrator] server status: ${s}\n`);
        }
      },
      onOffline: (reason) =>
        process.stderr.write(`[orchestrator] presence offline: ${reason}\n`),
    });

    egress.onClientConnected(() => this.handleClientConnected());
    egress.onSessionStart((callId) => this.handleSessionStart(callId));
    egress.onSessionEnd((reason) => this.handleSessionEnd(reason));

    if (egress.trtc) {
      this.trtcExitUnsub = egress.trtc.onExit((info) => this.handleTrtcExit(info));
    }
  }

  start(): void {
    this.presence.start();
  }

  async stop(): Promise<void> {
    this.presence.stop();
    if (this.trtcExitUnsub) {
      this.trtcExitUnsub();
      this.trtcExitUnsub = null;
    }
    if (this.active && this.egress.trtc) {
      try {
        this.egress.trtc.leave();
      } catch (err) {
        process.stderr.write(`[orchestrator] cleanup leave failed: ${String(err)}\n`);
      }
    }
    this.active = null;
  }

  /** Diagnostic snapshot (used by tests). */
  get snapshot(): {
    activeRoomId: string | null;
    pythonAttached: boolean;
    botId: string;
  } {
    return {
      activeRoomId: this.active?.assignment.roomId ?? null,
      pythonAttached: this.active?.pythonAttached ?? false,
      botId: this.presence.botId,
    };
  }

  // ── Presence event handlers ─────────────────────────────────────────────

  private handleAssignment(a: Assignment): void {
    if (!this.egress.trtc) {
      process.stderr.write(
        '[orchestrator] handleAssignment: no TRTC controller; ignoring\n',
      );
      return;
    }

    if (this.blockedRoomIds.has(a.roomId)) {
      process.stderr.write(
        `[orchestrator] handleAssignment dropped: roomId=${a.roomId} is blocked ` +
          '(ended this side, waiting for server to dismiss)\n',
      );
      return;
    }

    // The controller is the source of truth for IDLE/CONNECT/DISCONNECT.
    // It will silently reject if we are already CONNECTed or just left
    // this exact room.
    const accepted = this.egress.trtc.connect(a);
    if (!accepted) return;

    process.stderr.write(
      `[orchestrator] handleAssignment accepted roomId=${a.roomId} ` +
        `(prevActive=${this.active?.assignment.roomId ?? 'none'})\n`,
    );
    this.active = { assignment: a, pythonAttached: false };
    this.emitRinging(a);
  }

  // ── TRTC event handlers ─────────────────────────────────────────────────

  private handleServerIdle(): void {
    // Server says no call is active for us — the call is dismissed, so
    // any prior blocks on the just-ended roomId can go.
    if (this.blockedRoomIds.size > 0) {
      this.blockedRoomIds.clear();
    }
    // If TRTC is still in a room (server got there first via webhook 102 /
    // cron or by Bot crash recovery), proactively leave so we don't sit
    // in stale CONNECT for 30-90s waiting on TRTC keep-alive.
    // handleTrtcExit will then emit ended to Python.
    const trtc = this.egress.trtc;
    if (!trtc) return;
    if (trtc.connectState !== 'CONNECT') return;
    process.stderr.write('[orchestrator] server IDLE while CONNECT; leaving room\n');
    try {
      trtc.leave();
    } catch (err) {
      process.stderr.write(`[orchestrator] leave on server-IDLE threw: ${String(err)}\n`);
    }
  }

  private handleTrtcExit(info: { roomId: string | null; reason: number }): void {
    process.stderr.write(
      `[orchestrator] handleTrtcExit roomId=${info.roomId} reason=${info.reason}\n`,
    );
    if (!this.active) return;
    // If TRTC tells us about a different room than we think is active
    // (shouldn't happen, but defensive), still emit ended for ours.
    const roomId = this.active.assignment.roomId;
    this.active = null;
    this.emitEnded(roomId, `trtc-exit:${info.reason}`);
  }

  // ── WS / Session event handlers ─────────────────────────────────────────

  private handleClientConnected(): void {
    // If we have an unconsumed assignment, replay the ringing event so
    // Python's late connect doesn't miss the call.
    if (this.active && !this.active.pythonAttached) {
      this.emitRinging(this.active.assignment);
    }
  }

  private handleSessionStart(callId: string): void {
    process.stderr.write(`[orchestrator] handleSessionStart call_id=${callId}\n`);
    if (!this.active) {
      process.stderr.write(
        `[orchestrator] session_start(call_id=${callId}) with no active assignment; ignoring\n`,
      );
      return;
    }
    if (this.active.assignment.roomId !== callId) {
      process.stderr.write(
        `[orchestrator] session_start call_id mismatch ` +
          `(server=${this.active.assignment.roomId}, python=${callId})\n`,
      );
      return;
    }
    this.active.pythonAttached = true;
    this.emitConnected(callId);
  }

  private handleSessionEnd(reason: string): void {
    process.stderr.write(
      `[orchestrator] handleSessionEnd reason=${reason} ` +
        `(active=${this.active?.assignment.roomId ?? 'none'} ` +
        `pythonAttached=${this.active?.pythonAttached ?? false})\n`,
    );
    if (!this.active) return;
    // Block any further assignment for this roomId. The presence server
    // stays RESERVED with the same roomId until cron dismisses it
    // (~1 min) or the caller hangs up; without this, the very next
    // heartbeat would feed us the same assignment and we'd rejoin in a
    // loop. The block clears on the next presence onIdle.
    this.blockedRoomIds.add(this.active.assignment.roomId);
    // Python is done. Ask TRTC to leave; the resulting onExitRoom will
    // route through handleTrtcExit, which clears `active` and emits ended.
    if (this.egress.trtc) {
      try {
        this.egress.trtc.leave();
      } catch (err) {
        process.stderr.write(
          `[orchestrator] trtc.leave on sessionEnd threw: ${String(err)}\n`,
        );
      }
    } else {
      // No TRTC at all — clear active manually so a future assignment works.
      const roomId = this.active.assignment.roomId;
      this.active = null;
      this.emitEnded(roomId, `session-end:${reason}`);
    }
  }

  // ── Envelope emission ──────────────────────────────────────────────────

  private emitRinging(a: Assignment): void {
    const payload: RoomStatePayload = {
      state: 'ringing',
      call_id: a.roomId,
      remote_users: [],
      network_quality: 0,
      detail: `assignment:${a.displayName}`,
    };
    const env = makeEnvelope('room_state', payload, 0);
    const sent = this.egress.pushEnvelope(env);
    process.stderr.write(
      `[orchestrator] emitRinging roomId=${a.roomId} delivered=${sent}\n`,
    );
  }

  private emitConnected(roomId: string): void {
    const payload: RoomStatePayload = {
      state: 'connected',
      call_id: roomId,
      remote_users: [],
      network_quality: 0,
      detail: 'session-start',
    };
    this.egress.pushEnvelope(makeEnvelope('room_state', payload, 0));
  }

  private emitEnded(roomId: string, detail: string): void {
    const payload: RoomStatePayload = {
      state: 'ended',
      call_id: roomId,
      remote_users: [],
      network_quality: 0,
      detail,
    };
    const sent = this.egress.pushEnvelope(makeEnvelope('room_state', payload, 0));
    process.stderr.write(
      `[orchestrator] emitEnded roomId=${roomId} detail=${detail} delivered=${sent}\n`,
    );
  }
}
