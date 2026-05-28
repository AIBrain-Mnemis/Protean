/**
 * Session — owns the lifecycle from session_start to session_ended for one
 * connected client. Routes tool calls per the `handler` field on each tool
 * declaration.
 *
 * Provider choice + secrets are bridge-side env (PROTEAN_BRIDGE_REALTIME,
 * GEMINI_API_KEY).
 */

import type { BrowserWindow } from 'electron';

import {
  Envelope,
  RoomStatePayload,
  SessionStartPayload,
  ToolDeclaration,
} from './protocol.js';
import { errorEnvelope, makeEnvelope } from './envelope.js';
import { createRealtime } from './realtime/factory.js';
import type { Realtime } from './realtime/types.js';
import { createMockTransport, MockTransport } from './transport/localMock.js';
import { MicCaptureController } from './audio/captureController.js';
import { AudioPlaybackController } from './audio/playbackController.js';
import { ScreenCaptureController } from './screen/captureController.js';
import type { TrtcController } from './trtcController.js';
import { resampleInt16Mono } from './audio/resample.js';
import { setRequestedDisplay } from './main.js';

/** Tools the bridge handles itself (handler:"bridge"). Mirrors the bridge capability list. */
export const BRIDGE_LOCAL_TOOLS = [
  'start_screen',
  'stop_screen',
  'request_screenshot',
] as const;

const BRIDGE_LOCAL_SET = new Set<string>(BRIDGE_LOCAL_TOOLS);

/** Sample rates for the realtime LLM I/O legs. */
const REALTIME_INPUT_RATE = 16_000;  // Gemini wants 16 kHz mono int16
const REALTIME_OUTPUT_RATE = 24_000; // Gemini emits 24 kHz mono int16

export interface SessionEgress {
  send: (env: Envelope) => void;
  sendBinary: (buf: Buffer) => void;
}

/**
 * Optional lifecycle hooks. Used by BridgeOrchestrator to drive the
 * matchmaking server REST handshake (confirm / deleteCall) without
 * Session needing to know about presence.
 */
export interface SessionLifecycleHooks {
  /** Fires after session_started envelope is sent. */
  onStart?: (callId: string) => void;
  /** Fires when session terminates (after session_ended envelope). */
  onEnd?: (reason: string) => void;
}

let sessionCounter = 0;

interface PendingToolCall {
  realtimeCallId: string;
  name: string;
}

export class Session {
  private startedAt: number | null = null;
  private realtime: Realtime | null = null;
  private transport: MockTransport | null = null;
  private tools: ToolDeclaration[] = [];
  private toolByName = new Map<string, ToolDeclaration>();
  private sessionId = '';
  private pendingToolCalls = new Map<string, PendingToolCall>();

  private mic: MicCaptureController | null = null;
  private playback: AudioPlaybackController | null = null;
  private screen: ScreenCaptureController | null = null;
  /** Cut 2: when set, audio I/O flows over TRTC instead of the renderer mic/speaker. */
  private trtcRemoteUnsub: (() => void) | null = null;

  /**
   * @param egress - JSON + BINARY senders to the connected Python client.
   * @param rendererWindow - The hidden BrowserWindow that hosts the
   *   audio/screen capture controllers. Pass `null` in tests/headless mode;
   *   bridge-local tools degrade to stub responses without it.
   * @param lifecycleHooks - Optional callbacks for orchestrator integration
   *   (presence/REST handshake). Pass `{}` or omit when running standalone.
   * @param trtc - Optional TRTC controller. When provided, audio routes
   *   through TRTC (remote PCM → Gemini, Gemini PCM → TRTC sendCustomAudioData)
   *   instead of the renderer mic/speaker.
   */
  constructor(
    private readonly egress: SessionEgress,
    private readonly rendererWindow: BrowserWindow | null,
    private readonly lifecycleHooks: SessionLifecycleHooks = {},
    private readonly trtc: TrtcController | null = null,
  ) {}

  get isActive(): boolean {
    return this.startedAt !== null;
  }

