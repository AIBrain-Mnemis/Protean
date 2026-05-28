/**
 * Shared Realtime provider interface.
 *
 * Both the mock (for tests / dev) and the real Gemini client (for production)
 * implement this. `session.ts` picks one via the factory based on
 * PROTEAN_BRIDGE_REALTIME env (default: mock).
 */

import type { ToolDeclaration } from '../protocol.js';

/** Events the realtime provider pushes back to the bridge session. */
export interface RealtimeEvents {
  /** Assistant text turn (streaming). */
  onAssistantText: (text: string) => void;
  /** Assistant audio chunk (24 kHz mono int16 PCM). */
  onAssistantAudio: (pcm: Buffer) => void;
  /** User speech transcript (only fires if input transcription is enabled). */
  onUserSpeech: (text: string, final: boolean, audioDuration?: number) => void;
  /**
   * Realtime model wants to call a tool.
   *
   * `callId` is the provider's id; the session must echo it back when
   * eventually calling Realtime.sendToolResponse. `name` and `arguments`
   * are forwarded as-is.
   */
  onToolCall: (callId: string, name: string, args: Record<string, unknown>) => void;
  /** Non-fatal error from the provider. */
  onError: (code: string, message: string, recoverable: boolean) => void;
  /** Provider closed the connection. session.ts should propagate session_ended. */
  onClose: (reason: string) => void;
}

/** Driver for one realtime conversation session. */
export interface Realtime {
  /**
   * Connect + configure. Returns once the underlying transport is open and
   * the system instruction + tools are accepted.
   *
   * Bridge MUST strip the `handler` field from each tool before forwarding;
   * `tools` here is the post-strip view.
   */
  start(systemInstruction: string, tools: ToolDeclaration[]): Promise<void>;

  /** Send a user text turn (turn-complete=true). */
  sendUserText(text: string): void;

  /** Send a user audio chunk (16 kHz mono int16 PCM). */
  sendUserAudio(pcm: Buffer): void;

  /** Send an out-of-band system notification (Python-side `notify`). */
  sendNotification(text: string): void;

  /** Send a single image (typically a JPEG screenshot). */
  sendImage(mime: string, imageB64: string): void;

  /** Reply to a previous tool_call. */
  sendToolResponse(callId: string, name: string, message: string, error?: string): void;

  /** Close the underlying connection and release resources. */
  stop(): Promise<void>;
}
