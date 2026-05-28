/**
 * Lifecycle: bridge dies when its parent (Python daemon) closes our stdin.
 *
 * Same pattern as `protean/overlay/window.py`. We don't trust the parent to
 * always send us a clean shutdown signal — closing stdin is what we treat as
 * "die now". On detection we run all registered cleanup callbacks then exit.
 *
 * In the Electron main process, plain `process.exit()` skips Electron's
 * graceful shutdown. If `registerAppQuit` is called we route through
 * Electron's `app.quit()` instead, which destroys windows and tears down
 * the renderer cleanly.
 */

const cleanups: Array<() => Promise<void> | void> = [];
let appQuit: (() => void) | null = null;
let exiting = false;

export function onShutdown(fn: () => Promise<void> | void): void {
  cleanups.push(fn);
}

/** Register an Electron-aware exit callback (calls app.quit()). */
export function registerAppQuit(fn: () => void): void {
  appQuit = fn;
}

async function exitGracefully(code: number, reason: string): Promise<void> {
  if (exiting) return;
  exiting = true;
  process.stderr.write(`[bridge] shutdown: ${reason}\n`);

  // Cap cleanup at 400ms — total shutdown budget is ≤500ms.
  const deadline = Date.now() + 400;
  for (const fn of cleanups) {
    try {
      const remaining = deadline - Date.now();
      if (remaining <= 0) break;
      await Promise.race([
        Promise.resolve(fn()),
        new Promise((resolve) => setTimeout(resolve, remaining)),
      ]);
    } catch (err) {
      process.stderr.write(`[bridge] cleanup error: ${String(err)}\n`);
    }
  }

  if (appQuit) {
    try {
      appQuit();
    } catch (err) {
      process.stderr.write(`[bridge] app.quit error: ${String(err)}\n`);
    }
    // app.quit() is asynchronous; force exit shortly after to guarantee
    // we don't linger past the supervisor's timeout.
    setTimeout(() => process.exit(code), 200).unref();
    return;
  }
  process.exit(code);
}

export function installLifecycle(): void {
  // Parent closed stdin → die. We listen on 'close' (the stream itself is
  // gone) rather than 'end' (no more data) because Electron's stdin emits
  // 'end' after consuming any initial bytes, even if the parent's pipe is
  // still open. 'close' only fires on actual parent disconnect.
  //
  // We do NOT call resume(); we want stdin in paused mode so the heartbeat
  // bytes the supervisor writes don't trigger an early 'end'.
  process.stdin.on('close', () => void exitGracefully(0, 'stdin closed'));

  // OS signals → graceful exit.
  process.on('SIGTERM', () => void exitGracefully(0, 'SIGTERM'));
  process.on('SIGINT', () => void exitGracefully(0, 'SIGINT'));

  // Last resort: if uncaught, exit non-zero so supervisor can decide.
  process.on('uncaughtException', (err) => {
    process.stderr.write(`[bridge] uncaughtException: ${String(err)}\n`);
    void exitGracefully(1, 'uncaughtException');
  });
  process.on('unhandledRejection', (reason) => {
    process.stderr.write(`[bridge] unhandledRejection: ${String(reason)}\n`);
    void exitGracefully(1, 'unhandledRejection');
  });
}
