/**
 * Mock realtime provider — echoes input as assistant text.
 *
 * Used for tests and dev when GEMINI_API_KEY is unavailable. Implements the
 * shared `Realtime` interface so `session.ts` doesn't care which one is
 * active.
 */

import type { Realtime, RealtimeEvents } from './types.js';
import type { ToolDeclaration } from '../protocol.js';

class MockRealtime implements Realtime {
  private started = false;
  private tools: ToolDeclaration[] = [];
  private toolCallCounter = 0;

  constructor(private readonly events: RealtimeEvents) {}

  async start(_systemInstruction: string, tools: ToolDeclaration[]): Promise<void> {
    this.started = true;
    this.tools = tools;
  }

  sendUserText(text: string): void {
    if (!this.started) return;
    // Test backdoor: a user_text starting with "__tool__:NAME:JSON_ARGS"
    // simulates the realtime model deciding to call NAME with the given
    // arguments. Lets tests exercise the full tool routing path without a
    // real model.
    const toolMatch = text.match(/^__tool__:([^:]+):(.*)$/);
    if (toolMatch) {
      const name = toolMatch[1] ?? '';
      const argsStr = toolMatch[2] ?? '{}';
      let args: Record<string, unknown> = {};
      try {
        args = JSON.parse(argsStr);
      } catch {
        args = { __raw: argsStr };
      }
      this.toolCallCounter += 1;
      const callId = `mock-call-${this.toolCallCounter}`;
      queueMicrotask(() => this.events.onToolCall(callId, name, args));
      return;
    }
    queueMicrotask(() => this.events.onAssistantText(`echo: ${text}`));
  }

  sendUserAudio(_pcm: Buffer): void {
    // Mock has nowhere to send audio; ignore. Tests may peek at this if needed.
  }

  sendNotification(text: string): void {
    if (!this.started) return;
    queueMicrotask(() => this.events.onAssistantText(`notified: ${text}`));
  }

  sendImage(_mime: string, _imageB64: string): void {
    if (!this.started) return;
    queueMicrotask(() => this.events.onAssistantText('image received'));
  }

  sendToolResponse(callId: string, name: string, message: string, error?: string): void {
    // Mock realtime acknowledges the tool reply by emitting an assistant turn
    // describing what it received. Tests can assert on this.
    if (!this.started) return;
    const tail = error ? `error=${error}` : `message=${message}`;
    queueMicrotask(() =>
      this.events.onAssistantText(`tool ${name} (call_id=${callId}) -> ${tail}`),
    );
  }

  async stop(): Promise<void> {
    this.started = false;
    this.tools = [];
  }
}

export function createMockRealtime(events: RealtimeEvents): Realtime {
  return new MockRealtime(events);
}
