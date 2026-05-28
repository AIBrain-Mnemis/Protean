/**
 * Real Gemini Live realtime provider.
 *
 * Wraps `@google/genai`'s bidirectional Live session. Emits `Realtime`
 * events back to the bridge `Session`.
 *
 * Reads `GEMINI_API_KEY` from process env when start() is invoked. Secrets
 * are bridge-side; never sent over IPC.
 *
 * Tool routing reminder: only tools with handler:"python" reach the
 * bridge -> Realtime path; bridge MUST strip the `handler` field before
 * forwarding (we do it inside `start()`).
 */

import {
  GoogleGenAI,
  Modality,
  type FunctionDeclaration,
  type LiveServerMessage,
  type Session,
} from '@google/genai';

import type { ToolDeclaration } from '../protocol.js';
import type { Realtime, RealtimeEvents } from './types.js';

const DEFAULT_MODEL = 'gemini-live-2.5-flash-preview';
const DEFAULT_VOICE = 'Puck';

// How close to goAway's deadline we should resume. The server warns us
// some seconds before it will terminate the WebSocket; we keep using the
// current session until the deadline minus this margin, then swap. Bigger
// margin = safer (more time for resume handshake to complete) but the
// gap-free window before the swap is shorter.
const _RESUME_SAFETY_MARGIN_MS = 3000;

/** Optional knobs; values are read from env if not provided here. */
export interface CreateGeminiOptions {
  apiKey?: string;
  model?: string;
  voice?: string;
  /** Whether to ask Gemini for input transcription (drives onUserSpeech). */
  enableInputTranscription?: boolean;
}

class GeminiRealtime implements Realtime {
  private session: Session | null = null;
  private connected = false;
  private closed = false;
  private inboundAudioBytes = 0;
  private inboundAudioChunks = 0;
  private outboundMessages = 0;
  private outboundAudioChunks = 0;
  private outboundTextChunks = 0;
  private metricsTimer: NodeJS.Timeout | null = null;

  // Session-resumption state. Gemini Live caps session duration; before
  // termination it sends a goAway, and (when sessionResumption is enabled)
  // periodic sessionResumptionUpdate messages with a handle that lets us
  // open a fresh WS while keeping the model-side context. On goAway we
  // reconnect transparently — events.onClose is NOT fired unless the
  // resume itself fails.
  private ai: GoogleGenAI | null = null;
  private model = DEFAULT_MODEL;
  private baseConfig: Record<string, unknown> | null = null;
  private lastResumptionHandle: string | null = null;
  private reconnecting = false;
  // Pending deferred resume (scheduled by goAway). Cleared if we resume
  // for any other reason (e.g. an actual close) or on stop().
  private resumeTimer: NodeJS.Timeout | null = null;
  // Buffer user PCM while the WS handshake for the resume is in flight,
  // so we don't drop the user's speech across the gap.
  private pendingAudio: Buffer[] = [];
  private pendingAudioBytes = 0;

  constructor(
    private readonly events: RealtimeEvents,
    private readonly opts: CreateGeminiOptions,
  ) {}

  async start(systemInstruction: string, tools: ToolDeclaration[]): Promise<void> {
    const apiKey = this.opts.apiKey ?? process.env['GEMINI_API_KEY'];
    if (!apiKey) {
      throw new Error(
        'GEMINI_API_KEY is required to start the gemini realtime provider',
      );
    }
    const model = this.opts.model
      ?? process.env['PROTEAN_REALTIME_MODEL']
      ?? DEFAULT_MODEL;
    process.stderr.write(`[bridge] gemini model=${model}\n`);

    this.ai = new GoogleGenAI({ apiKey });
    this.model = model;

    const functionDeclarations = toFunctionDeclarations(tools);

    // Build config piece by piece so we don't violate exactOptionalPropertyTypes.
    const config: Record<string, unknown> = {
      responseModalities: [Modality.AUDIO],
      speechConfig: {
        voiceConfig: { prebuiltVoiceConfig: { voiceName: this.opts.voice ?? DEFAULT_VOICE } },
      },
    };
    if (systemInstruction) {
      config['systemInstruction'] = systemInstruction;
    }
    if (functionDeclarations.length > 0) {
      config['tools'] = [{ functionDeclarations }];
    }
    if (this.opts.enableInputTranscription !== false) {
      // Drives onUserSpeech (final transcripts of the user's mic input).
      config['inputAudioTranscription'] = {};
      config['outputAudioTranscription'] = {};
    }
    this.baseConfig = config;

    await this.openSession(null);

    // Periodic stderr beacon so we can see whether audio is flowing each
    // way without trawling per-chunk logs. Cleared in stop().
    this.metricsTimer = setInterval(() => {
      process.stderr.write(
        `[bridge] gemini metrics: connected=${this.connected} ` +
          `userAudio=${this.inboundAudioChunks}chunks/${this.inboundAudioBytes}B ` +
          `serverMsgs=${this.outboundMessages} ` +
          `serverAudio=${this.outboundAudioChunks} ` +
          `serverText=${this.outboundTextChunks}\n`,
      );
    }, 3_000);
    if (this.metricsTimer.unref) this.metricsTimer.unref();
  }

