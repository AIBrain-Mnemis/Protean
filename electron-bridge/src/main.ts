/**
 * Bridge entry point — Electron main process.
 *
 * 1. Wait for Electron app ready.
 * 2. Open a hidden BrowserWindow that hosts the renderer (mic + screen +
 *    speaker playback). Renderer is in dist/renderer/.
 * 3. Install lifecycle hooks (stdin EOF / signals -> exit).
 * 4. Start the WebSocket server on localhost.
 * 5. Emit the single bootstrap JSON line to stdout.
 *
 * Logs go to stderr so they never corrupt the bootstrap line.
 */

import { app, BrowserWindow, ipcMain, session as electronSession } from 'electron';
import * as path from 'node:path';
import { fileURLToPath } from 'node:url';

import { IPC } from './ipc.js';
import { installLifecycle, onShutdown, registerAppQuit } from './lifecycle.js';
import { startServer } from './wsServer.js';
import { BridgeOrchestrator } from './orchestrator.js';
import { loadOrCreateBotId } from './presence/index.js';
import { TrtcController } from './trtcController.js';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// We deliberately do NOT call app.requestSingleInstanceLock() — the Python
// supervisor is the source of truth for "one bridge at a time", and tests
// spawn many bridges back-to-back. Electron's lock is global per app name
// and would block legitimate sequential spawns.

// Bridge has no real GUI — keep alive even if all windows close (a noop,
// since we don't expose window controls anyway, but be explicit).
app.on('window-all-closed', () => {
  // Do nothing — bridge lifetime is controlled by stdin EOF / signals.
});

// Surface main-process errors to stderr.
app.on('render-process-gone', (_event, _webContents, details) => {
  process.stderr.write(`[bridge] render-process-gone: ${JSON.stringify(details)}\n`);
});

let hiddenWindow: BrowserWindow | null = null;

function createHiddenWindow(): BrowserWindow {
  const win = new BrowserWindow({
    show: false,
    width: 1,
    height: 1,
    skipTaskbar: true,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      // sandbox:false so the renderer can access getUserMedia +
      // getDisplayMedia without going through extra IPC hoops.
      sandbox: false,
      backgroundThrottling: false,
      preload: path.join(__dirname, 'renderer', 'preload.cjs'),
    },
  });
  void win.loadFile(path.join(__dirname, 'renderer', 'index.html'));
  return win;
}

function configurePermissions(): void {
  // Auto-grant media + display-capture permissions for our own renderer.
  // Bridge runs in a single-purpose process; there is no untrusted page.
  const ses = electronSession.defaultSession;
  ses.setPermissionRequestHandler((_webContents, permission, callback) => {
    // 'display-capture' isn't in Electron's narrow permission type but is
    // valid at runtime; cast for the comparison.
    const p = permission as string;
    if (p === 'media' || p === 'display-capture') {
      callback(true);
      return;
    }
    callback(false);
  });
  ses.setPermissionCheckHandler((_webContents, permission) => {
    const p = permission as string;
    return p === 'media' || p === 'display-capture';
  });
  // Display-capture: the realtime talker picks the display via the
  // `start_screen.display` argument; the session controller stashes the
  // requested 1-based index here just before triggering getDisplayMedia
  // in the renderer. If unset, fall back to the primary display so the
  // bridge and the macOS executor (`screencapture -D1`) stay aligned by
  // default.
  ses.setDisplayMediaRequestHandler((_request, callback) => {
    void (async () => {
      const { desktopCapturer, screen } = await import('electron');
      const sources = await desktopCapturer.getSources({ types: ['screen'] });
      const first = sources[0];
      if (!first) {
        callback({});
        return;
      }

      const allDisplays = screen.getAllDisplays();
      const primary = screen.getPrimaryDisplay();
      // Order: primary first, then the rest as Electron reports them.
      // This makes index 1 == primary, which aligns with macOS Quartz's
      // CGMainDisplayID convention. Indices ≥2 are best-effort.
      const ordered = [primary, ...allDisplays.filter((d) => d.id !== primary.id)];

      const requested = requestedDisplayIndex;
      let targetId: string | null = null;
      if (requested && requested >= 1 && requested <= ordered.length) {
        const display = ordered[requested - 1];
        if (display) targetId = String(display.id);
      }
      if (!targetId) targetId = String(primary.id);

      const chosen = sources.find((s) => s.display_id === targetId) ?? first;
      process.stderr.write(
        `[bridge] display-capture: requested_index=${requested ?? 'auto'} ` +
          `targetId=${targetId} chosen=${chosen.name}#${chosen.display_id}\n`,
      );
      callback({ video: chosen });
    })();
  });
}

