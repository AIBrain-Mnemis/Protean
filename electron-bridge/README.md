# protean-electron-bridge

Communication-layer subprocess for Protean. Owns:

- TRTC connection (`trtc-electron-sdk`) — audio, video, screen, room state
- Realtime LLM connection (Gemini Live) — WebSocket, PCM streaming, tool calls
- Presence heartbeat to the room state server

Protocol: type declarations live in
`electron-bridge/src/protocol.ts` and its Python mirror
`protean/channels/_protocol.py`. The two MUST stay in lockstep — the
MessageType union in both is diffed in CI.

## Layout

```
src/
  index.ts        # entry: bootstrap + WS server lifecycle
  protocol.ts     # IPC protocol types (mirror of protean/channels/_protocol.py)
dist/             # tsc output (gitignored)
```

## Build

```
cd electron-bridge
npm install
npm run build      # tsc -> dist/
npm run typecheck  # type-only check
```

### Windows ARM64

`trtc-electron-sdk` ships only an x64 Windows native; an ARM64 Electron
cannot load it (“trtc_electron_sdk.node is not a valid Win32
application” at runtime). On a Windows ARM64 host, force npm to install
the x64 Electron build before the first `npm install`:

```powershell
$env:npm_config_arch = "x64"
npm install
npm run build
```

If an arm64 install already exists, delete `node_modules` and
`package-lock.json` first — the lockfile pins target arch.

### Install internals (macOS)

`npm install` runs `scripts/install-trtc-frameworks.cjs` as a postinstall
step. The script:

1. Calls `require('electron')`, which lazy-downloads the Electron binary
   into `node_modules/electron/dist/Electron.app/` (Electron 42+ no
   longer downloads during its own postinstall).
2. `rsync`s the TRTC native frameworks
   (`TXFFmpeg.framework`, `TXSoundTouch.framework`) from
   `node_modules/trtc-electron-sdk/build/mac-framework/<arch>/` into
   `Electron.app/Contents/Frameworks/`.

Without step 2 the bridge fails at runtime with
`dlopen: Library not loaded: @rpath/TXFFmpeg.framework/TXFFmpeg`.
The trtc-electron-sdk package ships its own postinstall that tries to do
the same copy, but it races Electron's lazy download and frequently runs
before Electron is on disk. Our script is idempotent and handles the
ordering deterministically.

## Run

Spawned by the Python daemon, never run directly. For ad-hoc testing:

```
node dist/index.js   # writes bootstrap line to stdout, exits when stdin closes
```

## Versioning

This subproject moves with the Protean repo. The IPC protocol version
(`PROTOCOL_VERSION` in `src/protocol.ts`) and the bridge package `version`
in `package.json` are independent. See protocol section 9.
