#!/usr/bin/env node
/*
 * Bestcap CAPTCHA solver for vyce.cloud renewals.
 *
 * Connects to a Chrome DevTools Protocol endpoint, opens vyce.cloud, renders the
 * Bestcap widget through the site's own library, triggers the "I'm not a robot"
 * check and reports the token minted by the captcha service.
 *
 * The captcha frame refuses to run as a top-level document, so it must be
 * embedded by the page exactly like the official frontend does.
 *
 * With a 4th argument (server id) the script additionally submits the renewal
 * from inside the browser: it POSTs /api/servers/<id>/renew with
 * {"captchaResponse": token} from the already-authenticated page.
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
// 验证码 frame 偶发卡在 /v1/init 不返回，重新导航通常即可恢复；
// 因此不对单次加载直接判负，而是给几次干净的重试。
const FRAME_READY_TIMEOUT_MS = 20000;
const FRAME_ATTEMPTS = 3;

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

// Bestcap 现在只在被嵌入时渲染：把 frame 当顶层文档打开会被替换成
// “embedded only”。因此这里改为在 parent 页面上调用官方库 render()，
// 由它自己创建 iframe——与真实前端完全一致的加载方式。
const FRAME_TARGET_RE = /captcha\.vyceai\.com\/v1\/frame/;
const BESTCAP_LIB = 'https://captcha.vyceai.com/v1/bestcap.js';

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

  // 先把会话 Cookie 装进空白页，再导航，页面一打开就是已登录状态。
  for (const cookie of parseCookies(COOKIE_HEADER)) {
    await send('Network.setCookie', {
      url: `${PARENT_ORIGIN}/`,
      name: cookie.name,
      value: cookie.value,
      path: '/',
      secure: true,
      httpOnly: true,
    });
  }
  await send('Page.navigate', { url: `${PARENT_ORIGIN}/` });

  // 官方库由页面自己加载；加载失败时手动注入同一个脚本。
  for (let attempt = 1; attempt <= FRAME_ATTEMPTS; attempt++) {
    if (await waitForBestcap(send, sessionId)) break;
    process.stdout.write(`BESTCAP:retry ${attempt}\n`);
    await send('Runtime.evaluate', {
      expression: `(()=>{const s=document.createElement('script');s.src=${JSON.stringify(BESTCAP_LIB)};document.head&&document.head.appendChild(s);return true})()`,
      returnByValue: true,
    });
    await sleep(1500);
  }

  // 和前端一样把控件渲染到页面里，由库自己创建 iframe。
  const rendered = valueOf(await send('Runtime.evaluate', {
    expression: `(()=>{try{
      window.__bcToken=null;window.__bcError=null;window.__bcMsgs=[];
      window.addEventListener('message',(ev)=>{
        const d=ev.data;
        if(d&&typeof d==='object'&&d.bc===1) window.__bcMsgs.push(String(d.type||''));
      },true);
      const host=document.createElement('div');
      host.id='bc-solver-host';
      host.style.cssText='position:fixed;left:8px;top:8px;z-index:2147483647;width:340px;height:96px;background:#fff';
      document.body.appendChild(host);
      window.bestcap.render(host,{
        sitekey:${JSON.stringify(SITEKEY)},
        callback:(v)=>{window.__bcToken=v;},
        'expired-callback':()=>{window.__bcExpired=true;},
        'error-callback':(e)=>{window.__bcError=String((e&&e.message)||e);}
      });
      return 'ok';
    }catch(e){return 'ERR:'+String((e&&e.message)||e);}})()`,
    returnByValue: true,
  }));
  if (rendered !== 'ok') throw new Error(`cannot render captcha widget: ${rendered}`);

  // 关联到控件自己的 OOPIF：跨域 iframe 的 DOM 只能在该 target 里读。
  const frameSid = await attachFrameTarget(send);
  if (!frameSid) throw new Error('captcha frame target not found');
  await send('Runtime.enable', {}, frameSid);
  await send('Network.enable', {}, frameSid);

  let rect = null;
  const readyDeadline = Date.now() + FRAME_READY_TIMEOUT_MS * FRAME_ATTEMPTS;
  while (Date.now() < readyDeadline && !rect && !token) {
    await sleep(400);
    rect = valueOf(await send('Runtime.evaluate', {
      expression: `(()=>{const b=document.getElementById("check");if(!b)return null;const r=b.getBoundingClientRect();return r.width>=5&&r.height>=5?{x:r.x+r.width/2,y:r.y+r.height/2}:null})()`,
      returnByValue: true,
    }, frameSid));
  }
  if (!rect && !token) throw new Error('captcha widget did not become ready');

  if (rect && !token) {
    // 真实鼠标事件：合成的 .click() 不被验证码当成用户交互。
    const clickBox = async (box) => {
      await send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: box.x, y: box.y }, frameSid);
      await send('Input.dispatchMouseEvent', { type: 'mousePressed', x: box.x, y: box.y, button: 'left', clickCount: 1 }, frameSid);
      await sleep(60);
      await send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: box.x, y: box.y, button: 'left', clickCount: 1 }, frameSid);
    };
    await clickBox(rect);

    // 控件偶尔会忽略落在 before-interactive 之前的点击，所以定时补点：
    // 只要令牌还没到、勾选框还在，就当作还没开始解题。
    const solveDeadline = Date.now() + SOLVE_TIMEOUT_MS;
    let nextClickAt = Date.now() + 6000;
    while (!token && Date.now() < solveDeadline) {
      await sleep(300);
      const state = valueOf(await send('Runtime.evaluate', {
        expression: `JSON.stringify({t:window.__bcToken||'',e:window.__bcError||'',m:window.__bcMsgs||[]})`,
        returnByValue: true,
      }));
      let parsed = {};
      try { parsed = JSON.parse(state || '{}'); } catch { /* page navigating */ }
      if (parsed.t) { token = parsed.t; break; }
      if (parsed.e) throw new Error(`captcha widget error: ${parsed.e}`);

      if (Date.now() >= nextClickAt) {
        nextClickAt = Date.now() + 6000;
        const again = valueOf(await send('Runtime.evaluate', {
          expression: `(()=>{const b=document.getElementById("check");if(!b)return null;const r=b.getBoundingClientRect();return r.width>=5&&r.height>=5?{x:r.x+r.width/2,y:r.y+r.height/2}:null})()`,
          returnByValue: true,
        }, frameSid));
        if (again) await clickBox(again);
      }
    }
    if (!token) {
      const msgs = valueOf(await send('Runtime.evaluate', {
        expression: `(window.__bcMsgs||[]).join(',')`, returnByValue: true,
      }));
      throw new Error(`captcha solve timeout (widget messages: ${msgs || 'none'})`);
    }
  }

  process.stdout.write(`TOKEN:${token}\n`);

  if (RENEW_SERVER_ID) {
    // Bestcap 令牌生成后，给验证码后端与页面 1.5 秒同步就绪时间，避免立即 siteverify 遇到副本同步延迟
    await sleep(1500);
    const renew = await renewInBrowser(send, token, sessionId);
    process.stdout.write(`RENEW:${renew}\n`);
  }

  settled = true;
  process.exit(0);
}

async function waitForBestcap(send, sid) {
  const deadline = Date.now() + 20000;
  while (Date.now() < deadline) {
    await sleep(400);
    const kind = valueOf(await send('Runtime.evaluate', {
      expression: 'typeof window.bestcap', returnByValue: true,
    }, sid));
    if (kind === 'object') return true;
  }
  return false;
}

async function attachFrameTarget(send) {
  const deadline = Date.now() + 20000;
  while (Date.now() < deadline) {
    await sleep(400);
    const targets = await send('Target.getTargets', {}, null);
    const info = ((targets.result && targets.result.targetInfos) || [])
      .find((t) => FRAME_TARGET_RE.test(t.url || ''));
    if (!info) continue;
    const attached = await send('Target.attachToTarget', { targetId: info.targetId, flatten: true }, null);
    const sid = attached.result && attached.result.sessionId;
    if (sid) return sid;
  }
  return null;
}

async function renewInBrowser(send, token, sid) {

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
