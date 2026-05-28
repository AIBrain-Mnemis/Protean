/**
 * WebSocket server: accepts exactly one client per bridge process.
 *
 * Token check happens at upgrade time (HTTP 401) so unauthorized clients
 * never see a WS handshake. Second concurrent client -> HTTP 409.
 *
 * URL format: ws://127.0.0.1:{port}/?token={token}.
 */

import * as crypto from 'node:crypto';
import * as http from 'node:http';
import { AddressInfo } from 'node:net';
import { URL } from 'node:url';
import { WebSocket, WebSocketServer } from 'ws';
import type { BrowserWindow } from 'electron';

import {
  Envelope,
  HelloPayload,
  PROTOCOL_VERSION,
  PRE_SESSION_TYPES,
  WelcomePayload,
} from './protocol.js';
import {
  EnvelopeError,
  errorEnvelope,
  makeEnvelope,
  parseEnvelope,
  TEXT_LIMIT_BYTES,
} from './envelope.js';
import { Session, BRIDGE_LOCAL_TOOLS } from './session.js';
import type { SessionLifecycleHooks } from './session.js';
import type { TrtcController } from './trtcController.js';

export interface BridgeServerInfo {
  port: number;
  token: string;
  pid: number;
  protocol: number;
}

/**
 * Hooks the BridgeOrchestrator uses to push envelopes and observe client
 * lifecycle. Returned alongside BridgeServerInfo from startServer().
 */
export interface BridgeHooks {
  /**
   * Push an envelope to the active WS client.
   * @returns true if delivered, false if no client is connected.
   */
  pushEnvelope: (env: Envelope) => boolean;
  /** Register a callback fired when a new WS client attaches. */
  onClientConnected: (cb: () => void) => void;
  /** Register a callback fired when a Session.start() succeeds. */
  onSessionStart: (cb: (callId: string) => void) => void;
  /** Register a callback fired when a Session.end() runs. */
  onSessionEnd: (cb: (reason: string) => void) => void;
}

/** Bridge package version, advertised in `welcome.bridge_version`. */
const BRIDGE_VERSION = '0.1.0';

const PING_INTERVAL_MS = 5_000;
const PONG_TIMEOUT_AFTER_MISSES = 3;

export interface StartOptions {
  /** Bind host. Localhost only. */
  host?: string;
  /** Bind port. 0 = OS-assigned. */
  port?: number;
  /** Hidden BrowserWindow used by the per-session controllers. */
  rendererWindow?: BrowserWindow;
  /** Optional TRTC driver. When present, sessions route audio through TRTC. */
  trtc?: TrtcController;
}

export async function startServer(
  opts: StartOptions = {},
): Promise<{ info: BridgeServerInfo; hooks: BridgeHooks }> {
  const host = opts.host ?? '127.0.0.1';
  const port = opts.port ?? 0;
  const token = crypto.randomBytes(16).toString('hex');
  const rendererWindow = opts.rendererWindow ?? null;
  const trtc = opts.trtc ?? null;

  const httpServer = http.createServer((_req, res) => {
    res.statusCode = 404;
    res.end();
  });

  const wss = new WebSocketServer({ noServer: true, maxPayload: TEXT_LIMIT_BYTES });

  let activeClient: WebSocket | null = null;
  const clientConnectedCbs: Array<() => void> = [];
  const sessionStartCbs: Array<(callId: string) => void> = [];
  const sessionEndCbs: Array<(reason: string) => void> = [];

  const sessionHooks: SessionLifecycleHooks = {
    onStart: (callId) => {
      for (const cb of sessionStartCbs) {
        try {
          cb(callId);
        } catch (err) {
          process.stderr.write(`[ws] session-start hook threw: ${String(err)}\n`);
        }
      }
    },
    onEnd: (reason) => {
      for (const cb of sessionEndCbs) {
        try {
          cb(reason);
        } catch (err) {
          process.stderr.write(`[ws] session-end hook threw: ${String(err)}\n`);
        }
      }
    },
  };

  const hooks: BridgeHooks = {
    pushEnvelope: (env) => {
      if (!activeClient || activeClient.readyState !== WebSocket.OPEN) return false;
      safeSend(activeClient, env);
      return true;
    },
    onClientConnected: (cb) => {
      clientConnectedCbs.push(cb);
    },
    onSessionStart: (cb) => {
      sessionStartCbs.push(cb);
    },
    onSessionEnd: (cb) => {
      sessionEndCbs.push(cb);
    },
  };

  httpServer.on('upgrade', (req, socket, head) => {
    const url = new URL(req.url ?? '/', `http://${host}`);
    const provided = url.searchParams.get('token') ?? '';

    // Constant-time compare; both buffers must be same length.
    const a = Buffer.from(token, 'utf-8');
    const b = Buffer.from(provided, 'utf-8');
    const okToken = a.length === b.length && crypto.timingSafeEqual(a, b);

    if (!okToken) {
      socket.write('HTTP/1.1 401 Unauthorized\r\nConnection: close\r\n\r\n');
      socket.destroy();
      return;
    }
    if (activeClient !== null) {
      socket.write('HTTP/1.1 409 Conflict\r\nConnection: close\r\n\r\n');
      socket.destroy();
      return;
    }

    wss.handleUpgrade(req, socket, head, (ws) => {
      activeClient = ws;
      attachClient(ws, rendererWindow, trtc, sessionHooks, () => {
        if (activeClient === ws) activeClient = null;
      });
      // Notify orchestrator (etc) that a fresh client is attached. Done
      // AFTER attach so that any envelopes the callback decides to push
      // land on a ready socket.
      for (const cb of clientConnectedCbs) {
        try {
          cb();
        } catch (err) {
          process.stderr.write(`[ws] client-connected hook threw: ${String(err)}\n`);
        }
      }
    });
  });

  await new Promise<void>((resolve, reject) => {
    httpServer.once('error', reject);
    httpServer.listen(port, host, () => resolve());
  });

  const addr = httpServer.address() as AddressInfo;
  const info: BridgeServerInfo = {
    port: addr.port,
    token,
    pid: process.pid,
    protocol: PROTOCOL_VERSION,
  };
  return { info, hooks };
}

