/**
 * Mock transport — pretends a remote user joined the room shortly after
 * session_start. No real TRTC; lets us prove the IPC plumbing without
 * any media SDK.
 */

import { RoomStatePayload } from '../protocol.js';

export interface MockTransportEvents {
  onRoomState: (payload: RoomStatePayload) => void;
}

export interface MockTransport {
  enterRoom(callId: string): void;
  exitRoom(): void;
}

export function createMockTransport(events: MockTransportEvents): MockTransport {
  let timer: NodeJS.Timeout | null = null;
  let activeCallId = '';

  const clear = (): void => {
    if (timer) {
      clearTimeout(timer);
      timer = null;
    }
  };

  return {
    enterRoom(callId) {
      activeCallId = callId;
      clear();
      // Simulate "connected" ~50ms after enterRoom.
      timer = setTimeout(() => {
        events.onRoomState({
          state: 'connected',
          call_id: activeCallId,
          remote_users: [
            { user_id: 'mock-remote', has_audio: true, has_video: false, has_screen: false },
          ],
          network_quality: 4,
          detail: 'mock:connected',
        });
      }, 50);
    },
    exitRoom() {
      clear();
      const callId = activeCallId;
      activeCallId = '';
      events.onRoomState({
        state: 'ended',
        call_id: callId,
        remote_users: [],
        network_quality: 0,
        detail: 'mock:exit',
      });
    },
  };
}
