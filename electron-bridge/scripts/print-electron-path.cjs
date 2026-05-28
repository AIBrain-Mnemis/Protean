/**
 * Print the absolute path to the Electron binary that the npm `electron`
 * package shipped, then exit.
 *
 * The `electron` package, when imported from a regular Node script, exports
 * the path string to its bundled binary. From inside Electron itself it
 * exports the API surface. The Python supervisor needs the path so it can
 * spawn the right binary as a subprocess.
 *
 *   $ node scripts/print-electron-path.js
 *   /path/to/electron-bridge/node_modules/electron/dist/electron
 */

const electronPath = require('electron');

if (typeof electronPath !== 'string') {
  // We're running inside Electron itself by mistake; complain loudly.
  process.stderr.write('print-electron-path must be run with `node`, not `electron`.\n');
  process.exit(2);
}

process.stdout.write(electronPath + '\n');
