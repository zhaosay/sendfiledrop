"""Check that the rendered page initializes its upload controls (requires Node.js)."""

import json
from pathlib import Path
import re
import subprocess
import unittest


class FrontendInitTest(unittest.TestCase):
    def test_upload_handlers_initialize(self):
        # Read the actual template without importing the server or touching user data.
        template = Path(__file__).with_name("templates").joinpath("index.html").read_text()
        app_js = Path(__file__).with_name("static").joinpath("app.js").read_text()
        # 外链的 /static/app.js 在页面里是空 <script src>，这里换成真实内容
        scripts = [body or app_js for body in re.findall(r"<script(?: src=\"/static/app.js\")?>(.*?)</script>", template, re.S)]
        harness = r"""
const vm = require('node:vm');
const assert = require('node:assert/strict');
const elements = new Map();
const timers = [];
const statusRequests = [];
let statusVersion = 1;
let bundleFailures = 0;
const textRequests = [];
function element(id) {
  if (!elements.has(id)) elements.set(id, {
    children: [], listeners: {},
    value: '', dataset: {maxChars: '20000'},
    style: {values: {}, setProperty(name, value) {this.values[name] = value}},
    classList: {
      values: new Set(), add(name) {this.values.add(name)}, remove(name) {this.values.delete(name)},
      contains(name) {return this.values.has(name)},
      toggle(name, force) {if (force) this.values.add(name); else this.values.delete(name)}
    },
    addEventListener(event, handler) {this.listeners[event] = handler},
    appendChild(child) {this.children.push(child)},
    setAttribute() {}, removeAttribute() {}, remove() {},
    querySelector(selector) {return element(id + selector)},
    querySelectorAll() {return []},
    scrollHeight: 0, scrollTop: 0, clientHeight: 0
  });
  return elements.get(id);
}
const context = {
  crypto: require('node:crypto').webcrypto,
  document: {
    title: 'sendfiledrop', documentElement: element('root'),
    getElementById: element, querySelector: element,
    createElement() {return element('created-' + elements.size)},
    querySelectorAll() {return []}, addEventListener() {}, removeEventListener() {},
    body: element('body')
  },
  window: {
    innerHeight: 800, addEventListener() {},
    visualViewport: {height: 800, scale: 1, listeners: {}, addEventListener(name, handler) {this.listeners[name] = handler}}
  },
  localStorage: {getItem() {return ''}},
  ZhaoUI: {setTheme() {}, setBrand() {}, message() {}, open() {}, close() {}},
  fetch: async (url, options) => {
    if (url === '/text') {
      textRequests.push(options.headers['X-SFD-Request-ID']);
      if (textRequests.length === 1) throw new Error('response lost');
    }
    if (url.startsWith('/status')) statusRequests.push(url);
    if (url.startsWith('/bundle') && bundleFailures > 0) {
      bundleFailures--;
      throw new Error('offline');
    }
    return {ok: true, text: async () => url.startsWith('/feed') ? 'version ' + statusVersion : '', json: async () => ({
      senders: [], online: 1, version: statusVersion, feed: 'version ' + statusVersion, favorites: ''
    })};
  },
  FormData: class {append() {}},
  XMLHttpRequest: class {
    constructor() {this.upload = {}; this.status = 200}
    open() {} setRequestHeader() {}
    send() {uploads++; this.status = uploadStatus; this.onload()}
  },
  alert() {},
  setInterval() {}, setTimeout(handler, delay) {timers.push({handler, delay})}, console
};
vm.createContext(context);
let uploads = 0;
let uploadStatus = 200;
for (const script of SCRIPTS) vm.runInContext(script, context);
assert.equal(element('root').style.values['--sfd-viewport-height'], '800px');
context.document.activeElement = {tagName: 'TEXTAREA'};
context.window.visualViewport.height = 420;
context.window.visualViewport.listeners.resize();
assert.equal(element('root').style.values['--sfd-viewport-height'], '420px');
assert.equal(element('root').classList.contains('keyboard-open'), true);
context.window.visualViewport.scale = 2;
context.window.visualViewport.listeners.resize();
assert.equal(element('root').style.values['--sfd-viewport-height'], '800px', 'Pinch zoom must not shrink the composer layout');
context.window.visualViewport.scale = 1;
context.window.visualViewport.height = 800;
context.document.activeElement = null;
context.window.visualViewport.listeners.resize();
assert.equal(element('root').classList.contains('keyboard-open'), false);
assert.equal(typeof element('attachBtn').onclick, 'function');
assert.equal(typeof element('fileInput').onchange, 'function');
assert.equal(typeof element('sendBtn').onclick, 'function');
(async () => {
  const file = {name: 'dragged.txt'};
  const drop = () => element('.main').listeners.drop({
    preventDefault() {}, dataTransfer: {files: [file]}
  });
  drop();
  assert.equal(uploads, 0, 'Dropping a file must wait for Send');
  await element('sendBtn').onclick();
  assert.equal(uploads, 1, 'Send must upload a file without text');
  await element('sendBtn').onclick();
  assert.equal(uploads, 1, 'Sent files must not be sent twice');
  element('fileInput').files = [file];
  element('fileInput').onchange();
  assert.equal(uploads, 1, 'Selected files must also wait for Send');
  uploadStatus = 500;
  await element('sendBtn').onclick();
  uploadStatus = 200;
  await element('sendBtn').onclick();
  assert.equal(uploads, 3, 'Failed files must remain available for retry');
  assert.equal(element('sendBtn').disabled, false);
  const img = {getAttribute: name => name === 'src' ? '/view/a.png' : 'a.png'};
  const onThumb = {closest: selector => selector === '.media-thumb img' ? img : null};
  const elsewhere = {closest: () => null};
  element('feed').listeners.dblclick({target: elsewhere, preventDefault() {}});
  assert.equal(element('body').children.length, 0, 'Double-clicking outside a thumbnail must not open a preview');
  element('feed').listeners.dblclick({target: onThumb, preventDefault() {}});
  assert.equal(element('body').children.length, 1, 'Double-clicking a thumbnail must open the preview');
  const lightbox = element('body').children[0];
  assert.equal(lightbox.className, 'lightbox');
  assert.equal(lightbox.children[0].src, '/view/a.png');
  lightbox.onclick({target: lightbox});
  assert.equal(element('body').children.length, 1, 'stub remove() does not detach, but the handler must not throw');
  const mainPane = element('.main');
  assert.equal(mainPane.classList.contains('filters-collapsed'), true, 'Filter bar must start collapsed by default');
  element('filterToggle').onclick();
  assert.equal(mainPane.classList.contains('filters-collapsed'), false, 'Toggle must expand the mobile filter bar');
  element('filterToggle').onclick();
  assert.equal(mainPane.classList.contains('filters-collapsed'), true, 'Toggle must collapse it again');
  const copyMessages = [];
  const original = '第一行\nhttps://www.instagram.com/esmehuang_';
  const copyButton = element('copyButton');
  copyButton.getAttribute = () => original;
  copyButton.closest = () => null;
  context.ZhaoUI.message = (message, options) => copyMessages.push([message, options.type]);
  context.window.isSecureContext = false;
  const field = {
    style: {}, focus() {}, select() {}, remove() {},
    setSelectionRange(start, end) {assert.deepEqual([start, end], [0, original.length])}
  };
  context.document.createElement = () => field;
  let copyAllowed = true;
  context.document.execCommand = command => {
    assert.equal(command, 'copy');
    assert.equal(field.value, original);
    return copyAllowed;
  };
  const copy = () => element('feed').listeners.click({
    currentTarget: element('feed'),
    target: {closest: selector => selector === '.copy-text' ? copyButton : null}
  });
  await copy();
  assert.deepEqual(copyMessages.pop(), ['内容已复制', 'success']);
  copyAllowed = false;
  await copy();
  assert.deepEqual(copyMessages.pop(), ['复制失败，请长按文字手动复制', 'error']);
  const input = element('textInput');
  assert.equal(input.listeners.keydown, undefined, 'Enter must retain native multiline and IME behavior');
  input.value = 'a'.repeat(20001);
  await element('sendBtn').onclick();
  assert.equal(input.value.length, 20001, 'Oversize draft must stay in the composer');
  assert.match(copyMessages.pop()[0], /20000/);
  const nextPoll = delay => {
    const index = timers.findIndex(timer => timer.handler.name === 'poll' && timer.delay === delay);
    assert.notEqual(index, -1, 'Expected a scheduled poll');
    return timers.splice(index, 1)[0].handler();
  };
  await new Promise(resolve => setImmediate(resolve));
  statusVersion = 2;
  bundleFailures = 1;
  await nextPoll(0);
  assert.equal(element('connectionStatus').hidden, false);
  assert.equal(element('feed').innerHTML, 'version 1');
  await nextPoll(3000);
  assert.deepEqual(statusRequests, ['/status', '/status?since=1', '/status?since=1']);
  assert.equal(element('connectionStatus').hidden, true);
  assert.equal(element('feed').innerHTML, 'version 2');
  input.value = 'retry this text';
  await element('sendBtn').onclick();
  assert.equal(input.value, 'retry this text');
  await element('sendBtn').onclick();
  assert.equal(input.value, '');
  assert.match(textRequests[0], /^[0-9a-f]{32}$/);
  assert.equal(textRequests[0], textRequests[1], 'A retry must reuse the original request ID');
  input.value = 'retry this text';
  await element('sendBtn').onclick();
  assert.notEqual(textRequests[1], textRequests[2], 'A new send must receive its own ID');
  console.log('Upload initialization, draft, send, and retry checks passed');
})().catch(error => {console.error(error); process.exitCode = 1});
"""
        result = subprocess.run(
            ["node"], input="const SCRIPTS = " + json.dumps(scripts) + ";\n" + harness,
            text=True, capture_output=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