  /** Monotonic seconds since session_start; 0 if not in session. */
  ts(): number {
    return this.startedAt === null ? 0 : (performance.now() - this.startedAt) / 1000;
  }

  start(payload: SessionStartPayload, msgId: string): void {
    if (this.isActive) {
      this.egress.send(
        errorEnvelope(
          {
            code: 'duplicate_session',
            message: 'session already active',
            fatal: false,
            inReplyTo: msgId,
          },
          this.ts(),
        ),
      );
      return;
    }

    // Validate handler:"bridge" tools against bridge's local capability.
    const realtime = payload.realtime;
    const tools = realtime?.tools ?? [];
    for (const t of tools) {
      if (t.handler === 'bridge' && !BRIDGE_LOCAL_SET.has(t.name)) {
        this.egress.send(
          errorEnvelope(
            {
              code: 'unsupported_local_tool',
              message: t.name,
              fatal: false,
              inReplyTo: msgId,
            },
            this.ts(),
          ),
        );
        return;
      }
    }

    this.startedAt = performance.now();
    this.tools = tools;
    this.toolByName.clear();
    for (const t of tools) this.toolByName.set(t.name, t);
    sessionCounter += 1;
    this.sessionId = `sess_${Date.now().toString(36)}_${sessionCounter}`;

    void this.bringUp(payload, msgId, realtime.system_instruction, tools);
  }

