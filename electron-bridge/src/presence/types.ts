/**
 * REST payload types for the matchmaking server.
 *
 * Heartbeat-only API: a single endpoint drives presence.
 *   POST /rtc/bots/:botId/heartbeat   (empty body)
 *   200 → { success: true, result: { status, assignment|null, serverTime } }
 *
 * The bot reports nothing about its own state. The server decides:
 *   - IDLE    : no work
 *   - RESERVED: assignment is present; bot should connect
 *   - BUSY    : already in a call (server-side bookkeeping only)
 *
 * Hangup is detected via TRTC `onExitRoom`, not via REST.
 */

export type ServerBotStatus = 'IDLE' | 'RESERVED' | 'BUSY';

/** Sent to the bot in heartbeat responses when serverStatus = RESERVED. */
export interface Assignment {
  sdkAppId: number;
  /** TRTC strRoomId (matches /^room_[A-Za-z0-9_]{1,32}$/). */
  roomId: string;
  /** Bot's TRTC userId (NOT botId), matches /^bot_[A-Za-z0-9_]{1,32}$/. */
  userId: string;
  /** TRTC entry credential, TTL 1h. */
  userSig: string;
  displayName: string;
  /** ms epoch when the reservation was created. */
  reservedAt: number;
}

export interface HeartbeatResult {
  status: ServerBotStatus;
  assignment: Assignment | null;
  serverTime: number;
}

export interface HeartbeatResponse {
  success: boolean;
  result: HeartbeatResult;
}

export interface ApiError {
  success: false;
  errors: Array<{ message: string; code?: string }>;
}

/**
 * Configuration for the presence client. All values come from env in
 * production; tests pass a manual config.
 */
export interface PresenceConfig {
  /** Base URL of the matchmaking server, e.g. `http://server.local:3000`. */
  baseUrl: string;
  /** Stable bot id of the form `bot_<8 hex>`. */
  botId: string;
  /** Default 2000 ms. */
  heartbeatIntervalMs?: number;
  /**
   * Default exponential backoff schedule on heartbeat network errors.
   * After 5 failures the client emits `onOffline` and pauses until
   * `start()` is called again.
   */
  backoffScheduleMs?: number[];
}