  /**
   * Open (or re-open) the underlying live session.
   * - `resumeHandle === null`: fresh session. Always succeeds or throws to
   *   the caller (initial connect path; never called twice unprompted).
   * - `resumeHandle !== null`: resume previous session state. Throws on
   *   failure so the caller can decide whether to give up.
   *
   * Always asks the server to send sessionResumptionUpdate messages so
   * we can keep `lastResumptionHandle` fresh.
   */
  private async openSession(resumeHandle: string | null): Promise<void> {
    if (!this.ai || !this.baseConfig) {
      throw new Error('openSession called before start()');
    }
    const config = { ...this.baseConfig };
    config['sessionResumption'] = resumeHandle ? { handle: resumeHandle } : {};

    const startMs = Date.now();
    await new Promise<void>((resolve, reject) => {
      let opened = false;
      const onOpen = (): void => {
        opened = true;
        this.connected = true;
        if (resumeHandle) {
          process.stderr.write(
            `[bridge] gemini resumed: handle=${resumeHandle.slice(0, 8)}… ` +
              `gap=${Date.now() - startMs}ms\n`,
          );
        }
        resolve();
      };
      const onError = (err: unknown): void => {
        const msg = errorMessage(err);
        process.stderr.write(`[bridge] gemini onerror: ${msg}\n`);
        if (!opened) {
          reject(new Error(`Gemini connect failed: ${msg}`));
        } else {
          this.events.onError('gemini_error', msg, true);
        }
      };
      const onClose = (event: unknown): void => {
        let code = 'unknown';
        let reason = '';
        if (event && typeof event === 'object') {
          if ('code' in event) code = String((event as { code: unknown }).code);
          if ('reason' in event) reason = String((event as { reason: unknown }).reason);
        }
        this.connected = false;
        process.stderr.write(
          `[bridge] gemini onclose code=${code} reason=${reason || '(none)'}\n`,
        );
        // If we are intentionally resuming, swallow this close — a fresh
        // session is about to take over. If the resume itself then fails,
        // resumeAfterGoAway will emit onClose explicitly.
        if (this.reconnecting || this.closed) return;
        this.closed = true;
        this.events.onClose(`gemini_close:${code}${reason ? ':' + reason : ''}`);
      };
      const onMessage = (msg: LiveServerMessage): void => {
        this.handleMessage(msg);
      };

      this.ai!.live
        .connect({
          model: this.model,
          // Cast: SDK type is strict; we built a valid config above.
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          config: config as any,
          callbacks: { onopen: onOpen, onmessage: onMessage, onerror: onError, onclose: onClose },
        })
        .then((session) => {
          this.session = session;
        })
        .catch(reject);
    });
  }

