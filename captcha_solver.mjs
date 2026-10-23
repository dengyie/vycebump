#!/usr/bin/env node
/*
 * Bestcap CAPTCHA solver for vyce.cloud renewals.
 *
 * Connects to a Chrome DevTools Protocol endpoint, loads the Bestcap frame as a
 * top-level document, triggers the "I'm not a robot" check and reports the
 * token minted by the captcha service.
 *
 * Usage: node captcha_solver.mjs <cdp-http-endpoint> [sitekey] [parent-origin]
 * stdout: "TOKEN:<token>"  (single line)
 * stderr: "ERROR:<reason>" (on failure), exit code 1
 *
 * Requires Node 18+ (global WebSocket/fetch) and no third-party packages.
 */
'use strict';

const CDP_HTTP = (process.argv[2] || 'http://127.0.0.1:9222').replace(/\/+$/, '');
const SITEKEY = process.argv[3] || 'bc_site_b05a3b42a31232929d6194f3';
const PARENT_ORIGIN = process.argv[4] || 'https://vyce.cloud';
const SOLVE_TIMEOUT_MS = 45000;

function fail(reason, code = 1) {
  process.stderr.write(`ERROR:${reason}\n`);
  process.exit(code);
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function getBrowserWs() {
  const res = await fetch(`${CDP_HTTP}/json/version`, { signal: AbortSignal.timeout(5000) });
  if (!res.ok) throw new Error(`/json/version HTTP ${res.status}`);
  const info = await res.json();
  if (!info.webSocketDebuggerUrl) throw new Error('no webSocketDebuggerUrl');
  return info.webSocketDebuggerUrl;
}

const frameUrl =
  'https://captcha.vyceai.com/v1/frame?sitekey=' + encodeURIComponent(SITEKEY) +
  '&id=solver&parent=' + encodeURIComponent(PARENT_ORIGIN) +
  '&theme=auto&size=normal&action=&cdata=&refresh=auto';

async function main() {
  const browserWs = await getBrowserWs();
  const ws = new WebSocket(browserWs);
  const pending = new Map();
  let msgId = 1;
  let token = null;
  let sessionId = null;
  let settled = false;

  function send(method, params = {}, sid = sessionId) {
    const id = msgId++;
    const message = { id, method, params };
    if (sid) message.sessionId = sid;
    ws.send(JSON.stringify(message));
    return new Promise((resolve) => pending.set(id, resolve));
  }

  function finish(err, value) {
    if (settled) return;
    settled = true;
    if (err) return fail(err.message || String(err));
    process.stdout.write(`TOKEN:${value}\n`);
    process.exit(0);
  }

  ws.addEventListener('message', async (ev) => {
    let r;
    try { r = JSON.parse(ev.data); } catch { return; }
    if (r.id && pending.has(r.id)) { pending.get(r.id)(r); pending.delete(r.id); return; }
    if (token) return;

    if (r.method === 'Network.loadingFinished') {
      const body = await send('Network.getResponseBody', { requestId: r.params.requestId }, r.sessionId);
      const text = body && body.result && body.result.body;
      if (!text) return;
      try {
        const data = JSON.parse(text);
        if (data && data.status === 'ok' && typeof data.token === 'string') {
          token = data.token;
          finish(null, token);
        }
      } catch { /* not the verify response */ }
    }
  });

  ws.addEventListener('error', () => finish(new Error('CDP websocket error')));

  await new Promise((resolve, reject) => {
    ws.addEventListener('open', resolve, { once: true });
    ws.addEventListener('error', reject, { once: true });
    setTimeout(() => reject(new Error('CDP connect timeout')), 10000);
  });

  const created = await send('Target.createTarget', { url: 'about:blank' }, null);
  const targetId = created.result && created.result.targetId;
  if (!targetId) throw new Error('Target.createTarget failed');

  const attached = await send('Target.attachToTarget', { targetId, flatten: true }, null);
  sessionId = attached.result && attached.result.sessionId;
  if (!sessionId) throw new Error('Target.attachToTarget failed');

  await send('Page.enable');
  await send('Runtime.enable');
  await send('Network.enable');
  await send('Page.navigate', { url: frameUrl });

  // Wait until the checkbox exists and the widget has finished /v1/init.
  const readyDeadline = Date.now() + 20000;
  let ready = false;
  while (Date.now() < readyDeadline) {
    await sleep(500);
    const res = await send('Runtime.evaluate', {
      expression: `(document.getElementById("card")||{}).dataset?.state + "|" + !!document.getElementById("check")`,
      returnByValue: true,
    });
    const value = res && res.result && res.result.result && res.result.result.value;
    if (typeof value === 'string' && value.endsWith('|true')) { ready = true; break; }
  }
  if (!ready) throw new Error('captcha widget did not become ready');

  // Let /v1/init and the pow setup settle before the click.
  await sleep(1200);

  const clicked = await send('Runtime.evaluate', {
    expression: `(()=>{const b=document.getElementById("check"); if(!b) return false; b.click(); return true;})()`,
    returnByValue: true,
  });
  if (!(clicked && clicked.result && clicked.result.result && clicked.result.result.value)) {
    throw new Error('captcha checkbox not found');
  }

  const solveDeadline = Date.now() + SOLVE_TIMEOUT_MS;
  while (!settled && Date.now() < solveDeadline) await sleep(250);
  if (!settled) finish(new Error('captcha solve timeout'));
}

main().catch((err) => fail(err.message || String(err)));
