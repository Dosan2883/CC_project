/* DOM/HTTP 모형으로 스크립트 연결과 사용자 흐름을 검증한다. 실제 브라우저 렌더링 검사는 아니다. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(root, "frontend/index.html"), "utf8");
class Element {
  constructor() {
    this.style = {}; this.dataset = {}; this.value = ""; this.children = []; this.parentElement = null;
    this.classList = {add() {}, remove() {}, toggle() {}, contains: name => (this.className || "").split(" ").includes(name)};
    this.scrollHeight = this.scrollTop = this.clientHeight = 0;
  }
  append(...children) { for (const child of children) { if (child && typeof child === "object") child.parentElement = this; this.children.push(child); } }
  appendChild(child) { this.append(child); }
  add(child) { this.append(child); }
  get nextElementSibling() { const nodes = this.parentElement?.children; return nodes?.[nodes.indexOf(this) + 1]; }
  replaceChildren(...children) { for (const child of this.children) if (child && typeof child === "object") child.parentElement = null; this.children = []; this.append(...children); }
  after(...nodes) { if (!this.parentElement) return; const parent = this.parentElement; let index = parent.children.indexOf(this); for (const node of nodes) { if (node && typeof node === "object") node.parentElement = parent; parent.children.splice(++index, 0, node); } }
  remove() { if (!this.parentElement) return; const parent = this.parentElement; parent.children = parent.children.filter(child => child !== this); this.parentElement = null; }
  removeAttribute(name) { delete this[name]; }
  querySelector(selector) {
    const match = selector.match(/^\.(\w+)(?:\[data-page="(\d+)"\])?$/);
    if (!match) return null;
    return this.children.find(child => child.classList?.contains(match[1]) &&
      (match[2] == null || String(child.dataset.page) === match[2])) || null;
  }
  querySelectorAll() { return []; }
  addEventListener() {}
  focus() {}
}
const elements = new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(match => [match[1], new Element()]));
const timers = new Map();
let timerId = 0, focused = false;
const requests = [];
let saved = {uiLocale: "en", sourceLanguage: "ja", defaultTargetLanguage: "fr", preferredSubtitleLanguage: "es"};
let aiConsent = false;
const document = {
  documentElement: {}, body: new Element(), visibilityState: "visible", hasFocus: () => focused,
  getElementById(id) { assert.ok(elements.has(id), "missing DOM element: " + id); return elements.get(id); },
  querySelectorAll() { return []; }, addEventListener() {}, createElement() { return new Element(); },
  createTextNode(textContent) { return {textContent}; }
};
class WebSocket {
  constructor() { this.readyState = 1; this.sent = []; WebSocket.last = this; }
  send(value) { this.sent.push(JSON.parse(value)); }
  close() {}
}
const context = vm.createContext({
  AbortController,
  Option: function (label, value) { this.textContent = label; this.value = value; },
  console, document, navigator: {language: "ko-KR"}, window: {addEventListener() {}},
  location: {protocol: "http:", host: "localhost"}, WebSocket, confirm: () => true,
  sessionStorage: {getItem() { return null; }, setItem() {}, removeItem() {}},
  setInterval() { return 0; }, clearInterval() {},
  setTimeout(fn, ms) { timers.set(++timerId, {fn, ms}); return timerId; }, clearTimeout(id) { timers.delete(id); },
  async fetch(url, options = {}) {
    const method = options.method || "GET";
    const body = options.body && typeof options.body === "string" ? JSON.parse(options.body) : options.body;
    requests.push({url, method, body, headers: options.headers});
    let result = {};
    if (url === "/api/languages") result = ["ko", "en", "ja", "zh-CN", "es", "fr"];
    else if (url === "/api/users/me/preferences") {
      if (method === "PATCH") saved = {...saved, ...body};
      result = saved;
    } else if (url === "/api/users/me/ai-consent") {
      if (method === "PATCH") aiConsent = body.allowed;
      result = {allowed: aiConsent};
    } else if (url.endsWith("/members")) result = [{user: "owner", role: "owner", lastReadSeq: 0}];
    else if (url.endsWith("/read")) result = {lastReadSeq: body.lastReadSeq};
    else if (url === "/api/rooms" || url === "/api/rooms/public") result = [];
    else if (url === "/api/files/upload-url") result = {mediaId: "file", uploadUrl: "/mock-upload", method: "PUT", headers: {"If-None-Match": "*"}, completeUrl: "/api/files/file/complete"};
    return {ok: true, status: 200, json: async () => result};
  }
});
const evaluate = source => vm.runInContext(source, context);
const tick = () => new Promise(resolve => setImmediate(resolve));
(async () => {
  for (const match of html.matchAll(/<script defer src="\/static\/([^"]+)"><\/script>/g)) {
    vm.runInContext(fs.readFileSync(path.join(root, "frontend/static", match[1]), "utf8"), context, {filename: match[1]});
  }
  await tick();
  assert.equal(elements.get("title").textContent, "AI 협업 메신저");
  elements.get("uiLocale").onchange({target: {value: "en"}});
  assert.equal(elements.get("title").textContent, "AI Collab Messenger");
  await evaluate('startSession("test-token", "owner")');
  assert.equal(elements.get("srcLang").value, "ja");
  assert.equal(elements.get("tgtLang").value, "fr");
  assert.equal(elements.get("prefSubtitle").value, "es");
  await evaluate('savePreferences({defaultTargetLanguage: "zh-CN"})');
  assert.equal(elements.get("tgtLang").value, "zh-CN");
  assert.equal(await evaluate("ensureAiConsent()"), true);
  const consentControl = elements.get("prefAiConsent");
  assert.equal(consentControl.checked, true);
  consentControl.checked = false;
  await consentControl.onchange({target: consentControl});
  assert.equal(aiConsent, false);
  assert.deepEqual(requests.filter(req => req.url === "/api/users/me/ai-consent" && req.method === "PATCH")
    .map(req => req.body.allowed), [true, false]);
  evaluate('enterRoom({roomId: "room", name: "Room", createdBy: "owner", lastReadSeq: 0})');
  await tick();
  assert.equal(elements.get("deleteRoom").hidden, false);
  WebSocket.last.onopen();
  assert.equal(WebSocket.last.sent[0].type, "auth");
  assert.ok(!WebSocket.last.sent.some(event => event.type === "preferences:update"), "connect must not overwrite saved settings");
  WebSocket.last.onmessage({data: JSON.stringify({type: "message:new", messageSeq: 1, sender: "owner", bodyOriginal: "hello", sentAt: Date.now(), sourceLanguage: "en"})});
  assert.ok(![...timers.values()].some(timer => timer.ms === 250), "unfocused tab must not mark read");
  focused = true;
  evaluate("markVisibleRead()");
  for (const [id, timer] of [...timers]) if (timer.ms === 250) { timers.delete(id); await timer.fn(); }
  assert.ok(requests.some(request => request.url === "/api/rooms/room/read" && request.body.lastReadSeq === 1));
  await elements.get("fileInput").onchange({target: {files: [{name: "demo.txt", size: 3}], value: "file"}});
  const uploadSteps = requests.filter(request => ["/mock-upload", "/api/files/file/complete", "/api/media/file/process"].includes(request.url));
  assert.deepEqual(uploadSteps.map(request => request.url), ["/mock-upload", "/api/files/file/complete", "/api/media/file/process"]);
  assert.equal(uploadSteps[0].headers["If-None-Match"], "*");
  for (const filename of ["demo.pdf", "demo.docx", "demo.wav", "demo.mp4"]) {
    const before = requests.length;
    await elements.get("fileInput").onchange({target: {files: [{name: filename, size: 3}], value: "file"}});
    assert.deepEqual(requests.slice(before).map(request => request.url),
      ["/api/files/upload-url", "/mock-upload", "/api/files/file/complete", "/api/media/file/process"]);
  }
  for (const filename of ["photo.jpg", "photo.jpeg", "photo.PNG"]) {
    const before = requests.length;
    await elements.get("fileInput").onchange({target: {files: [{name: filename, size: 3}], value: "file"}});
    assert.deepEqual(requests.slice(before).map(request => request.url),
      ["/api/files/upload-url", "/mock-upload", "/api/files/file/complete"]);
  }
  await elements.get("deleteRoom").onclick();
  assert.ok(requests.some(request => request.url === "/api/rooms/room" && request.method === "DELETE"));
  evaluate('enterRoom({roomId: "other", name: "Other", createdBy: "someone-else"})');
  assert.equal(elements.get("deleteRoom").hidden, true);
  console.log("Frontend checks passed: script loading, saved preferences, explicit read policy, direct upload completion, creator-only delete UI.");
})().catch(error => {console.error(error); process.exitCode = 1;});