  /**
   * Schedule a deferred resume after goAway. The current session keeps\n   * working until ~3s before the deadline (per goAway.timeLeft), then we
   * swap to a fresh session using the last resumption handle.
   *
   * If goAway carries no parseable timeLeft we treat it as urgent and
   * resume immediately. Idempotent: a second goAway just refreshes the
   * timer (server may send updated estimates).
   */
  private scheduleResume(timeLeftMs: number | null, rawTimeLeft: string): void {
    if (this.reconnecting || this.closed) return;
    if (this.resumeTimer) {
      clearTimeout(this.resumeTimer);
      this.resumeTimer = null;
    }
    const delayMs = timeLeftMs === null
      ? 0
      : Math.max(0, timeLeftMs - _RESUME_SAFETY_MARGIN_MS);
    process.stderr.write(
      `[bridge] gemini goAway: timeLeft=${rawTimeLeft}; deferring resume by ${delayMs}ms\n`,
    );
    if (delayMs === 0) {
      void this.resumeAfterGoAway();
      return;
    }
    this.resumeTimer = setTimeout(() => {
      this.resumeTimer = null;
      void this.resumeAfterGoAway();
    }, delayMs);
    if (this.resumeTimer.unref) this.resumeTimer.unref();
  }

  /**
   * Transparent reconnect after goAway. The previous session is closed,
   * a new one opens with the last resumption handle, and buffered user
   * audio is flushed once the new session is up. Idempotent (re-entrant
   * goAway during reconnect is a no-op). Emits events.onClose only if
   * resumption itself fails — otherwise upstream sees no break.
   */
  private async resumeAfterGoAway(): Promise<void> {
    if (this.reconnecting || this.closed) return;
    const handle = this.lastResumptionHandle;
    if (!handle) {
      // Server didn't have time to send a handle (e.g. cap hit very early).
      // Nothing to resume to — fall through to real close.
      process.stderr.write('[bridge] gemini goAway with no resumption handle; closing\n');
      this.connected = false;
      try {
        this.session?.close();
      } catch (err) {
        process.stderr.write(`[bridge] gemini close after goAway threw: ${String(err)}\n`);
      }
      return;
    }
    this.reconnecting = true;
    this.connected = false;
    try {
      this.session?.close();
    } catch (err) {
      process.stderr.write(`[bridge] gemini old session close threw: ${String(err)}\n`);
    }
    this.session = null;
    try {
      await this.openSession(handle);
      this.flushPendingAudio();
    } catch (err) {
      const msg = errorMessage(err);
      process.stderr.write(`[bridge] gemini resume failed: ${msg}\n`);
      this.closed = true;
      this.events.onClose(`gemini_resume_failed:${msg}`);
    } finally {
      this.reconnecting = false;
    }
  }

  private flushPendingAudio(): void {
    if (this.pendingAudio.length === 0) return;
    if (!this.session || !this.connected) return;
    const chunks = this.pendingAudio.length;
    const bytes = this.pendingAudioBytes;
    for (const pcm of this.pendingAudio) {
      this.session.sendRealtimeInput({
        audio: { data: pcm.toString('base64'), mimeType: 'audio/pcm;rate=16000' },
      });
      this.inboundAudioBytes += pcm.length;
      this.inboundAudioChunks += 1;
    }
    this.pendingAudio = [];
    this.pendingAudioBytes = 0;
    process.stderr.write(
      `[bridge] gemini resume: flushed ${chunks} buffered chunks (${bytes}B)\n`,
    );
  }

  sendUserText(text: string): void {
    if (!this.session || !this.connected) return;
    this.session.sendClientContent({ turns: text, turnComplete: true });
  }

  sendUserAudio(pcm: Buffer): void {
    if (this.closed) return;
    if (!this.session || !this.connected) {
      if (this.reconnecting) {
        // Buffer up to ~5s of 16 kHz mono int16 (≈ 160 KB) so the user
        // doesn't lose speech across the resume gap. Drop on overflow —
        // a too-long gap means resume is failing anyway.
        const cap = 5 * 32 * 1024;
        if (this.pendingAudioBytes + pcm.length <= cap) {
          this.pendingAudio.push(pcm);
          this.pendingAudioBytes += pcm.length;
        }
        return;
      }
      if (this.inboundAudioChunks === 0) {
        process.stderr.write(
          `[bridge] gemini.sendUserAudio dropped: session=${!!this.session} connected=${this.connected}\n`,
        );
      }
      return;
    }
    this.inboundAudioBytes += pcm.length;
    this.inboundAudioChunks += 1;
    if (this.inboundAudioChunks === 1) {
      process.stderr.write(
        `[bridge] gemini: first user-audio chunk bytes=${pcm.length}\n`,
      );
    }
    this.session.sendRealtimeInput({
      audio: { data: pcm.toString('base64'), mimeType: 'audio/pcm;rate=16000' },
    });
  }

