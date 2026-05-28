/**
 * Presence module re-exports.
 */

export { loadOrCreateBotId, generateBotId } from './botId.js';
export { PresenceClient } from './client.js';
export type {
  Assignment,
  HeartbeatResponse,
  HeartbeatResult,
  PresenceConfig,
  ServerBotStatus,
} from './types.js';
export type { PresenceEvents } from './client.js';
