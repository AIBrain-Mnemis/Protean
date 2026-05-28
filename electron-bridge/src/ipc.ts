/**
 * Electron IPC channel names + payload shapes.
 *
 * Used by main + preload + renderer. Keep this file dependency-free so it
 * can be imported from any process boundary.
 *
 * Wire convention: every channel is unidirectional and named
 * `protean:<area>:<verb>`. Renderer publishes via `proteanIPC` (exposed
 * by preload); main publishes via `webContents.send`.
 */

export const IPC = {
  // Renderer -> Main
  micPcm: 'protean:mic:pcm', // ArrayBuffer of int16 LE (16 kHz mono)
  micError: 'protean:mic:error', // string
  micStarted: 'protean:mic:started', // {sampleRate: number}
  micStopped: 'protean:mic:stopped', // null

  screenFrame: 'protean:screen:frame', // {jpegB64: string, w: number, h: number, ts: number}
  screenError: 'protean:screen:error', // string
  screenStarted: 'protean:screen:started', // {sourceLabel: string}
  screenStopped: 'protean:screen:stopped', // null

  rendererReady: 'protean:renderer:ready', // null

  // Main -> Renderer
  startMic: 'protean:main:start-mic', // null
  stopMic: 'protean:main:stop-mic', // null
  playPcm: 'protean:main:play-pcm', // ArrayBuffer of int16 LE (24 kHz mono)
  clearPlayback: 'protean:main:clear-playback', // null

  startScreen: 'protean:main:start-screen', // {fps: number, maxDim: number}
  stopScreen: 'protean:main:stop-screen', // null
  requestScreenshot: 'protean:main:request-screenshot', // null

  // SDK probe: preload reports whether trtc-electron-sdk
  // loads successfully under our renderer context. One-shot at startup.
  trtcProbe: 'protean:trtc:probe', // {ok, version?, error?, stack?}

  // TRTC enter/exit. Main drives, renderer owns the SDK.
  // Main -> Renderer
  trtcEnterRoom: 'protean:trtc:enter-room', // TrtcEnterRoomPayload
  trtcExitRoom: 'protean:trtc:exit-room', // null
  trtcSendPcm: 'protean:trtc:send-pcm', // TrtcSendPcmPayload
  trtcStartScreenShare: 'protean:trtc:start-screen-share', // TrtcStartScreenSharePayload
  trtcStopScreenShare: 'protean:trtc:stop-screen-share', // null
  // Renderer -> Main
  trtcEntered: 'protean:trtc:entered', // {result: number}  (>=0 ms = success, <0 = error)
  trtcExited: 'protean:trtc:exited', // {reason: number}
  trtcError: 'protean:trtc:error', // {code: number, message: string, extra?: string}
  trtcRemotePcm: 'protean:trtc:remote-pcm', // TrtcRemotePcmPayload
  trtcScreenShareState: 'protean:trtc:screen-share-state', // {state: 'started'|'stopped'|'failed', detail?: string}
} as const;

export interface TrtcEnterRoomPayload {
  sdkAppId: number;
  userId: string;
  userSig: string;
  /** Always use string roomId; pass numeric roomId=0 to TRTC. */
  strRoomId: string;
}

export interface TrtcStartScreenSharePayload {
  /**
   * 1-based physical display index to share (primary-first ordering, matches
   * `screen.getAllDisplays()` after primary is moved to front). Null/omitted
   * means "primary display". The renderer matches this against TRTC's
   * `getScreenCaptureSources()` to pick the right native source. Live
   * switching is supported: calling start again while already active just
   * re-targets to the new display via `selectScreenCaptureTarget`.
   */
  displayIndex?: number | null;
}

export interface TrtcSendPcmPayload {
  /**
   * Int16 LE PCM mono, base64-encoded. Same Electron IPC structured-clone
   * issue as TrtcRemotePcmPayload — strings round-trip; buffers don't.
   */
  pcmB64: string;
  sampleRate: number;
  /** Capture timestamp (ms); preload uses generateCustomPTS if 0. */
  timestampMs: number;
}

export interface TrtcRemotePcmPayload {
  /**
   * Int16 LE PCM, base64-encoded. We tried passing ArrayBuffer-slices and
   * Uint8Arrays but Electron's preload structured-clone path rejected both
   * with the native SDK's frame buffers. Strings round-trip cleanly.
   */
  pcmB64: string;
  sampleRate: number;
  channels: number;
}

export interface MicStartedPayload {
  sampleRate: number;
}

export interface ScreenFramePayload {
  /** Base64-encoded JPEG. */
  jpegB64: string;
  w: number;
  h: number;
  /** monotonic ms when frame was captured (renderer's perf.now()). */
  ts: number;
}

export interface StartScreenOptions {
  fps: number;
  maxDim: number;
  /** JPEG quality 0-100. */
  quality: number;
}