  sendNotification(text: string): void {
    // Gemini Live has no separate "system notification" channel; emit as a
    // user-text turn flagged so the model treats it as an out-of-band update.
    if (!this.session || !this.connected) return;
    this.session.sendClientContent({
      turns: `[system update] ${text}`,
      turnComplete: true,
    });
  }

  sendImage(mime: string, imageB64: string): void {
    if (!this.session || !this.connected) return;
    // Live API renamed `media` → `video`/`audio`/`text` (since 2025-Q4).
    // Sending the old field now closes the session with code 1007.
    this.session.sendRealtimeInput({ video: { data: imageB64, mimeType: mime } });
  }

  sendToolResponse(callId: string, name: string, message: string, error?: string): void {
    if (!this.session || !this.connected) return;
    const response = error ? { error } : { result: message };
    this.session.sendToolResponse({
      functionResponses: [{ id: callId, name, response }],
    });
  }

  async stop(): Promise<void> {
    if (this.metricsTimer) {
      clearInterval(this.metricsTimer);
      this.metricsTimer = null;
    }
    if (this.resumeTimer) {
      clearTimeout(this.resumeTimer);
      this.resumeTimer = null;
    }
    // Mark closed first so any in-flight onClose / resume short-circuits
    // and we don't fire a phantom onClose during normal teardown.
    this.closed = true;
    this.reconnecting = false;
    if (this.session) {
      try {
        this.session.close();
      } catch {
        // best-effort; provider may already be closed.
      }
    }
    this.session = null;
    this.connected = false;
    this.pendingAudio = [];
    this.pendingAudioBytes = 0;
  }

  private handleMessage(msg: LiveServerMessage): void {
    this.outboundMessages += 1;
    if (this.outboundMessages === 1) {
      // Dump the first message at full structure so we can see what
      // Gemini actually returns (text vs audio vs other).
      try {
        process.stderr.write(
          `[bridge] gemini first server msg keys=${Object.keys(msg).join(',')} ` +
            `serverContentKeys=${msg.serverContent ? Object.keys(msg.serverContent).join(',') : 'none'}\n`,
        );
      } catch (_e) { /* best-effort */ }
    }
    // Also dump the next 5 messages so we can see the structure of post-
    // setupComplete traffic — this is where audio/text would actually
    // come back, and a silent server is a real bug.
    if (this.outboundMessages > 1 && this.outboundMessages <= 6) {
      try {
        const sc = msg.serverContent;
        const modelTurn = sc?.modelTurn;
        const partsSummary = modelTurn?.parts
          ? modelTurn.parts.map((p) => {
              const k = Object.keys(p).join(',');
              if (p.inlineData) {
                return `inlineData{mime=${p.inlineData.mimeType ?? '?'},` +
                  `bytes=${p.inlineData.data ? Buffer.from(p.inlineData.data, 'base64').length : 0}}`;
              }
              return k || '<empty>';
            }).join('|')
          : '<no-parts>';
        process.stderr.write(
          `[bridge] gemini msg #${this.outboundMessages}: keys=${Object.keys(msg).join(',')} ` +
            `sc=${sc ? Object.keys(sc).join(',') : 'none'} ` +
            `parts=${partsSummary}\n`,
        );
      } catch (err) {
        process.stderr.write(`[bridge] gemini msg dump threw: ${String(err)}\n`);
      }
    }

    // Track resumption handles. Without these, goAway has nothing to
    // reconnect to and we'd fall through to a real close.
    if (msg.sessionResumptionUpdate) {
      const upd = msg.sessionResumptionUpdate;
      if (upd.resumable && upd.newHandle) {
        this.lastResumptionHandle = upd.newHandle;
      }
    }

    // GoAway: a server *advisory* warning that the session will be
    // terminated as ABORTED in `timeLeft` seconds. The current session
    // still works during that window. We defer the actual resume until
    // shortly before the deadline so the conversation stays uninterrupted
    // for as long as possible.
    if (msg.goAway) {
      const timeLeftMs = parseTimeLeftMs(msg.goAway.timeLeft);
      this.scheduleResume(timeLeftMs, msg.goAway.timeLeft ?? '?');
      return;
    }

    // Tool calls
    const toolCall = msg.toolCall;
    if (toolCall?.functionCalls) {
      for (const fc of toolCall.functionCalls) {
        const callId = fc.id ?? '';
        const name = fc.name ?? '';
        const args = (fc.args ?? {}) as Record<string, unknown>;
        if (name) this.events.onToolCall(callId, name, args);
      }
    }

    // Server content (text + audio)
    const sc = msg.serverContent;
    if (sc) {
      // Input transcript -> user_speech
      const it = sc.inputTranscription;
      if (it?.text) {
        this.events.onUserSpeech(it.text, Boolean(it.finished), undefined);
      }
      // Output transcript -> assistant text
      const ot = sc.outputTranscription;
      if (ot?.text) {
        this.outboundTextChunks += 1;
        this.events.onAssistantText(ot.text);
      }
      // Model turn parts: text and inline audio
      const parts = sc.modelTurn?.parts ?? [];
      for (const part of parts) {
        if (part.text) {
          this.outboundTextChunks += 1;
          this.events.onAssistantText(part.text);
        }
        if (part.inlineData?.data) {
          // 24 kHz mono int16 PCM, base64-encoded.
          const buf = Buffer.from(part.inlineData.data, 'base64');
          this.outboundAudioChunks += 1;
          if (this.outboundAudioChunks === 1) {
            process.stderr.write(
              `[bridge] gemini: first server audio chunk bytes=${buf.length} mime=${part.inlineData.mimeType ?? '?'}\n`,
            );
          }
          this.events.onAssistantAudio(buf);
        }
      }
    }
  }
}

