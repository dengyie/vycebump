#!/usr/bin/env node
/*
 * Bestcap CAPTCHA solver for vyce.cloud renewals.
 *
 * Connects to a Chrome DevTools Protocol endpoint, loads the Bestcap frame as a
 * top-level document, triggers the "I'm not a robot" check and reports the
 * token minted by the captcha service.
 *
 * With a 4th argument (server id) the script additionally submits the renewal
 * from inside the browser: it opens https://vyce.cloud, installs the session
 * cookie, and POSTs /api/servers/<id>/renew with {"captchaResponse": token}.
 * Renewing from the page itself keeps the request origin, user agent, TLS
 * fingerprint and egress IP identical to the official frontend, which is what
 * the platform's CAPTCHA validation expects.
 *
 * Usage: node captcha_solver.mjs <cdp-http-endpoint> [sitekey] [parent-origin] [server-id]
 * env:   VYCE_COOKIE  full cookie header, e.g. "__Host-vyce_sid=abc123"
 * stdout: "TOKEN:<token>"                   (always, on success)
 *         "RENEW:{...}"                     (renew mode: {status, body})
 * stderr: "ERROR:<reason>"                  (on failure), exit code 1
 *
 * Requires Node 18+ (global WebSocket/fetch) and no third-party packages.
 */
'use strict';

const CDP_HTTP = (process.argv[2] || 'http://127.0.0.1:9222').replace(/\/+$/, '');
const SITEKEY = process.argv[3] || 'bc_site_b05a3b42a31232929d6194f3';
const PARENT_ORIGIN = (process.argv[4] || 'https://vyce.cloud').replace(/\/+$/, '');
const RENEW_SERVER_ID = process.argv[5] || '';
const COOKIE_HEADER = process.env.VYCE_COOKIE || '';
const SOLVE_TIMEOUT_MS = 45000;
const RENEW_TIMEOUT_MS = 30000;

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

function parseCookies(header) {
  return String(header || '')
    .split(';')
    .map((part) => part.trim())
    .filter(Boolean)
    .map((part) => {
      const idx = part.indexOf('=');
      return idx > 0 ? { name: part.slice(0, idx).trim(), value: part.slice(idx + 1) } : null;
    })
    .filter((c) => c && c.name && c.value);
}

function valueOf(result) {
  return result && result.result && result.result.result && result.result.result.value;
}

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
  while (Date.now() < readyDeadline && !token) {
    await sleep(500);
    const value = valueOf(await send('Runtime.evaluate', {
      expression: `(document.getElementById("card")||{}).dataset?.state + "|" + !!document.getElementById("check")`,
      returnByValue: true,
    }));
    if (typeof value === 'string' && value.endsWith('|true')) { ready = true; break; }
  }
  if (!ready && !token) throw new Error('captcha widget did not become ready');

  if (!token) {
    // Let /v1/init and the pow setup settle before the click.
    await sleep(1200);
    const clicked = valueOf(await send('Runtime.evaluate', {
      expression: `(()=>{const b=document.getElementById("check"); if(!b) return false; b.click(); return true;})()`,
      returnByValue: true,
    }));
    if (!clicked) throw new Error('captcha checkbox not found');

    const solveDeadline = Date.now() + SOLVE_TIMEOUT_MS;
    while (!token && Date.now() < solveDeadline) await sleep(250);
    if (!token) throw new Error('captcha solve timeout');
  }

  process.stdout.write(`TOKEN:${token}\n`);

  if (RENEW_SERVER_ID) {
    const renew = await renewInBrowser(send, token);
    process.stdout.write(`RENEW:${renew}\n`);
  }

  settled = true;
  process.exit(0);
}

async function renewInBrowser(send, token) {
  const created = await send('Target.createTarget', { url: 'about:blank' }, null);
  const targetId = created.result && created.result.targetId;
  if (!targetId) return JSON.stringify({ status: 0, body: 'cannot open renewal tab' });

  const attached = await send('Target.attachToTarget', { targetId, flatten: true }, null);
  const sid = attached.result && attached.result.sessionId;
  if (!sid) return JSON.stringify({ status: 0, body: 'cannot attach renewal tab' });

  await send('Page.enable', {}, sid);
  await send('Runtime.enable', {}, sid);
  await send('Page.navigate', { url: `${PARENT_ORIGIN}/` }, sid);

  const loadDeadline = Date.now() + 20000;
  while (Date.now() < loadDeadline) {
    await sleep(300);
    const state = valueOf(await send('Runtime.evaluate', {
      expression: 'document.readyState', returnByValue: true,
    }, sid));
    if (state === 'complete') break;
  }

  for (const cookie of parseCookies(COOKIE_HEADER)) {
    await send('Network.setCookie', {
      url: `${PARENT_ORIGIN}/`,
      name: cookie.name,
      value: cookie.value,
      path: '/',
      secure: true,
      httpOnly: true,
    }, sid);
  }

  // Confirm the session works before spending the token.
  const authStatus = valueOf(await send('Runtime.evaluate', {
    expression: `fetch('/api/me',{credentials:'include'}).then(r=>r.status).catch(()=>0)`,
    awaitPromise: true,
    returnByValue: true,
  }, sid));
  if (authStatus !== 200) {
    return JSON.stringify({ status: 0, body: `browser session rejected the cookie (HTTP ${authStatus})` });
  }

  const expression =
    `(async()=>{try{` +
    `const r=await fetch('/api/servers/${encodeURIComponent(RENEW_SERVER_ID)}/renew',{` +
    `method:'POST',credentials:'include',` +
    `headers:{'Content-Type':'application/json','Accept':'application/json'},` +
    `body:JSON.stringify({captchaResponse:${JSON.stringify(token)}})});` +
    `const t=await r.text();` +
    `return JSON.stringify({status:r.status,body:t.slice(0,800)});` +
    `}catch(e){return JSON.stringify({status:0,body:String((e&&e.message)||e)});}})()`;

  const value = valueOf(await send('Runtime.evaluate', {
    expression, awaitPromise: true, returnByValue: true,
  }, sid));

  return value || JSON.stringify({ status: 0, body: 'renewal evaluation returned nothing' });
}

main().catch((err) => fail(err.message || String(err)));
