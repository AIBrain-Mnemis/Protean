/**
 * Persistent bot-id helper.
 *
 * Loads `<userData>/bot-id.txt` if it exists; otherwise generates a fresh
 * `bot_<8hex>` and writes it. Per API §3.1 the id format must match
 * `^bot_[a-zA-Z0-9_]{1,32}$`.
 */

import { app } from 'electron';
import * as crypto from 'node:crypto';
import * as fs from 'node:fs';
import * as path from 'node:path';

const FILE_NAME = 'bot-id.txt';
const ID_PATTERN = /^bot_[a-zA-Z0-9_]{1,32}$/;

/**
 * Load the persistent bot id from disk, or generate + persist a new one.
 *
 * Pass an explicit `dataDir` for tests; production omits it and we use
 * Electron's `app.getPath('userData')`.
 */
export function loadOrCreateBotId(dataDir?: string): string {
  const dir = dataDir ?? app.getPath('userData');
  const file = path.join(dir, FILE_NAME);

  try {
    const raw = fs.readFileSync(file, 'utf-8').trim();
    if (ID_PATTERN.test(raw)) {
      return raw;
    }
    process.stderr.write(
      `[presence] discarding malformed bot-id ${JSON.stringify(raw)} at ${file}\n`,
    );
  } catch (err) {
    if (!isFileNotFound(err)) {
      process.stderr.write(`[presence] reading bot-id failed: ${String(err)}\n`);
    }
  }

  const fresh = generateBotId();
  try {
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(file, fresh + '\n', 'utf-8');
  } catch (err) {
    process.stderr.write(
      `[presence] persisting bot-id to ${file} failed: ${String(err)}; ` +
        'continuing with in-memory id (will regenerate next run)\n',
    );
  }
  return fresh;
}

export function generateBotId(): string {
  return `bot_${crypto.randomBytes(4).toString('hex')}`;
}

function isFileNotFound(err: unknown): boolean {
  return (
    typeof err === 'object' &&
    err !== null &&
    'code' in err &&
    (err as { code: unknown }).code === 'ENOENT'
  );
}