  private async bringUp(
    payload: SessionStartPayload,
    msgId: string,
    systemInstruction: string,
    tools: ToolDeclaration[],
  ): Promise<void> {
    this.realtime = createRealtime({
      onAssistantText: (text) => {
        this.egress.send(makeEnvelope('assistant_text', { text }, this.ts()));
      },
      onAssistantAudio: (pcm) => {
        // Cut 2: prefer TRTC. The TTS PCM is 24 kHz mono int16; sendCustomAudioData
        // accepts 16/24/32/44.1/48 kHz directly so we forward at native rate.
        if (this.trtc) {
          this.trtc.sendPcm(pcm, REALTIME_OUTPUT_RATE);
        } else {
          this.playback?.play(pcm);
        }
      },
      onUserSpeech: (text, final, audioDuration) => {
        const out: { text: string; final: boolean; audio_duration?: number } = {
          text,
          final,
        };
        if (audioDuration !== undefined) out.audio_duration = audioDuration;
        this.egress.send(makeEnvelope('user_speech', out, this.ts()));
      },
      onToolCall: (callId, name, args) => {
        this.handleRealtimeToolCall(callId, name, args);
      },
      onError: (code, message, recoverable) => {
        this.egress.send(
          makeEnvelope(
            'realtime_error',
            { code, message, recoverable },
            this.ts(),
          ),
        );
      },
      onClose: (reason) => {
        if (this.isActive) this.end(`realtime_closed:${reason}`);
      },
    });

    this.transport = createMockTransport({
      onRoomState: (rs: RoomStatePayload) => {
        this.egress.send(makeEnvelope('room_state', rs, this.ts()));
      },
    });

    try {
      await this.realtime.start(systemInstruction, tools);
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      this.egress.send(
        errorEnvelope(
          {
            code: 'realtime_start_failed',
            message,
            fatal: false,
            inReplyTo: msgId,
          },
          this.ts(),
        ),
      );
      this.startedAt = null;
      this.realtime = null;
      this.transport = null;
      this.tools = [];
      this.toolByName.clear();
      this.sessionId = '';
      return;
    }

    // Wire renderer-backed controllers if we have a window. Without one
    // (e.g. tests), bridge-local tools degrade to stub responses.
    if (this.rendererWindow && !this.rendererWindow.isDestroyed()) {
      this.screen = new ScreenCaptureController(this.rendererWindow, {
        toRealtime: (jpegB64) => {
          this.realtime?.sendImage('image/jpeg', jpegB64);
        },
        toPython: (binary) => {
          this.egress.sendBinary(binary);
        },
      });

      if (this.trtc) {
        // Cut 2: TRTC owns audio. Subscribe to remote PCM, resample to
        // Gemini's 16 kHz mono input, forward.
        let rmsAccum = 0;
        let rmsSamples = 0;
        let rmsPeak = 0;
        let rmsLastEmit = Date.now();
        this.trtcRemoteUnsub = this.trtc.onRemotePcm((frame) => {
          if (!this.realtime) return;
          // Frame is int16 LE; channels usually 1, sampleRate typically
          // 48000 (TRTC native) but we trust whatever the SDK reports.
          // Preload base64-encodes to dodge the structured-clone bug.
          const bytes = Buffer.from(frame.pcmB64, 'base64');
          const view = new Int16Array(
            bytes.buffer,
            bytes.byteOffset,
            Math.floor(bytes.byteLength / 2),
          );
          const monoSrc = frame.channels === 2
            ? downmixStereoToMono(view)
            : view;
          const resampled = resampleInt16Mono(monoSrc, frame.sampleRate, REALTIME_INPUT_RATE);
          // RMS / peak probe: tells us whether the audio we forward to
          // Gemini is actually quiet during silence (so VAD has a chance
          // to fire end-of-turn) or whether TRTC is sending comfort noise.
          for (let i = 0; i < resampled.length; i += 1) {
            const s = resampled[i] ?? 0;
            const a = s < 0 ? -s : s;
            if (a > rmsPeak) rmsPeak = a;
            rmsAccum += s * s;
          }
          rmsSamples += resampled.length;
          const now = Date.now();
          if (now - rmsLastEmit >= 5_000 && rmsSamples > 0) {
            const rms = Math.sqrt(rmsAccum / rmsSamples);
            // 16-bit max = 32767; pct uses peak / 32767.
            process.stderr.write(
              `[bridge] gemini-input audio: rms=${rms.toFixed(0)} peak=${rmsPeak} ` +
                `peakPct=${((rmsPeak / 32767) * 100).toFixed(1)}% samples=${rmsSamples}\n`,
            );
            rmsAccum = 0;
            rmsSamples = 0;
            rmsPeak = 0;
            rmsLastEmit = now;
          }
          // Send as Buffer (int16 LE, 16 kHz mono).
          const buf = Buffer.from(resampled.buffer, resampled.byteOffset, resampled.byteLength);
          this.realtime.sendUserAudio(buf);
        });
      } else {
        this.playback = new AudioPlaybackController(this.rendererWindow);
        this.mic = new MicCaptureController(this.rendererWindow, (pcm) => {
          this.realtime?.sendUserAudio(pcm);
        });
        // Auto-start mic so the assistant can hear the user.
        this.mic.start();
      }
    }

    this.transport.enterRoom(payload.accept_call_id);

    this.egress.send(
      makeEnvelope('session_started', { session_id: this.sessionId }, this.ts()),
    );

    // Fire lifecycle hook so the orchestrator can call presence.confirm.
    try {
      this.lifecycleHooks.onStart?.(payload.accept_call_id);
    } catch (err) {
      process.stderr.write(`[session] onStart hook threw: ${String(err)}\n`);
    }
  }

  end(reason: string): void {
    if (!this.isActive) return;
    this.transport?.exitRoom();
    void this.realtime?.stop();
    this.mic?.dispose();
    this.screen?.dispose();
    if (this.trtc) {
      try {
        this.trtc.stopScreenShare();
      } catch (err) {
        process.stderr.write(`[session] trtc.stopScreenShare threw: ${String(err)}\n`);
      }
    }
    this.playback?.clear();
    if (this.trtcRemoteUnsub) {
      try {
        this.trtcRemoteUnsub();
      } catch (err) {
        process.stderr.write(`[session] trtc unsubscribe threw: ${String(err)}\n`);
      }
      this.trtcRemoteUnsub = null;
    }
    this.mic = null;
    this.playback = null;
    this.screen = null;
    const summary = { duration: this.ts(), user_words: 0, assistant_words: 0 };
    this.egress.send(
      makeEnvelope(
        'session_ended',
        { reason, transcript_summary: summary },
        this.ts(),
      ),
    );
    this.startedAt = null;
    this.realtime = null;
    this.transport = null;
    this.tools = [];
    this.toolByName.clear();
    this.sessionId = '';
    this.pendingToolCalls.clear();

    // Fire lifecycle hook so the orchestrator can call presence.deleteCall.
    try {
      this.lifecycleHooks.onEnd?.(reason);
    } catch (err) {
      process.stderr.write(`[session] onEnd hook threw: ${String(err)}\n`);
    }
  }

