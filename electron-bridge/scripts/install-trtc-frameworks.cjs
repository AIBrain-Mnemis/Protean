#!/usr/bin/env node
/*
 * Install trtc-electron-sdk native frameworks into Electron.app.
 *
 * The upstream `trtc-electron-sdk` package has its own postinstall that
 * does the same thing, but it relies on Electron already being on disk
 * when trtc's postinstall runs — and npm does not guarantee install order
 * between sibling deps. In practice it races and the frameworks end up
 * missing, then the bridge fails at runtime with:
 *
 *   dlopen(...trtc_electron_sdk.node): Library not loaded:
 *   @rpath/TXFFmpeg.framework/TXFFmpeg
 *
 * This script runs as our own postinstall after npm finishes installing
 * everything, so by the time it runs both Electron and trtc-electron-sdk
 * are guaranteed to be on disk. It is idempotent.
 *
 * macOS only — on Windows trtc-electron-sdk places its .dlls under
 * build/Release directly and Electron's loader picks them up without
 * a copy step. Linux is not supported by the upstream SDK.
 */

const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');

if (process.platform !== 'darwin') {
  process.exit(0);
}

const ROOT = path.resolve(__dirname, '..');
const ARCH = process.arch; // 'arm64' or 'x64'

const SRC = path.join(
  ROOT,
  'node_modules',
  'trtc-electron-sdk',
  'build',
  'mac-framework',
  ARCH,
);
const DST = path.join(
  ROOT,
  'node_modules',
  'electron',
  'dist',
  'Electron.app',
  'Contents',
  'Frameworks',
);

function log(msg) {
  process.stdout.write(`[install-trtc-frameworks] ${msg}\n`);
}

function die(msg) {
  process.stderr.write(`[install-trtc-frameworks] ERROR: ${msg}\n`);
  process.exit(1);
}

if (!fs.existsSync(SRC)) {
  // trtc-electron-sdk is an optionalDependency; if it didn't install (e.g.
  // unsupported platform/arch), silently no-op.
  log(`source not found, skipping: ${SRC}`);
  process.exit(0);
}

// Electron 42 ships without a postinstall hook — the binary downloads
// lazily on first `require('electron')`. Force that download here so the
// Frameworks dir exists before we copy into it.
if (!fs.existsSync(DST)) {
  log('triggering electron binary download (first require)…');
  try {
    require(path.join(ROOT, 'node_modules', 'electron'));
  } catch (err) {
    die(
      `electron lazy-install failed: ${String(err && err.message ? err.message : err)}\n` +
        'Try `cd electron-bridge && npx install-electron` and re-run install.',
    );
  }
}

if (!fs.existsSync(DST)) {
  die(
    `electron Frameworks dir still missing after download: ${DST}\n` +
      'Run `cd electron-bridge && npx install-electron --no` then `npm install`.',
  );
}

// Frameworks to ensure are present. Each is a .framework directory under SRC.
const FRAMEWORKS = fs
  .readdirSync(SRC, { withFileTypes: true })
  .filter((e) => e.isDirectory() && e.name.endsWith('.framework'))
  .map((e) => e.name);

if (FRAMEWORKS.length === 0) {
  die(`no .framework directories found under ${SRC}`);
}

for (const name of FRAMEWORKS) {
  const from = path.join(SRC, name);
  const to = path.join(DST, name);
  // Always rsync; it's a no-op if files are identical and preserves
  // symlinks inside the .framework (TXFFmpeg -> Versions/Current/TXFFmpeg).
  try {
    execFileSync('rsync', ['-a', '--delete', from + '/', to + '/'], {
      stdio: 'inherit',
    });
    log(`installed ${name}`);
  } catch (err) {
    die(`rsync failed for ${name}: ${String(err && err.message ? err.message : err)}`);
  }
}

log('done');
