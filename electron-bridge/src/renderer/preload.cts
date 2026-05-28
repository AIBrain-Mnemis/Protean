/**
 * Renderer preload — bridges Electron IPC to the page via contextBridge.
 *
 * Runs in a Node sandbox before the page script loads. Exposes a tiny,
 * typed surface as `window.proteanIPC` so the page never needs `require`
 * or direct access to Node APIs.
 *
 * .cts because preloads must be CommonJS even in a "type":"module"
 * package (Electron loads them via require()).
 *
 * Keep this file loose-typed (ts-nocheck): the renderer's index.ts
 * declares the typed shape via a `Window.proteanIPC` declaration. This
 * file is the trivial JS shim implementing that shape.
 */

// @ts-nocheck

const { contextBridge, ipcRenderer } = require('electron');

// Channel names (mirrored from src/ipc.ts; kept inline so this preload has
// zero TS import surface). Whenever you change these, update both files.
const CH = {
  micPcm: 'protean:mic:pcm',
  micError: 'protean:mic:error',
  micStarted: 'protean:mic:started',
  micStopped: 'protean:mic:stopped',
  screenFrame: 'protean:screen:frame',
  screenError: 'protean:screen:error',
  screenStarted: 'protean:screen:started',
  screenStopped: 'protean:screen:stopped',
  rendererReady: 'protean:renderer:ready',
  startMic: 'protean:main:start-mic',
  stopMic: 'protean:main:stop-mic',
  playPcm: 'protean:main:play-pcm',
  clearPlayback: 'protean:main:clear-playback',
  startScreen: 'protean:main:start-screen',
  stopScreen: 'protean:main:stop-screen',
  requestScreenshot: 'protean:main:request-screenshot',
  trtcProbe: 'protean:trtc:probe',
  trtcEnterRoom: 'protean:trtc:enter-room',
  trtcExitRoom: 'protean:trtc:exit-room',
  trtcEntered: 'protean:trtc:entered',
  trtcExited: 'protean:trtc:exited',
  trtcError: 'protean:trtc:error',
  trtcRemotePcm: 'protean:trtc:remote-pcm',
  trtcSendPcm: 'protean:trtc:send-pcm',
  trtcStartScreenShare: 'protean:trtc:start-screen-share',
  trtcStopScreenShare: 'protean:trtc:stop-screen-share',
  trtcScreenShareState: 'protean:trtc:screen-share-state',
};

// ── trtc-electron-sdk load probe ────────────────────────────────────────────
// Try to require the SDK from preload (CJS context with full Node access).
// Result is reported to main via IPC so it lands in bridge.log even if the
// renderer's own scripts haven't loaded yet.
function probeTrtcSdk() {
  let result;
  try {
    const trtc = require('trtc-electron-sdk');
    const TRTCCloud = trtc.default || trtc;
    let version = 'unknown';
    try {
      const cloud = TRTCCloud.getTRTCShareInstance
        ? TRTCCloud.getTRTCShareInstance()
        : new TRTCCloud();
      version = cloud.getSDKVersion ? cloud.getSDKVersion() : 'no-getSDKVersion';
    } catch (vErr) {
      // Class loaded but instantiation/version call failed; still useful info.
      result = {
        ok: false,
        stage: 'instantiate',
        error: String(vErr && vErr.message ? vErr.message : vErr),
        stack: vErr && vErr.stack ? String(vErr.stack).split('\n').slice(0, 8).join('\n') : null,
      };
    }
    if (!result) {
      result = {
        ok: true,
        version: version,
        topKeys: Object.keys(trtc),
      };
    }
  } catch (rErr) {
    result = {
      ok: false,
      stage: 'require',
      error: String(rErr && rErr.message ? rErr.message : rErr),
      stack: rErr && rErr.stack ? String(rErr.stack).split('\n').slice(0, 8).join('\n') : null,
    };
  }
  ipcRenderer.send(CH.trtcProbe, result);
}
// Run after preload's contextBridge setup so the probe doesn't block IPC setup.
setTimeout(probeTrtcSdk, 0);