  /**
   * Realtime model wants to invoke `name`. Route per the `handler` field.
   *  - python -> forward as IPC `tool_call`; Python replies via `tool_result`.
   *  - bridge -> handle locally.
   */
  private handleRealtimeToolCall(
    callId: string,
    name: string,
    args: Record<string, unknown>,
  ): void {
    const tool = this.toolByName.get(name);
    if (!tool) {
      this.realtime?.sendToolResponse(callId, name, '', `unknown tool: ${name}`);
      return;
    }

    if (tool.handler === 'bridge') {
      void this.handleBridgeLocalTool(callId, name, args);
      return;
    }

    const env = makeEnvelope(
      'tool_call',
      { call_id: callId, name, arguments: args },
      this.ts(),
    );
    this.pendingToolCalls.set(env.id, { realtimeCallId: callId, name });
    this.egress.send(env);
  }

  private async handleBridgeLocalTool(
    callId: string,
    name: string,
    args: Record<string, unknown>,
  ): Promise<void> {
    // Without a renderer (tests), all bridge-local tools stub.
    if (!this.screen) {
      this.replyBridgeStub(callId, name);
      return;
    }

    try {
      switch (name) {
        case 'start_screen': {
          const mode = (args['mode'] as string) ?? 'share';
          if (mode !== 'observe' && mode !== 'share') {
            this.realtime?.sendToolResponse(
              callId, name, '',
              `start_screen requires mode='observe' or 'share' (got ${mode})`,
            );
            return;
          }
          // Talker-selected display (1-based). Only meaningful for
          // mode='share'; observe captures the remote peer's stream.
          const rawDisplay = args['display'];
          const requestedDisplay =
            typeof rawDisplay === 'number' && Number.isFinite(rawDisplay) && rawDisplay >= 1
              ? Math.floor(rawDisplay)
              : null;
          if (mode === 'share') {
            setRequestedDisplay(requestedDisplay);
          } else {
            setRequestedDisplay(null);
          }
          this.screen.start(mode);
          // In share mode, also publish the bot's screen on TRTC's
          // substream so the caller's TRTC client renders it. observe
          // mode only feeds Gemini for vision; no TRTC publish.
          if (mode === 'share' && this.trtc) {
            this.trtc.startScreenShare(requestedDisplay);
          }
          const source = mode === 'observe' ? 'remote_screen' : 'local_screen';
          // Resolve the effective display_index for the envelope: explicit
          // request wins; otherwise default to 1 (primary, matches the
          // main-process handler's fallback). For observe mode we leave it
          // undefined since the executor shouldn't infer a local display.
          const effectiveDisplay =
            mode === 'share' ? (requestedDisplay ?? 1) : undefined;
          this.egress.send(
            makeEnvelope(
              'screen_state',
              {
                mode,
                source,
                fps: 1.0,
                resolution: [1280, 720] as [number, number],
                source_label: this.screen.currentSourceLabel,
                ...(effectiveDisplay !== undefined
                  ? { display_index: effectiveDisplay }
                  : {}),
              },
              this.ts(),
            ),
          );
          // Renderer reports the surface label asynchronously after
          // getDisplayMedia resolves. Re-emit screen_state when the label
          // becomes known so Python can update its share-target state.
          this.screen.onSourceLabelChange((label) => {
            if (!this.screen) return;
            this.egress.send(
              makeEnvelope(
                'screen_state',
                {
                  mode,
                  source,
                  fps: 1.0,
                  resolution: [1280, 720] as [number, number],
                  source_label: label,
                  ...(effectiveDisplay !== undefined
                    ? { display_index: effectiveDisplay }
                    : {}),
                },
                this.ts(),
              ),
            );
          });
          this.realtime?.sendToolResponse(callId, name, `screen capture started in ${mode} mode`);
          return;
        }
        case 'stop_screen': {
          this.screen.onSourceLabelChange(null);
          this.screen.stop();
          setRequestedDisplay(null);
          if (this.trtc) {
            this.trtc.stopScreenShare();
          }
          this.egress.send(
            makeEnvelope(
              'screen_state',
              {
                mode: 'off',
                source: 'null',
                fps: 0,
                resolution: [0, 0] as [number, number],
                source_label: '',
              },
              this.ts(),
            ),
          );
          this.realtime?.sendToolResponse(callId, name, 'screen capture stopped');
          return;
        }
        case 'request_screenshot': {
          if (this.screen.currentMode === 'off') {
            this.realtime?.sendToolResponse(
              callId, name, '',
              'request_screenshot requires an active screen capture (call start_screen first)',
            );
            return;
          }
          const ok = await this.screen.requestSingleshot();
          this.realtime?.sendToolResponse(
            callId,
            name,
            ok ? 'screenshot captured' : 'screenshot timeout',
          );
          return;
        }
        default:
          this.replyBridgeStub(callId, name);
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      this.realtime?.sendToolResponse(callId, name, '', msg);
    }
  }

  private replyBridgeStub(callId: string, name: string): void {
    this.realtime?.sendToolResponse(
      callId,
      name,
      `${name} acknowledged (no renderer; stub)`,
    );
  }

  /** Handle a parsed envelope from the client (Python). */
  dispatch(env: Envelope): void {
    switch (env.type) {
      case 'session_start':
        this.start(env.payload as SessionStartPayload, env.id);
        return;
      case 'session_end':
        this.end((env.payload as { reason?: string }).reason ?? 'client');
        return;
      case 'notify':
        this.realtime?.sendNotification((env.payload as { text: string }).text);
        return;
      case 'send_text':
        this.realtime?.sendUserText((env.payload as { text: string }).text);
        return;
      case 'send_image': {
        const p = env.payload as { mime: string; image_b64: string };
        this.realtime?.sendImage(p.mime, p.image_b64);
        return;
      }
      case 'tool_result': {
        const payload = env.payload as {
          in_reply_to: string;
          call_id: string;
          name: string;
          message: string;
          error?: string;
        };
        const pending = this.pendingToolCalls.get(payload.in_reply_to);
        if (!pending) return;
        this.pendingToolCalls.delete(payload.in_reply_to);
        this.realtime?.sendToolResponse(
          pending.realtimeCallId,
          pending.name,
          payload.message,
          payload.error,
        );
        return;
      }
      case 'recorder_state':
        return;
      case 'ping':
        this.egress.send(makeEnvelope('pong', { in_reply_to: env.id }, this.ts()));
        return;
      case 'hello':
      case 'welcome':
      case 'session_started':
      case 'session_ended':
      case 'assistant_text':
      case 'user_speech':
      case 'realtime_error':
      case 'tool_call':
      case 'room_state':
      case 'screen_state':
      case 'pong':
      case 'error':
        return;
      default:
        ((_x: never) => undefined)(env.type as never);
    }
  }
}

/** Average L+R channels into a single mono int16 array. */
function downmixStereoToMono(stereo: Int16Array): Int16Array {
  const out = new Int16Array(stereo.length >> 1);
  for (let i = 0, j = 0; i < out.length; i += 1, j += 2) {
    const a = stereo[j] ?? 0;
    const b = stereo[j + 1] ?? 0;
    out[i] = (a + b) >> 1;
  }
  return out;
}