function attachClient(
  ws: WebSocket,
  rendererWindow: BrowserWindow | null,
  trtc: TrtcController | null,
  lifecycleHooks: SessionLifecycleHooks,
  onClose: () => void,
): void {
  const session = new Session(
    {
      send: (env) => safeSend(ws, env),
      sendBinary: (buf) => safeSendBinary(ws, buf),
    },
    rendererWindow,
    lifecycleHooks,
    trtc,
  );

  let helloSeen = false;
  let pongTimer: NodeJS.Timeout | null = null;
  let missedPongs = 0;

  const cleanup = (): void => {
    if (pongTimer) {
      clearInterval(pongTimer);
      pongTimer = null;
    }
    if (session.isActive) session.end('client_closed');
    onClose();
  };

  ws.on('pong', () => {
    missedPongs = 0;
  });

  pongTimer = setInterval(() => {
    if (ws.readyState !== WebSocket.OPEN) return;
    missedPongs += 1;
    if (missedPongs > PONG_TIMEOUT_AFTER_MISSES) {
      process.stderr.write('[bridge] WS ping timeout; closing client\n');
      ws.terminate();
      return;
    }
    ws.ping();
  }, PING_INTERVAL_MS);

  ws.on('message', (raw, isBinary) => {
    if (isBinary) {
      // No inbound binary; this direction is server-to-client only.
      return;
    }
    let env: Envelope;
    try {
      env = parseEnvelope(raw as Buffer);
    } catch (err) {
      if (err instanceof EnvelopeError) {
        safeSend(ws, errorEnvelope(err, session.ts()));
        if (err.fatal) {
          ws.close(1011, err.code);
        }
        return;
      }
      safeSend(
        ws,
        errorEnvelope(
          { code: 'bad_envelope', message: String(err), fatal: true },
          session.ts(),
        ),
      );
      ws.close(1011, 'bad_envelope');
      return;
    }

    // Pre-session whitelist.
    if (!session.isActive && !PRE_SESSION_TYPES.has(env.type) && env.type !== 'session_start') {
      safeSend(
        ws,
        errorEnvelope(
          {
            code: 'not_in_session',
            message: env.type,
            fatal: false,
            inReplyTo: env.id,
          },
          session.ts(),
        ),
      );
      return;
    }

    if (env.type === 'hello') {
      handleHello(ws, env.payload as HelloPayload, env.id);
      helloSeen = true;
      return;
    }

    if (!helloSeen) {
      safeSend(
        ws,
        errorEnvelope(
          { code: 'bad_envelope', message: 'hello required first', fatal: true, inReplyTo: env.id },
          session.ts(),
        ),
      );
      ws.close(1011, 'no_hello');
      return;
    }

    session.dispatch(env);
  });

  ws.on('close', cleanup);
  ws.on('error', (err) => {
    process.stderr.write(`[bridge] ws error: ${String(err)}\n`);
  });
}

function handleHello(ws: WebSocket, hello: HelloPayload, _msgId: string): void {
  if (hello.protocol_version !== PROTOCOL_VERSION) {
    safeSend(
      ws,
      errorEnvelope({
        code: 'version_mismatch',
        message: `expected ${PROTOCOL_VERSION}, got ${hello.protocol_version}`,
        fatal: true,
      }),
    );
    ws.close(1011, 'version_mismatch');
    return;
  }

  const capabilities: string[] = [
    'realtime.gemini',
    'transport.local_mock',
    `tools.local:${BRIDGE_LOCAL_TOOLS.join(',')}`,
  ];

  const welcome: WelcomePayload = {
    bridge_version: BRIDGE_VERSION,
    capabilities,
  };
  safeSend(ws, makeEnvelope('welcome', welcome, 0));
}

function safeSend(ws: WebSocket, env: Envelope): void {
  if (ws.readyState !== WebSocket.OPEN) return;
  try {
    ws.send(JSON.stringify(env));
  } catch (err) {
    process.stderr.write(`[bridge] send error: ${String(err)}\n`);
  }
}

function safeSendBinary(ws: WebSocket, buf: Buffer): void {
  if (ws.readyState !== WebSocket.OPEN) return;
  try {
    ws.send(buf, { binary: true });
  } catch (err) {
    process.stderr.write(`[bridge] sendBinary error: ${String(err)}\n`);
  }
}