// ── TRTC enter/exit (Cut 1) ─────────────────────────────────────────────────
// Preload owns the SDK lifecycle because the SDK requires both Node `require`
// (only available in preload, not the contextIsolated main world) AND a
// renderer-process Electron context (so we can't host it in main.ts). Main
// drives it via IPC; preload reports SDK callbacks back via IPC.
let trtcCloud = null;
let trtcListenersAttached = false;
// Cut 2: audio bridge.
let trtcAudioBridgeArmed = false;

function getTrtcCloud() {
  if (trtcCloud) return trtcCloud;
  const trtc = require('trtc-electron-sdk');
  const TRTCCloud = trtc.default || trtc;
  trtcCloud = TRTCCloud.getTRTCShareInstance
    ? TRTCCloud.getTRTCShareInstance()
    : new TRTCCloud();
  if (!trtcListenersAttached) {
    trtcListenersAttached = true;
    trtcCloud.on('onEnterRoom', (result) => {
      ipcRenderer.send(CH.trtcEntered, { result });
      if (result >= 0) {
        armAudioBridge();
      }
    });
    trtcCloud.on('onExitRoom', (reason) => {
      ipcRenderer.send(CH.trtcExited, { reason });
      disarmAudioBridge();
      // Reset screen-share state so the next room can start it cleanly.
      // The SDK auto-stops on exitRoom but our local flag would otherwise
      // block a re-start.
      trtcScreenShareActive = false;
    });
    trtcCloud.on('onError', (errcode, errmsg, extra) => {
      const info = {
        code: typeof errcode === 'number' ? errcode : -1,
        message: String(errmsg == null ? '' : errmsg),
      };
      if (extra != null) info.extra = String(extra);
      ipcRenderer.send(CH.trtcError, info);
    });
  }
  return trtcCloud;
}

// Cut 2: arm audio I/O once we're in a room.
//   - Disable SDK mic, enable custom audio capture (sendCustomAudioData path).
//   - Mute local speaker output (setAudioPlayoutVolume(0)) — we're a bot,
//     we never want to play remote audio through the host's speakers; the
//     audio frame callback still fires unaffected (it sits before playout).
//   - Subscribe to onMixedPlayAudioFrame to ferry remote-mixed PCM up to main,
//     where it's resampled and forwarded to the realtime LLM.
function armAudioBridge() {
  if (trtcAudioBridgeArmed || !trtcCloud) return;
  trtcAudioBridgeArmed = true;
  try {
    trtcCloud.stopLocalAudio();
  } catch (err) {
    ipcRenderer.send(CH.trtcError, {
      code: -1,
      message: 'stopLocalAudio threw: ' + (err && err.message ? err.message : String(err)),
    });
  }
  try {
    trtcCloud.enableCustomAudioCapture(true);
  } catch (err) {
    ipcRenderer.send(CH.trtcError, {
      code: -1,
      message: 'enableCustomAudioCapture threw: ' +
        (err && err.message ? err.message : String(err)),
    });
  }
  try {
    if (trtcCloud.setAudioPlayoutVolume) {
      trtcCloud.setAudioPlayoutVolume(0);
    }
  } catch (_e) { /* best-effort: still get the callback even if mute fails */ }
  let firstFrameLogged = false;
  try {
    trtcCloud.setAudioFrameCallback({
      onCapturedAudioFrame: null,
      onLocalProcessedAudioFrame: null,
      // Use per-user pre-mix audio (onPlayAudioFrame) instead of post-mix
      // (onMixedPlayAudioFrame). The mix step is where setAudioPlayoutVolume
      // is applied, so the post-mix path returns silence when volume=0,
      // even though the per-user audio is intact. We want both: silent
      // local playback AND a usable copy for Gemini.
      onMixedPlayAudioFrame: null,
      onMixedAllAudioFrame: null,
      onPlayAudioFrame: (frame, userId) => {
        if (!frame || !frame.data) return;
        const length = typeof frame.length === 'number'
          ? frame.length
          : (frame.data && typeof frame.data.length === 'number' ? frame.data.length : 0);
        if (length <= 0) return;
        if (!firstFrameLogged) {
          firstFrameLogged = true;
          let kind = 'unknown';
          try {
            if (Buffer.isBuffer(frame.data)) kind = 'Buffer';
            else if (frame.data instanceof Uint8Array) kind = 'Uint8Array';
            else if (frame.data instanceof ArrayBuffer) kind = 'ArrayBuffer';
            else kind = Object.prototype.toString.call(frame.data);
          } catch (_e) { /* keep 'unknown' */ }
          ipcRenderer.send(CH.trtcError, {
            code: 0,
            message: `INFO first remote frame: rate=${frame.sampleRate} ch=${frame.channel} bytes=${length} dataType=${kind} userId=${userId}`,
          });
        }
        // Allocate a fresh Uint8Array we control fully and copy bytes one
        // by one. This sidesteps every flavor of "weird N-API buffer that
        // doesn't structured-clone" we've seen from the native SDK.
        const out = new Uint8Array(length);
        const src = frame.data;
        try {
          if (Buffer.isBuffer(src) || src instanceof Uint8Array) {
            out.set(src.subarray(0, length));
          } else if (src instanceof ArrayBuffer) {
            out.set(new Uint8Array(src, 0, length));
          } else {
            for (let i = 0; i < length; i += 1) out[i] = src[i] & 0xff;
          }
        } catch (err) {
          ipcRenderer.send(CH.trtcError, {
            code: -1,
            message: 'remote-pcm copy threw: ' + (err && err.message ? err.message : String(err)),
          });
          return;
        }
        try {
          ipcRenderer.send(CH.trtcRemotePcm, {
            pcmB64: Buffer.from(out).toString('base64'),
            sampleRate: typeof frame.sampleRate === 'number' ? frame.sampleRate : 48000,
            channels: typeof frame.channel === 'number' ? frame.channel : 1,
          });
        } catch (err) {
          ipcRenderer.send(CH.trtcError, {
            code: -1,
            message: 'remote-pcm send threw: ' + (err && err.message ? err.message : String(err)),
          });
        }
      },
    });
  } catch (err) {
    ipcRenderer.send(CH.trtcError, {
      code: -1,
      message: 'setAudioFrameCallback threw: ' +
        (err && err.message ? err.message : String(err)),
    });
  }
}

