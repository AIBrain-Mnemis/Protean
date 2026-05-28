/**
 * Presence client — REST heartbeat against the matchmaking server.
 *
 *   - POST /rtc/bots/:botId/heartbeat every 2s with an empty body.
 *   - Server responds with `{ success, result: { status, assignment, serverTime } }`.
 *   - When status === "RESERVED" and assignment is present, fire onAssignment.
 *     The orchestrator hands the assignment to TrtcController, which owns the
 *     IDLE/CONNECT/DISCONNECT state machine and decides whether to actually join.
 *
 * Hangup detection is no longer in this layer: there is no `shouldHangup`
 * field, and there is no `confirm` or DELETE call to make. TRTC's
 * `onExitRoom` is the single source of truth for "we left the call".
 *
 * Network failures back off 1/2/4/8/16s; after 5 consecutive failures the
 * client emits `onOffline` and pauses heartbeating until `start()` is
 * called again.
 *
 * HTTP is done via `node:http` / `node:https` instead of the global fetch.
 * Electron's main-process fetch goes through Chromium's network stack and
 * has several reproducible localhost hangs (IPv6 preference, half-open
 * keep-alive, Origin/CORS preflight). Node's raw http client sidesteps
 * all of them.
 */

import * as http from 'node:http';
import * as https from 'node:https';
import { URL } from 'node:url';

import {
  Assignment,
  HeartbeatResponse,
  PresenceConfig,
  ServerBotStatus,
} from './types.js';

/** Minimal Response-shaped value used internally. */
interface MinimalResponse {
  ok: boolean;
  status: number;
  text: () => Promise<string>;
  json: () => Promise<unknown>;
}

const DEFAULT_BACKOFF_MS = [1_000, 2_000, 4_000, 8_000, 16_000];
const DEFAULT_INTERVAL_MS = 2_000;

export interface PresenceEvents {
  /** Server has a fresh assignment for us. Bridge should ask TRTC to connect. */
  onAssignment: (a: Assignment) => void;
  /**
   * Server says no call is active for us. If TRTC still thinks it's in a
   * room (server got there first via webhook 102 / cron), the orchestrator
   * should leave proactively instead of waiting for TRTC's keep-alive.
   */
  onIdle: () => void;
  /** Server's view of our state — diagnostics only. */
  onServerStatus: (status: ServerBotStatus) => void;
  /** 5 consecutive failures; presence is dead until start() is called again. */
  onOffline: (reason: string) => void;
}

export class PresenceClient {
  private timer: NodeJS.Timeout | null = null;
  private inflight = false;
  private consecutiveFailures = 0;
  private stopped = true;

  constructor(
    private readonly config: PresenceConfig,
    private readonly events: PresenceEvents,
  ) {}

  get botId(): string {
    return this.config.botId;
  }

  /** Start the heartbeat loop. Idempotent. */
  start(): void {
    if (!this.stopped) return;
    this.stopped = false;
    this.consecutiveFailures = 0;
    void this.tick();
  }

  /** Stop the heartbeat loop. Idempotent. */
  stop(): void {
    this.stopped = true;
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
  }

  // ── Internal loop ──────────────────────────────────────────────────────

  private async tick(): Promise<void> {
    if (this.stopped) return;
    if (this.inflight) {
      this.scheduleNext(this.intervalMs());
      return;
    }
    this.inflight = true;
    try {
      const response = await this.heartbeat();
      this.consecutiveFailures = 0;
      this.handleResponse(response);
    } catch (err) {
      this.consecutiveFailures += 1;
      process.stderr.write(
        `[presence] heartbeat failure #${this.consecutiveFailures}: ${String(err)}\n`,
      );
      const schedule = this.config.backoffScheduleMs ?? DEFAULT_BACKOFF_MS;
      if (this.consecutiveFailures > schedule.length) {
        this.stopped = true;
        this.events.onOffline(
          `${this.consecutiveFailures} consecutive heartbeat failures: ${String(err)}`,
        );
        this.inflight = false;
        return;
      }
      const delay = schedule[this.consecutiveFailures - 1] ?? schedule[schedule.length - 1] ?? 16_000;
      this.scheduleNext(delay);
      this.inflight = false;
      return;
    }
    this.inflight = false;
    this.scheduleNext(this.intervalMs());
  }

