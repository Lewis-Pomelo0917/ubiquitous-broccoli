import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const htmlPath = new URL('../frontend/index.html', import.meta.url);
const html = readFileSync(htmlPath, 'utf8');
const scriptStart = html.lastIndexOf("  <script>\n    (() => {\n      var btn = document.getElementById('always-on-top-toggle');");
assert.notEqual(scriptStart, -1, 'always-on-top script missing');
const scriptEnd = html.indexOf('</script>', scriptStart);
const script = html.slice(scriptStart + html.slice(scriptStart).indexOf('\n') + 1, scriptEnd);

class Element {
  constructor() { this.hidden = true; this.disabled = false; this.textContent = ''; this.attributes = {}; this.listeners = {}; }
  addEventListener(type, listener) { this.listeners[type] = listener; }
  click() { if (this.listeners.click) this.listeners.click(); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  removeAttribute(name) { delete this.attributes[name]; }
}

const tick = () => new Promise((resolve) => setImmediate(resolve));

async function run(initialState, setAlwaysOnTop) {
  const button = new Element();
  const status = new Element();
  const currentWindow = { isAlwaysOnTop: () => Promise.resolve(initialState), setAlwaysOnTop };
  const context = {
    document: { getElementById: (id) => id === 'always-on-top-toggle' ? button : status },
    window: { __TAURI__: { window: { getCurrentWindow: () => currentWindow } } },
    Promise, setImmediate
  };
  vm.runInNewContext(script, context);
  await tick();
  return { button, status };
}

const off = await run(false, () => Promise.resolve());
assert.equal(off.button.hidden, false);
assert.equal(off.button.textContent, '窗口置顶');
assert.equal(off.button.getAttribute('aria-pressed'), 'false');

let resolveEnable;
let enableCalls = 0;
const on = await run(true, () => {
  enableCalls += 1;
  return new Promise((resolve) => { resolveEnable = resolve; });
});
assert.equal(on.button.textContent, '取消置顶');
on.button.click();
on.button.click();
assert.equal(enableCalls, 1);
assert.equal(on.button.disabled, true);
resolveEnable();
await tick();
assert.equal(on.button.textContent, '窗口置顶');
assert.equal(on.button.getAttribute('aria-pressed'), 'false');

const failure = await run(false, () => Promise.reject(new Error('test')));
failure.button.click();
await tick();
assert.match(failure.status.textContent, /设置窗口置顶失败/);
assert.equal(failure.status.hidden, false);
assert.equal(failure.button.disabled, false);

const browserButton = new Element();
const browserStatus = new Element();
vm.runInNewContext(script, {
  document: { getElementById: (id) => id === 'always-on-top-toggle' ? browserButton : browserStatus },
  window: {}, Promise
});
assert.equal(browserButton.hidden, true);
assert.equal(browserStatus.hidden, true);

const permission = JSON.parse(readFileSync(new URL('../desktop/src-tauri/capabilities/default.json', import.meta.url), 'utf8'));
assert.deepEqual(permission.permissions, ['core:default', 'core:window:allow-set-always-on-top']);
assert.match(html, /always-on-top-toggle\[aria-pressed="true"\]/);

console.log('always-on-top check passed');