function disarmAudioBridge() {
  if (!trtcAudioBridgeArmed || !trtcCloud) return;
  trtcAudioBridgeArmed = false;
  try {
    trtcCloud.setAudioFrameCallback({
      onCapturedAudioFrame: null,
      onLocalProcessedAudioFrame: null,
      onPlayAudioFrame: null,
      onMixedPlayAudioFrame: null,
      onMixedAllAudioFrame: null,
    });
  } catch (_e) { /* best-effort */ }
  try { trtcCloud.enableCustomAudioCapture(false); } catch (_e) { /* best-effort */ }
}

ipcRenderer.on(CH.trtcEnterRoom, (_e, p) => {
  try {
    const cloud = getTrtcCloud();
    const trtc = require('trtc-electron-sdk');
    const TRTCParams = trtc.TRTCParams;
    const TRTCAppScene = trtc.TRTCAppScene;
    // strRoomId path: numeric roomId=0 per SDK contract.
    const params = new TRTCParams(p.sdkAppId, p.userId, p.userSig, 0, p.strRoomId);
    cloud.enterRoom(params, TRTCAppScene.TRTCAppSceneAudioCall);
  } catch (err) {
    ipcRenderer.send(CH.trtcError, {
      code: -1,
      message: 'enterRoom threw: ' + (err && err.message ? err.message : String(err)),
    });
  }
});

ipcRenderer.on(CH.trtcExitRoom, () => {
  try {
    if (!trtcCloud) return;
    trtcCloud.exitRoom();
  } catch (err) {
    ipcRenderer.send(CH.trtcError, {
      code: -1,
      message: 'exitRoom threw: ' + (err && err.message ? err.message : String(err)),
    });
  }
});

