/**
 * IPC protocol types for the Python <-> Electron bridge.
 *
 * This file is the TypeScript-side mirror of `protean/channels/_protocol.py`.
 * The two MUST stay in lockstep — the MessageType union below and the one in
 * the Python file are diffed in CI.
 *
 * This file is type declarations only; no runtime logic, no I/O.
 */

// ── Protocol version ────────────────────────────────────────────

export const PROTOCOL_VERSION = 1 as const;

// ── Message type union (mirror of MessageType in _protocol.py) ─────────────

export type MessageType =
  // Handshake / lifecycle
  | 'hello'
  | 'welcome'
  | 'session_start'
  | 'session_started'
  | 'session_end'
  | 'session_ended'
  // Realtime conversation events (S→C)
  | 'assistant_text'
  | 'user_speech'
  | 'realtime_error'
  // Tool calls
  | 'tool_call'
  | 'tool_result'
  // Out-of-band (C→S)
  | 'notify'
  | 'send_text'
  | 'send_image'
  // State pushes (S→C)
  | 'room_state'
  | 'screen_state'
  // Recorder coordination (C→S)
  | 'recorder_state'
  // Liveness
  | 'ping'
  | 'pong'
  // Error envelope
  | 'error';

// ── Envelope (every TEXT message) ────────────────────────────────────────────

export interface Envelope<P = unknown> {
  v: typeof PROTOCOL_VERSION;
  type: MessageType;
  id: string; // sender-unique correlation id
  ts: number; // monotonic seconds since session_start; 0 = pre-session
  payload: P;
}

// ── Handshake payloads ─────────────────────────────────────────────────────

export interface HelloPayload {
  protocol_version: number;
  daemon_version: string;
  daemon_pid: number;
}

export interface WelcomePayload {
  bridge_version: string;
  capabilities: string[];
}

// ── Session lifecycle payloads ───────────────────────────────────────────────────

/**
 * Tool declaration sent in session_start.realtime.tools[].
 *
 * Bridge MUST strip `handler` before forwarding to the Realtime provider.
 */
export interface ToolDeclaration {
  name: string;
  handler: 'python' | 'bridge';
  description: string;
  parameters: Record<string, unknown>;
}

export interface RealtimeConfig {
  voice: string;
  input_sample_rate: number;
  output_sample_rate: number;
  video_fps_limit: number;
  system_instruction: string;
  tools: ToolDeclaration[];
}

export interface SessionStartPayload {
  realtime: RealtimeConfig;
  accept_call_id: string;
}

export interface SessionStartedPayload {
  session_id: string;
}

export interface TranscriptSummary {
  duration: number;
  user_words: number;
  assistant_words: number;
}

export interface SessionEndPayload {
  reason: string;
}

export interface SessionEndedPayload {
  reason: string;
  transcript_summary?: TranscriptSummary;
}

// ── Realtime event payloads ───────────────────────────────────────────────────

export interface AssistantTextPayload {
  text: string;
}

export interface UserSpeechPayload {
  text: string;
  audio_duration?: number;
  final: boolean;
}

export interface RealtimeErrorPayload {
  code: string;
  message: string;
  recoverable: boolean;
}

// ── Tool call payloads ─────────────────────────────────────────────────────────

export interface ToolCallPayload {
  call_id: string;
  name: string;
  arguments: Record<string, unknown>;
}

export interface ToolResultPayload {
  in_reply_to: string;
  call_id: string;
  name: string;
  message: string;
  is_async: boolean;
  error?: string;
}

// ── Out-of-band payloads ──────────────────────────────────────────────────────

export interface NotifyPayload {
  text: string;
}

export interface SendTextPayload {
  text: string;
}

export interface SendImagePayload {
  mime: string;
  image_b64: string;
  caption?: string;
}

// ── State push payloads ───────────────────────────────────────────────────────────────────

export interface RemoteUser {
  user_id: string;
  has_audio: boolean;
  has_video: boolean;
  has_screen: boolean;
}

export type RoomState =
  | 'idle'
  | 'ringing'
  | 'connected'
  | 'ended'
  | 'error';

export interface RoomStatePayload {
  state: RoomState;
  call_id: string;
  remote_users: RemoteUser[];
  network_quality: number; // TRTC's onNetworkQuality, 0-6
  detail: string;
}

export type ScreenMode = 'off' | 'observe' | 'share';
export type ScreenSource = 'remote_screen' | 'local_screen' | 'null';

export interface ScreenStatePayload {
  mode: ScreenMode;
  source: ScreenSource;
  fps: number;
  resolution: [number, number];
  /**
   * Human-readable label for the captured surface, taken from
   * MediaStreamTrack.label (e.g. "Screen 1", "Window: Outlook"). Empty
   * string when no capture is active or the label is unknown.
   */
  source_label?: string;
  /**
   * 1-based index of the captured physical display, in the bridge's
   * primary-first ordering (display 1 = primary, matches macOS
   * `screencapture -D1`). Only set for `mode='share'`. The executor
   * uses this to target the same display the talker is sharing.
   */
  display_index?: number;
}

// ── Recorder payload ──────────────────────────────────────────────────────────

export interface RecorderStatePayload {
  recording: boolean;
  recording_id?: string;
  output_dir?: string;
}

// ── Liveness payloads ────────────────────────────────────────────────────────

export type PingPayload = Record<string, never>;

export interface PongPayload {
  in_reply_to: string;
}

// ── Error payload ─────────────────────────────────────────────────────────────

export type ErrorCode =
  | 'bad_envelope'
  | 'unknown_type'
  | 'version_mismatch'
  | 'unauthorized'
  | 'incompatible'
  | 'unsupported_local_tool'
  | 'payload_too_large'
  | 'not_in_session'
  | 'duplicate_session';

export interface ErrorPayload {
  /** ErrorCode for protocol-defined errors; free-form for domain errors. */
  code: string;
  message: string;
  in_reply_to?: string;
  fatal: boolean;
}

// ── Evidence frame header (BINARY) ─────────────────────────────────────────────
//
// Wire layout: 1-byte type tag + 2-byte BE header_len + header JSON + JPEG bytes.
// Only the JSON header is typed here; framing is done in transport code.

export const EVIDENCE_FRAME_TAG = 0x01 as const;

export interface EvidenceFrameHeader {
  ts: number;
  source: 'remote_screen' | 'local_screen';
  phash: number;
  w: number;
  h: number;
}

// ── Convenience: pre-session whitelist ─────────────────────────────────────────

export const PRE_SESSION_TYPES: ReadonlySet<MessageType> = new Set<MessageType>([
  'hello',
  'welcome',
  'ping',
  'pong',
  'error',
  'room_state',
  'recorder_state',
]);
