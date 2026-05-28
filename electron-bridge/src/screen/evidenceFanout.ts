/**
 * Evidence frame fanout — symmetric drop to realtime + Python.
 *
 * For every screen frame the bridge captures, push it BOTH to the realtime
 * provider AND to Python (as an evidence_frame BINARY message). Drop is
 * symmetric: when backpressure forces a frame to be dropped, both sinks
 * drop the same frame.
 */

import { EVIDENCE_FRAME_TAG, type EvidenceFrameHeader } from '../protocol.js';

export interface EvidenceFanoutSinks {
  /**
   * Push to realtime provider as image. mime is `image/jpeg`.
   * Returns truthy on success (sink accepted). Mock realtime always returns
   * truthy; real Gemini may be temporarily disconnected.
   */
  toRealtime: (jpegB64: string) => void;
  /** Push BINARY frame to Python over WebSocket. */
  toPython: (binary: Buffer) => void;
}

const MAX_PENDING = 5;
let droppedCounter = 0;
let lastDropLog = 0;

export interface PushFrameOptions {
  jpeg: Buffer;
  source: 'remote_screen' | 'local_screen';
  ts: number;
  phash: number;
  w: number;
  h: number;
}

/**
 * One-shot frame push: emits to both sinks atomically (well, sequentially —
 * but both sinks are non-blocking).
 *
 * Backpressure detection is the caller's responsibility — pass `pendingCount`
 * so we can drop symmetrically when overloaded.
 */
export function pushFrame(
  sinks: EvidenceFanoutSinks,
  opts: PushFrameOptions,
  pendingCount: number,
): boolean {
  if (pendingCount > MAX_PENDING) {
    droppedCounter += 1;
    const now = Date.now();
    if (now - lastDropLog > 60_000) {
      process.stderr.write(
        `[bridge] evidence backpressure: dropped ${droppedCounter} frames in last minute\n`,
      );
      droppedCounter = 0;
      lastDropLog = now;
    }
    return false;
  }

  const header: EvidenceFrameHeader = {
    ts: opts.ts,
    source: opts.source,
    phash: opts.phash,
    w: opts.w,
    h: opts.h,
  };
  const headerJson = Buffer.from(JSON.stringify(header), 'utf-8');
  if (headerJson.byteLength > 65535) {
    process.stderr.write('[bridge] evidence header > 64 KiB; dropping\n');
    return false;
  }
  const lenBuf = Buffer.allocUnsafe(2);
  lenBuf.writeUInt16BE(headerJson.byteLength, 0);
  const tagBuf = Buffer.from([EVIDENCE_FRAME_TAG]);
  const binary = Buffer.concat([tagBuf, lenBuf, headerJson, opts.jpeg]);

  // Symmetric send. Both must run; one failing does not abort the other.
  try {
    sinks.toPython(binary);
  } catch (err) {
    process.stderr.write(`[bridge] evidence python sink error: ${String(err)}\n`);
  }
  try {
    sinks.toRealtime(opts.jpeg.toString('base64'));
  } catch (err) {
    process.stderr.write(`[bridge] evidence realtime sink error: ${String(err)}\n`);
  }
  return true;
}
