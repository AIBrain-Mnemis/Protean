/**
 * Copy non-TS renderer assets (HTML, CSS, etc.) from src/renderer/ to dist/renderer/.
 *
 * tsc only emits .js/.d.ts; HTML is consumed by Electron at runtime.
 * Run as `npm run build` after `tsc`.
 */
const fs = require('node:fs');
const path = require('node:path');

const projectRoot = path.dirname(__dirname);
const srcDir = path.join(projectRoot, 'src', 'renderer');
const dstDir = path.join(projectRoot, 'dist', 'renderer');

if (!fs.existsSync(srcDir)) {
  process.exit(0);
}
fs.mkdirSync(dstDir, { recursive: true });

const exts = new Set(['.html', '.css', '.svg']);
let copied = 0;
for (const entry of fs.readdirSync(srcDir, { withFileTypes: true })) {
  if (!entry.isFile()) continue;
  const ext = path.extname(entry.name).toLowerCase();
  if (!exts.has(ext)) continue;
  fs.copyFileSync(path.join(srcDir, entry.name), path.join(dstDir, entry.name));
  copied += 1;
}
process.stderr.write(`[copy-renderer-assets] copied ${copied} files\n`);