function toFunctionDeclarations(tools: ToolDeclaration[]): FunctionDeclaration[] {
  // Strip `handler` before forwarding to Gemini.
  const out: FunctionDeclaration[] = [];
  for (const t of tools) {
    if (!t.parameters) continue;
    out.push({
      name: t.name,
      description: t.description,
      // Cast: ToolDeclaration.parameters is opaque Record<string, unknown>;
      // we trust the Python side to ship a valid JSON Schema.
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      parameters: t.parameters as any,
    });
  }
  return out;
}

function errorMessage(err: unknown): string {
  if (err && typeof err === 'object') {
    if ('message' in err && typeof (err as { message: unknown }).message === 'string') {
      return (err as { message: string }).message;
    }
    if ('error' in err) {
      return errorMessage((err as { error: unknown }).error);
    }
  }
  return String(err);
}

/**
 * Parse goAway.timeLeft into milliseconds.
 *
 * The field is a proto Duration serialized as a string. Common forms:
 *   "30s"        → 30_000
 *   "30.5s"      → 30_500
 *   "1m"         → 60_000
 *   "PT30S"      → 30_000  (ISO-8601, rare)
 * Returns null if the string can't be parsed; callers should treat that
 * as "deadline is imminent, resume now".
 */
function parseTimeLeftMs(s: string | undefined): number | null {
  if (!s) return null;
  const trimmed = s.trim();
  let m = trimmed.match(/^(\d+(?:\.\d+)?)\s*s$/i);
  if (m) return Math.round(parseFloat(m[1]!) * 1000);
  m = trimmed.match(/^(\d+(?:\.\d+)?)\s*ms$/i);
  if (m) return Math.round(parseFloat(m[1]!));
  m = trimmed.match(/^(\d+(?:\.\d+)?)\s*m$/i);
  if (m) return Math.round(parseFloat(m[1]!) * 60_000);
  m = trimmed.match(/^PT(\d+(?:\.\d+)?)S$/i);
  if (m) return Math.round(parseFloat(m[1]!) * 1000);
  return null;
}

export function createGeminiRealtime(
  events: RealtimeEvents,
  opts: CreateGeminiOptions = {},
): Realtime {
  return new GeminiRealtime(events, opts);
}
