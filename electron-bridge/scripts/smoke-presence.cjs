/**
 * Smoke test for the presence client.
 *
 * Runs `npm run build` first, then drives PresenceClient against a fake
 * matchmaking server to verify the heartbeat loop, assignment dedup, and
 * shouldHangup path all work end-to-end.
 *
 * Run with: `node scripts/smoke-presence.cjs`
 */

const http = require('node:http');
const path = require('node:path');

async function main() {
  const distPath = path.resolve(__dirname, '..', 'dist', 'presence', 'client.js');
  let PresenceClient;
  try {
    ({ PresenceClient } = await import(`file://${distPath.replace(/\\/g, '/')}`));
  } catch (err) {
    console.error('Failed to import compiled presence client:', err.message);
    console.error('Did you run `npm run build` first?');
    process.exit(2);
  }

  const events = [];
  let heartbeatCount = 0;
  let stage = 'idle'; // idle -> reserved -> busy -> hangup -> done

  // Fake server that walks the bot through IDLE → RESERVED → BUSY → hangup.
  const server = http.createServer((req, res) => {
    let body = '';
    req.on('data', (c) => (body += c));
    req.on('end', () => {
      const url = req.url || '';
      if (req.method === 'POST' && url.endsWith('/heartbeat')) {
        heartbeatCount += 1;
        let response;
        if (stage === 'idle' && heartbeatCount >= 2) {
          stage = 'reserved';
          response = {
            serverStatus: 'RESERVED',
            assignment: {
              roomId: 'room_smoketest',
              userId: 'user_botside',
              userSig: 'usig_synthetic_xxxxxxxx',
              sdkAppId: 1400000000,
              displayName: 'Smoke',
              reservedAt: Date.now(),
            },
            shouldHangup: false,
            serverTime: Date.now(),
          };
        } else if (stage === 'reserved') {
          // Server keeps sending the same assignment until confirm; the
          // client must dedup.
          response = {
            serverStatus: 'RESERVED',
            assignment: {
              roomId: 'room_smoketest',
              userId: 'user_botside',
              userSig: 'usig_synthetic_xxxxxxxx',
              sdkAppId: 1400000000,
              displayName: 'Smoke',
              reservedAt: Date.now(),
            },
            shouldHangup: false,
            serverTime: Date.now(),
          };
        } else if (stage === 'busy') {
          response = {
            serverStatus: 'BUSY',
            assignment: null,
            shouldHangup: false,
            serverTime: Date.now(),
          };
        } else if (stage === 'hangup') {
          response = {
            serverStatus: 'IDLE',
            assignment: null,
            shouldHangup: true,
            serverTime: Date.now(),
          };
          stage = 'done';
        } else {
          response = {
            serverStatus: 'IDLE',
            assignment: null,
            shouldHangup: false,
            serverTime: Date.now(),
          };
        }
        res.writeHead(200, { 'content-type': 'application/json' });
        res.end(JSON.stringify(response));
        return;
      }

      if (req.method === 'POST' && url.includes('/confirm')) {
        stage = 'busy';
        res.writeHead(200, { 'content-type': 'application/json' });
        res.end(JSON.stringify({ ok: true, status: 'BUSY' }));
        return;
      }

      if (req.method === 'DELETE' && url.startsWith('/api/calls/')) {
        res.writeHead(200, { 'content-type': 'application/json' });
        res.end(JSON.stringify({ ok: true }));
        return;
      }

      res.writeHead(404);
      res.end();
    });
  });

  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const port = server.address().port;
  const baseUrl = `http://127.0.0.1:${port}`;

  const client = new PresenceClient(
    {
      baseUrl,
      botId: 'bot_smoketst',
      version: '0.1.0-smoke',
      heartbeatIntervalMs: 200, // fast for the smoke test
    },
    {
      onAssignment: (a) => {
        events.push(`assignment:${a.roomId}`);
        // Pretend TRTC enterRoom succeeded; confirm.
        client.confirm(a.roomId).then(
          () => events.push(`confirmed:${a.roomId}`),
          (e) => events.push(`confirm-failed:${e.message}`),
        );
        client.setLocalStatus('BUSY');
      },
      onShouldHangup: () => {
        events.push('hangup');
        client.setLocalStatus('IDLE');
        // Trigger the next stage to avoid endless heartbeat.
        stage = 'done';
      },
      onServerStatus: (s) => events.push(`server:${s}`),
      onOffline: (r) => events.push(`offline:${r}`),
    },
  );
  client.start();

  // Move to hangup phase after a confirm has happened.
  setTimeout(() => {
    if (stage === 'busy') stage = 'hangup';
  }, 1500);

  // Run the loop for ~3s.
  await new Promise((resolve) => setTimeout(resolve, 3000));
  client.stop();
  server.close();

  console.log('events:', events);
  // Assertions
  const assignmentCount = events.filter((e) => e.startsWith('assignment:')).length;
  const confirmCount = events.filter((e) => e.startsWith('confirmed:')).length;
  const hangupCount = events.filter((e) => e === 'hangup').length;

  let ok = true;
  function check(cond, msg) {
    if (!cond) {
      console.error('FAIL:', msg);
      ok = false;
    }
  }
  check(assignmentCount === 1, `expected exactly 1 assignment event (dedup), got ${assignmentCount}`);
  check(confirmCount === 1, `expected exactly 1 confirm, got ${confirmCount}`);
  check(hangupCount === 1, `expected exactly 1 hangup, got ${hangupCount}`);
  check(events.includes('server:RESERVED'), 'never saw server:RESERVED');
  check(events.includes('server:BUSY'), 'never saw server:BUSY');

  if (!ok) process.exit(1);
  console.log('smoke-presence: OK');
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
