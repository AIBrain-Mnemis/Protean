/**
 * Realtime provider factory.
 *
 * Selection order:
 *   1. explicit `kind` arg
 *   2. PROTEAN_BRIDGE_REALTIME env (mock | gemini)
 *   3. auto: gemini when GEMINI_API_KEY is present, else mock
 *
 * The choice of provider AND its credentials are
 * bridge-side concerns. Python never selects a provider over IPC.
 */

import { createGeminiRealtime } from './gemini.js';
import { createMockRealtime } from './mock.js';
import type { Realtime, RealtimeEvents } from './types.js';

export type RealtimeKind = 'mock' | 'gemini';

export function resolveKind(envVar: string | undefined): RealtimeKind {
  const v = (envVar ?? '').toLowerCase().trim();
  if (v === 'gemini') return 'gemini';
  if (v === 'mock') return 'mock';
  if (v !== '') {
    process.stderr.write(
      `[bridge] unknown PROTEAN_BRIDGE_REALTIME=${v}; falling back to auto\n`,
    );
  }
  // Auto: prefer gemini if a key is configured, else fall back to mock so
  // tests/dev work without credentials.
  if (process.env['GEMINI_API_KEY']) {
    return 'gemini';
  }
  return 'mock';
}

export function createRealtime(events: RealtimeEvents, kind?: RealtimeKind): Realtime {
  const chosen: RealtimeKind = kind ?? resolveKind(process.env['PROTEAN_BRIDGE_REALTIME']);
  process.stderr.write(`[bridge] realtime provider: ${chosen}\n`);
  if (chosen === 'gemini') {
    return createGeminiRealtime(events);
  }
  return createMockRealtime(events);
}