// Cut 2: feed Gemini TTS PCM into the TRTC custom-audio path.
ipcRenderer.on(CH.trtcSendPcm, (_e, p) => {
  try {
    if (!trtcCloud || !trtcAudioBridgeArmed) return;
    const trtc = require('trtc-electron-sdk');
    const TRTCAudioFrame = trtc.TRTCAudioFrame;
    const TRTCAudioFrameFormat = trtc.TRTCAudioFrameFormat;
    // Main side base64-encodes (same Electron clone bug as remote-pcm).
    const buf = Buffer.from(p.pcmB64, 'base64');
    const ts = p.timestampMs && p.timestampMs > 0
      ? p.timestampMs
      : (trtcCloud.generateCustomPTS ? trtcCloud.generateCustomPTS() : Date.now());
    const frame = new TRTCAudioFrame(
      TRTCAudioFrameFormat.TRTCAudioFrameFormatPCM,
      buf,
      buf.length,
      p.sampleRate,
      1, // mono
      ts,
    );
    trtcCloud.sendCustomAudioData(frame);
  } catch (err) {
    ipcRenderer.send(CH.trtcError, {
      code: -1,
      message: 'sendCustomAudioData threw: ' +
        (err && err.message ? err.message : String(err)),
    });
  }
});

// ── Screen share (substream) ────────────────────────────────────────────────
// Bot publishes its primary screen on TRTCVideoStreamTypeSub so the caller
// sees what we're doing. The 1-based `displayIndex` from main process picks
// which physical display to share; null = primary. Live switching is
// supported by calling selectScreenCaptureTarget while capture is active.
let trtcScreenShareActive = false;

ipcRenderer.on(CH.trtcStartScreenShare, (_e, payload) => {
  const requestedIndex = payload && typeof payload === 'object'
    ? (payload as { displayIndex?: number | null }).displayIndex ?? null
    : null;
  if (!trtcCloud) {
    ipcRenderer.send(CH.trtcScreenShareState, {
      state: 'failed',
      detail: 'TRTC not initialized (no enterRoom yet)',
    });
    return;
  }
  try {
    const trtc = require('trtc-electron-sdk');
    const TRTCScreenCaptureSourceType = trtc.TRTCScreenCaptureSourceType;
    const TRTCVideoStreamType = trtc.TRTCVideoStreamType;

    // Enumerate sources. Use small thumbs/icons (we don't render them, just
    // need the metadata).
    const sources = trtcCloud.getScreenCaptureSources(64, 64, 32, 32);
    if (!sources || sources.length === 0) {
      ipcRenderer.send(CH.trtcScreenShareState, {
        state: 'failed',
        detail: 'getScreenCaptureSources returned no entries',
      });
      return;
    }

    // Build a primary-first list of Screen-typed sources so 1-based
    // requestedIndex maps consistently with main.ts's display ordering.
    const screens = sources.filter((s) =>
      s && s.type === TRTCScreenCaptureSourceType.TRTCScreenCaptureSourceTypeScreen
    );
    const primaryIdx = screens.findIndex((s) => s.isMainScreen);
    const ordered = primaryIdx >= 0
      ? [screens[primaryIdx], ...screens.filter((_, i) => i !== primaryIdx)]
      : screens.slice();

    let chosen;
    if (requestedIndex && requestedIndex >= 1 && requestedIndex <= ordered.length) {
      chosen = ordered[requestedIndex - 1];
    } else {
      // null / out-of-range → primary (or first screen / first source).
      chosen = ordered[0] ?? sources[0];
    }
    if (!chosen) {
      ipcRenderer.send(CH.trtcScreenShareState, {
        state: 'failed',
        detail: `no source matches displayIndex=${requestedIndex}`,
      });
      return;
    }

    const Rect = trtc.Rect;
    const TRTCScreenCaptureProperty = trtc.TRTCScreenCaptureProperty;
    const fullRect = Rect ? new Rect(0, 0, 0, 0) : { left: 0, top: 0, right: 0, bottom: 0 };
    const props = TRTCScreenCaptureProperty
      ? new TRTCScreenCaptureProperty(true, true, true, 0, true)
      : { enableCaptureMouse: true, enableHighLight: true, enableHighPerformance: true, highLightColor: 0, enableHighLightAnimation: true };

    if (trtcScreenShareActive) {
      // Already publishing — just retarget. TRTC supports live source
      // switching without stopping the substream.
      trtcCloud.selectScreenCaptureTarget(chosen, fullRect, props);
      ipcRenderer.send(CH.trtcScreenShareState, {
        state: 'started',
        detail: `retargeted to displayIndex=${requestedIndex ?? 'primary'} ` +
          `source=${chosen.sourceName ?? chosen.sourceId ?? '?'} ` +
          `mainScreen=${chosen.isMainScreen ? 'yes' : 'no'}`,
      });
      return;
    }

    trtcCloud.selectScreenCaptureTarget(chosen, fullRect, props);
    trtcCloud.startScreenCapture(null, TRTCVideoStreamType.TRTCVideoStreamTypeSub, null);
    trtcScreenShareActive = true;
    ipcRenderer.send(CH.trtcScreenShareState, {
      state: 'started',
      detail: `displayIndex=${requestedIndex ?? 'primary'} ` +
        `source=${chosen.sourceName ?? chosen.sourceId ?? '?'} ` +
        `type=${chosen.type} mainScreen=${chosen.isMainScreen ? 'yes' : 'no'}`,
    });
  } catch (err) {
    ipcRenderer.send(CH.trtcScreenShareState, {
      state: 'failed',
      detail: 'startScreenCapture threw: ' + (err && err.message ? err.message : String(err)),
    });
  }
});

