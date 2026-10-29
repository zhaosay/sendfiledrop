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
function element(id) {
  if (!elements.has(id)) elements.set(id, {
    children: [], listeners: {},
    value: '',
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
  document: {
    title: 'sendfiledrop', documentElement: element('root'),
    getElementById: element, querySelector: element,
    createElement() {return element('created-' + elements.size)},
    querySelectorAll() {return []}, addEventListener() {}
  },
  window: {
    innerHeight: 800, addEventListener() {},
    visualViewport: {height: 800, scale: 1, listeners: {}, addEventListener(name, handler) {this.listeners[name] = handler}}
  },
  localStorage: {getItem() {return ''}},
  ZhaoUI: {setTheme() {}, setBrand() {}, message() {}, open() {}, close() {}},
  fetch: async () => ({ok: true, text: async () => '', json: async () => ({senders: [], online: 1, version: 0})}),
  FormData: class {append() {}},
  XMLHttpRequest: class {
    constructor() {this.upload = {}; this.status = 200}
    open() {} setRequestHeader() {}
    send() {uploads++; this.status = uploadStatus; this.onload()}
  },
  alert() {},
  setInterval() {}, setTimeout() {}, console
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