  private intervalMs(): number {
    return this.config.heartbeatIntervalMs ?? DEFAULT_INTERVAL_MS;
  }

  private scheduleNext(delay: number): void {
    if (this.stopped) return;
    if (this.timer) clearTimeout(this.timer);
    this.timer = setTimeout(() => void this.tick(), delay);
  }

  private async heartbeat(): Promise<HeartbeatResponse> {
    const res = await this.postEmpty(
      `/rtc/bots/${encodeURIComponent(this.config.botId)}/heartbeat`,
    );
    if (!res.ok) {
      throw new Error(
        `heartbeat HTTP ${res.status}: ${(await res.text()).slice(0, 200)}`,
      );
    }
    const body = (await res.json()) as HeartbeatResponse;
    if (!body || !body.result) {
      throw new Error(`heartbeat malformed body: ${JSON.stringify(body).slice(0, 200)}`);
    }
    return body;
  }

  private handleResponse(res: HeartbeatResponse): void {
    const result = res.result;
    this.events.onServerStatus(result.status);

    if (result.status === 'RESERVED' && result.assignment) {
      // Always forward the assignment. Dedup is owned by the consumer
      // (TrtcController) which knows whether it's already CONNECTed to
      // the same room or in a DISCONNECT state for that room.
      this.events.onAssignment(result.assignment);
    } else if (result.status === 'IDLE') {
      // Per docs/rtc-client-integration.md §4.3: bot must proactively
      // exitRoom on IDLE. The orchestrator decides whether anything
      // actually needs to happen (no-op if controller is already IDLE
      // or DISCONNECT).
      this.events.onIdle();
    }
  }

  private async postEmpty(path: string): Promise<MinimalResponse> {
    const url = new URL(path, this.config.baseUrl);
    const payload = Buffer.from('{}', 'utf-8');
    const timeoutMs = Math.max(3_000, this.intervalMs() * 1.5);

    const isHttps = url.protocol === 'https:';
    const lib = isHttps ? https : http;
    const port = url.port ? Number(url.port) : isHttps ? 443 : 80;

    return new Promise<MinimalResponse>((resolve, reject) => {
      const req = lib.request(
        {
          // Force IPv4 to dodge AAAA-preference stalls against localhost
          // / dev hosts that only listen on 0.0.0.0.
          family: 4,
          host: url.hostname,
          port,
          path: url.pathname + url.search,
          method: 'POST',
          headers: {
            'Content-Type': 'application/json; charset=utf-8',
            'Content-Length': payload.length,
            'Connection': 'close',
          },
        },
        (res) => {
          const chunks: Buffer[] = [];
          res.on('data', (chunk: Buffer) => chunks.push(chunk));
          res.on('end', () => {
            const buf = Buffer.concat(chunks);
            const status = res.statusCode ?? 0;
            resolve({
              ok: status >= 200 && status < 300,
              status,
              text: async () => buf.toString('utf-8'),
              json: async () => {
                const text = buf.toString('utf-8');
                if (!text) return null;
                return JSON.parse(text);
              },
            });
          });
          res.on('error', reject);
        },
      );

      const timer = setTimeout(() => {
        req.destroy(new Error(`presence request timeout after ${timeoutMs}ms`));
      }, timeoutMs);
      timer.unref();

      req.on('error', (err) => {
        clearTimeout(timer);
        reject(err);
      });
      req.on('close', () => clearTimeout(timer));

      req.write(payload);
      req.end();
    });
  }
}
