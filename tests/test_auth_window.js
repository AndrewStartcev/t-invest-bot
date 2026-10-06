// Exercise the actual click handler with delayed HTTP replies and popup blocking.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');

const html = fs.readFileSync(path.join(__dirname, '../demo/index.html'), 'utf8');
const handlers = html.slice(html.indexOf('    function openServerBrowser()'),
  html.indexOf("    el('auth-button').addEventListener('click', startAuth)"));

function harness({server = true, blocked = false, status = 'required'} = {}) {
  const calls = [], elements = new Map();
  const popup = {closed: false, opener: {}, focus() {calls.push('focus');}};
  const pending = [];
  const context = vm.createContext({
    serverBrowserUrl: server ? '/desktop/vnc.html?path=desktop/websockify' : null,
    pulseWindow: null, authStatus: status,
    el(id) {if (!elements.has(id)) elements.set(id, {}); return elements.get(id);},
    window: {open(url, name) {calls.push({open: url, name}); return blocked ? null : popup;}},
    api(endpoint) {
      calls.push({request: endpoint});
      return new Promise(resolve => pending.push(resolve));
    },
  });
  vm.runInContext(handlers, context);
  return {context, calls, elements, popup, pending};
}

test('opens the client window before waiting for the server; reuses it on another click', async () => {
  const h = harness();
  const first = h.context.startAuth();
  assert.equal(h.calls[0].open, h.context.serverBrowserUrl);
  assert.equal(h.calls[1].request, '/api/auth/start');
  assert.equal(h.popup.opener, null);
  assert.equal(h.elements.get('auth-button').disabled, true);
  h.pending.shift()();
  await first;
  assert.equal(h.elements.get('auth-button').disabled, false);
  h.context.authStatus = 'waiting';
  const second = h.context.startAuth();
  assert.equal(h.calls[2], 'focus');
  assert.equal(h.calls[3].request, '/api/browser/show');
  h.pending.shift()();
  await second;
});

test('explains how to open the browser when the popup is blocked', async () => {
  const h = harness({blocked: true});
  const click = h.context.startAuth();
  assert.equal(h.calls[1].request, '/api/auth/start');
  h.pending.shift()();
  await click;
  assert.match(h.elements.get('auth-message').textContent, /Открыть серверный браузер/);
  assert.equal(h.elements.get('auth-button').disabled, false);
});

test('local Windows mode does not open a remote desktop window', async () => {
  const h = harness({server: false});
  const click = h.context.startAuth();
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0].request, '/api/auth/start');
  h.pending.shift()();
  await click;
});
