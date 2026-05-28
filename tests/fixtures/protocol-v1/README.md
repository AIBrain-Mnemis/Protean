# Protocol v1 golden fixtures

One JSON file per `MessageType` from `protean/channels/_protocol.py`.

Each fixture is a complete, valid wire envelope (one TEXT WebSocket message).
Both Python and TypeScript test suites load these to verify schema agreement.

## Coverage

| File | `type` | Direction |
|---|---|---|
| `hello.json` | `hello` | C→S |
| `welcome.json` | `welcome` | S→C |
| `session_start.json` | `session_start` | C→S |
| `session_started.json` | `session_started` | S→C |
| `session_end.json` | `session_end` | C→S |
| `session_ended.json` | `session_ended` | S→C |
| `assistant_text.json` | `assistant_text` | S→C |
| `user_speech.json` | `user_speech` | S→C |
| `realtime_error.json` | `realtime_error` | S→C |
| `tool_call.json` | `tool_call` | S→C |
| `tool_result.json` | `tool_result` | C→S |
| `notify.json` | `notify` | C→S |
| `send_text.json` | `send_text` | C→S |
| `send_image.json` | `send_image` | C→S |
| `room_state.json` | `room_state` | S→C |
| `screen_state.json` | `screen_state` | S→C |
| `recorder_state.json` | `recorder_state` | C→S |
| `ping.json` | `ping` | both |
| `pong.json` | `pong` | both |
| `error.json` | `error` | both |

## Conventions

- All values are synthetic; no real secrets or PII.
- `id` follows `msg_NNNN` numbering for readability — production ids are
  free-form sender-unique strings.
- `ts` is `0` for pre-session messages (`hello`, `welcome`, `room_state` while
  idle, etc.); otherwise monotonic seconds since `session_started`.
- `send_image.image_b64` is elided to keep the fixture small; tests should
  substitute real bytes when round-tripping.

## Evidence frames (BINARY) are not represented here

Evidence frames use a custom binary framing (1-byte tag + 2-byte BE header
length + JSON header + JPEG bytes) and cannot be serialized as a single JSON
file. See ``EvidenceFrameHeader`` in ``_protocol.py`` / ``protocol.ts`` for
the typed JSON header schema.