// Module-level state read by `setDisplayMediaRequestHandler` above. The
// session controller calls `setRequestedDisplay(N)` immediately before
// asking the renderer to start capture; `N` is the 1-based index the
// realtime talker passed to `start_screen.display`. `null` means "auto"
// (handler picks the primary display).
let requestedDisplayIndex: number | null = null;
export function setRequestedDisplay(index: number | null | undefined): void {
  requestedDisplayIndex =
    typeof index === 'number' && Number.isFinite(index) && index >= 1
      ? Math.floor(index)
      : null;
}
export function getRequestedDisplay(): number | null {
  return requestedDisplayIndex;
}

async function bootstrap(): Promise<void> {
  installLifecycle();
  registerAppQuit(() => {
    if (hiddenWindow && !hiddenWindow.isDestroyed()) {
      hiddenWindow.destroy();
    }
    hiddenWindow = null;
    app.quit();
  });

  configurePermissions();

  // P3.14: log the trtc-electron-sdk load probe result from preload.
  // Lands in bridge.log so we can verify the SDK loads under our renderer
  // before writing the full media integration.
  ipcMain.on(IPC.trtcProbe, (_e, result) => {
    process.stderr.write(`[bridge] trtc-probe: ${JSON.stringify(result)}\n`);
  });

  hiddenWindow = createHiddenWindow();

  // Cut 1: install TRTC controller so the orchestrator can drive enter/exit.
  const trtcController = new TrtcController(hiddenWindow);
  trtcController.install();

  const { info, hooks } = await startServer({
    rendererWindow: hiddenWindow,
    trtc: trtcController,
  });

  // Optional: orchestrator boots only if a presence server URL is set.
  // Without it, bridge runs in transport.local_mock mode.
  let orchestrator: BridgeOrchestrator | null = null;
  const presenceUrl = process.env['PROTEAN_PRESENCE_URL'];
  if (presenceUrl) {
    try {
      const botId = loadOrCreateBotId();
      orchestrator = new BridgeOrchestrator(
        {
          baseUrl: presenceUrl,
          botId,
        },
        {
          pushEnvelope: hooks.pushEnvelope,
          onClientConnected: hooks.onClientConnected,
          onSessionStart: hooks.onSessionStart,
          onSessionEnd: hooks.onSessionEnd,
          trtc: trtcController,
        },
      );
      orchestrator.start();
      process.stderr.write(
        `[bridge] orchestrator started: presence=${presenceUrl} bot=${botId}\n`,
      );
    } catch (err) {
      process.stderr.write(
        `[bridge] orchestrator failed to start: ${String(err)}\n`,
      );
      orchestrator = null;
    }
  } else {
    process.stderr.write(
      '[bridge] PROTEAN_PRESENCE_URL not set; running without orchestrator\n',
    );
  }

  // Bootstrap line — exactly one JSON object on its own line.
  process.stdout.write(
    JSON.stringify({
      protocol: info.protocol,
      port: info.port,
      token: info.token,
      pid: info.pid,
    }) + '\n',
  );

  onShutdown(async () => {
    if (orchestrator) {
      try {
        await orchestrator.stop();
      } catch (err) {
        process.stderr.write(`[bridge] orchestrator stop failed: ${String(err)}\n`);
      }
    }
    try {
      trtcController.uninstall();
    } catch (err) {
      process.stderr.write(`[bridge] trtc uninstall failed: ${String(err)}\n`);
    }
    if (hiddenWindow && !hiddenWindow.isDestroyed()) {
      hiddenWindow.destroy();
    }
    hiddenWindow = null;
  });

  process.stderr.write(
    `[bridge] listening on 127.0.0.1:${info.port} (pid=${info.pid})\n`,
  );
}

app.whenReady().then(() => {
  bootstrap().catch((err) => {
    process.stderr.write(`[bridge] fatal: ${String(err)}\n`);
    process.exit(1);
  });
});