ipcRenderer.on(CH.trtcStopScreenShare, () => {
  if (!trtcScreenShareActive || !trtcCloud) {
    ipcRenderer.send(CH.trtcScreenShareState, { state: 'stopped', detail: 'not-active' });
    return;
  }
  try {
    trtcCloud.stopScreenCapture();
    trtcScreenShareActive = false;
    ipcRenderer.send(CH.trtcScreenShareState, { state: 'stopped' });
  } catch (err) {
    ipcRenderer.send(CH.trtcScreenShareState, {
      state: 'failed',
      detail: 'stopScreenCapture threw: ' + (err && err.message ? err.message : String(err)),
    });
  }
});

contextBridge.exposeInMainWorld('proteanIPC', {
  // Renderer -> Main (publish-only)
  publish: {
    micPcm: (buf) => ipcRenderer.send(CH.micPcm, buf),
    micError: (msg) => ipcRenderer.send(CH.micError, msg),
    micStarted: (info) => ipcRenderer.send(CH.micStarted, info),
    micStopped: () => ipcRenderer.send(CH.micStopped, null),
    screenFrame: (frame) => ipcRenderer.send(CH.screenFrame, frame),
    screenError: (msg) => ipcRenderer.send(CH.screenError, msg),
    screenStarted: (info) => ipcRenderer.send(CH.screenStarted, info),
    screenStopped: () => ipcRenderer.send(CH.screenStopped, null),
    rendererReady: () => ipcRenderer.send(CH.rendererReady, null),
  },
  // Main -> Renderer (subscribe-only)
  subscribe: {
    startMic: (handler) => {
      const wrap = (_e, p) => handler(p);
      ipcRenderer.on(CH.startMic, wrap);
      return () => ipcRenderer.off(CH.startMic, wrap);
    },
    stopMic: (handler) => {
      const wrap = (_e, p) => handler(p);
      ipcRenderer.on(CH.stopMic, wrap);
      return () => ipcRenderer.off(CH.stopMic, wrap);
    },
    playPcm: (handler) => {
      const wrap = (_e, p) => handler(p);
      ipcRenderer.on(CH.playPcm, wrap);
      return () => ipcRenderer.off(CH.playPcm, wrap);
    },
    clearPlayback: (handler) => {
      const wrap = (_e, p) => handler(p);
      ipcRenderer.on(CH.clearPlayback, wrap);
      return () => ipcRenderer.off(CH.clearPlayback, wrap);
    },
    startScreen: (handler) => {
      const wrap = (_e, p) => handler(p);
      ipcRenderer.on(CH.startScreen, wrap);
      return () => ipcRenderer.off(CH.startScreen, wrap);
    },
    stopScreen: (handler) => {
      const wrap = (_e, p) => handler(p);
      ipcRenderer.on(CH.stopScreen, wrap);
      return () => ipcRenderer.off(CH.stopScreen, wrap);
    },
    requestScreenshot: (handler) => {
      const wrap = (_e, p) => handler(p);
      ipcRenderer.on(CH.requestScreenshot, wrap);
      return () => ipcRenderer.off(CH.requestScreenshot, wrap);
    },
  },
});
