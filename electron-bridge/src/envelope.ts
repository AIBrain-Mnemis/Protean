/**
 * Envelope validation + error helpers.
 */

import {
  Envelope,
  ErrorCode,
  ErrorPayload,
  MessageType,
  PROTOCOL_VERSION,
} from './protocol.js';

export const TEXT_LIMIT_BYTES = 256 * 1024;
export const BINARY_LIMIT_BYTES = 4 * 1024 * 1024;

const KNOWN_TYPES: ReadonlySet<MessageType> = new Set<MessageType>([
  'hello',
  'welcome',
  'session_start',
  'session_started',
  'session_end',
  'session_ended',
  'assistant_text',
  'user_speech',
  'realtime_error',
  'tool_call',
  'tool_result',
  'notify',
  'send_text',
  'send_image',
  'room_state',
  'screen_state',
  'recorder_state',
  'ping',
  'pong',
  'error',
]);

export class EnvelopeError extends Error {
  constructor(
    public readonly code: ErrorCode,
    public readonly fatal: boolean,
    message: string,
    public readonly inReplyTo?: string,
  ) {
    super(message);
  }
}

/** Parse a raw TEXT WS frame into a typed envelope, or throw EnvelopeError. */
export function parseEnvelope(raw: string | Buffer): Envelope {
  const text = typeof raw === 'string' ? raw : raw.toString('utf-8');
  if (Buffer.byteLength(text, 'utf-8') > TEXT_LIMIT_BYTES) {
    throw new EnvelopeError('payload_too_large', false, 'TEXT message exceeds 256 KiB');
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    throw new EnvelopeError('bad_envelope', true, 'invalid JSON');
  }

  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    throw new EnvelopeError('bad_envelope', true, 'envelope must be a JSON object');
  }

  const obj = parsed as Record<string, unknown>;
  for (const key of ['v', 'type', 'id', 'ts', 'payload'] as const) {
    if (!(key in obj)) {
      throw new EnvelopeError('bad_envelope', true, `missing required field: ${key}`);
    }
  }

  if (obj['v'] !== PROTOCOL_VERSION) {
    throw new EnvelopeError(
      'version_mismatch',
      true,
      `expected v=${PROTOCOL_VERSION}, got ${String(obj['v'])}`,
      typeof obj['id'] === 'string' ? obj['id'] : undefined,
    );
  }

  const type = obj['type'];
  if (typeof type !== 'string' || !KNOWN_TYPES.has(type as MessageType)) {
    throw new EnvelopeError(
      'unknown_type',
      false,
      `unknown type: ${String(type)}`,
      typeof obj['id'] === 'string' ? obj['id'] : undefined,
    );
  }

  if (typeof obj['id'] !== 'string' || obj['id'].length === 0) {
    throw new EnvelopeError('bad_envelope', true, '`id` must be a non-empty string');
  }
  if (typeof obj['ts'] !== 'number') {
    throw new EnvelopeError('bad_envelope', true, '`ts` must be a number');
  }
  if (typeof obj['payload'] !== 'object' || obj['payload'] === null) {
    throw new EnvelopeError('bad_envelope', true, '`payload` must be an object');
  }

  return obj as unknown as Envelope;
}

/** Build a standard error envelope. */
export function errorEnvelope(
  err: EnvelopeError | { code: string; message: string; fatal?: boolean; inReplyTo?: string },
  ts = 0,
): Envelope<ErrorPayload> {
  const payload: ErrorPayload = {
    code: err.code,
    message: err.message,
    fatal: 'fatal' in err ? Boolean(err.fatal) : false,
  };
  const inReplyTo = 'inReplyTo' in err ? err.inReplyTo : undefined;
  if (inReplyTo) payload.in_reply_to = inReplyTo;
  return {
    v: PROTOCOL_VERSION,
    type: 'error',
    id: `err_${Math.random().toString(36).slice(2, 10)}`,
    ts,
    payload,
  };
}

let envelopeCounter = 0;

/** Build a fresh envelope of the given type. Bridge-side helper. */
export function makeEnvelope<P>(
  type: MessageType,
  payload: P,
  ts: number,
): Envelope<P> {
  envelopeCounter += 1;
  return {
    v: PROTOCOL_VERSION,
    type,
    id: `b_${Date.now().toString(36)}_${envelopeCounter}`,
    ts,
    payload,
  };
}
