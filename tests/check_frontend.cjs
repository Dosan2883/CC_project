/* DOM/HTTP 모형으로 스크립트 연결과 사용자 흐름을 검증한다. 실제 브라우저 렌더링 검사는 아니다. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const {webcrypto} = require("node:crypto");
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
  reset() {}
  pause() { this.pauses = (this.pauses || 0) + 1; }
  load() {}
}
const elements = new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(match => [match[1], new Element()]));
const timers = new Map();
const localSettings = new Map();
const sessionSettings = new Map();
const storage = values => ({
  get length() { return values.size; }, key: index => [...values.keys()][index] ?? null,
  getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key),
});
let timerId = 0, focused = false;
const requests = [];
let saved = {uiLocale: "en", sourceLanguage: "ja", defaultTargetLanguage: "fr", preferredSubtitleLanguage: "es"};
let aiConsent = false;
const document = {
  documentElement: {dataset: {}}, body: new Element(), visibilityState: "visible", hasFocus: () => focused,
  getElementById(id) { assert.ok(elements.has(id), "missing DOM element: " + id); return elements.get(id); },
  querySelectorAll() { return []; }, addEventListener() {}, removeEventListener() {}, createElement() { return new Element(); },
  createTextNode(textContent) { return {textContent}; }
};
class WebSocket {
  constructor() { this.readyState = 1; this.sent = []; WebSocket.last = this; }
  send(value) { this.sent.push(JSON.parse(value)); }
  close() {}
}
const context = vm.createContext({
  AbortController, crypto:webcrypto,
  Option: function (label, value) { this.textContent = label; this.value = value; },
  console, document, navigator: {language: "ko-KR"}, window: {addEventListener() {}},
  location: {protocol: "http:", host: "localhost"}, WebSocket, confirm: () => true,
  localStorage: storage(localSettings), sessionStorage: storage(sessionSettings),
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
assert.match(html, /<script src="\/static\/shared\/theme\.js"><\/script>/);
assert.doesNotMatch(html, /<script\s*>/);
const themeBootstrap = fs.readFileSync(path.join(root, "frontend/static/shared/theme.js"), "utf8");
function loadFrontendScripts() {
  evaluate(themeBootstrap);
  for (const match of html.matchAll(/<script defer src="\/static\/([^"?]+)(?:\?[^"]*)?"><\/script>/g)) {
    vm.runInContext(fs.readFileSync(path.join(root, "frontend/static", match[1]), "utf8"), context, {filename: match[1]});
  }
}
(async () => {
  sessionSettings.set("token", "previous-token");
  localSettings.set("messageOutbox:previous:room", "legacy text");
  let finishAutoLogin;
  const realFetch = context.fetch;
  context.fetch = (url, options) => {
    if (url === "/api/auth/me") return new Promise(resolve => { finishAutoLogin = resolve; });
    if (url === "/api/auth/login") return Promise.resolve({ok:true, json:async () => ({token:"test-token", nickname:"owner"})});
    if (url === "/invalid-response") return Promise.resolve({ok:false, status:422, json:async () => ({detail:[{msg:"validation failed"}]})});
    return realFetch(url, options);
  };
  loadFrontendScripts();
  assert.ok(!localSettings.has("messageOutbox:previous:room"));
  await tick();
  const theme = elements.get("prefTheme");
  assert.equal(theme.value, "system");
  theme.value = "dark";
  theme.onchange();
  assert.equal(document.documentElement.dataset.theme, "dark");
  assert.equal(localSettings.get("theme"), "dark");
  delete document.documentElement.dataset.theme;
  evaluate(themeBootstrap);
  assert.equal(document.documentElement.dataset.theme, "dark");
  theme.value = "system";
  theme.onchange();
  assert.equal(document.documentElement.dataset.theme, undefined);
  assert.equal(localSettings.get("theme"), "system");
  assert.equal(elements.get("title").textContent, "AI 협업 메신저");
  elements.get("uiLocale").onchange({target: {value: "en"}});
  assert.equal(elements.get("title").textContent, "AI Collab Messenger");
  elements.get("user").value = "owner";
  elements.get("authPw").value = "secret123";
  await elements.get("authForm").onsubmit({preventDefault() {}});
  finishAutoLogin({ok:false, status:401, json:async () => ({detail:"expired"})});
  await tick();
  assert.equal(sessionSettings.get("token"), "test-token");
  await assert.rejects(evaluate('api("/invalid-response")'), error => error.status === 422 && error.code === undefined);
  assert.equal(elements.get("srcLang").value, "ja");
  assert.equal(elements.get("tgtLang").value, "fr");
  await evaluate('savePreferences({defaultTargetLanguage: "zh-CN"})');
  assert.equal(elements.get("tgtLang").value, "zh-CN");
  assert.equal(await evaluate("ensureAiConsent()"), true);
  assert.equal(aiConsent, true);  // 처음 쓸 때 자동으로 허용이 저장된다
  assert.equal(await evaluate("ensureAiConsent()"), true);  // 이미 허용이면 다시 저장하지 않는다
  assert.deepEqual(requests.filter(req => req.url === "/api/users/me/ai-consent" && req.method === "PATCH")
    .map(req => req.body.allowed), [true]);
  evaluate('enterRoom({roomId: "room", name: "Room", createdBy: "owner", lastReadSeq: 0})');
  await tick();
  assert.equal(elements.get("deleteRoom").hidden, false);
  WebSocket.last.onopen();
  assert.equal(WebSocket.last.sent[0].type, "auth");
  elements.get("body").value = "private draft";
  elements.get("form").onsubmit({preventDefault() {}});
  elements.get("form").onsubmit({preventDefault() {}});
  assert.equal(elements.get("body").value, "");
  assert.deepEqual(WebSocket.last.sent.map(event => event.type), ["auth"]);
  assert.equal(JSON.parse(sessionSettings.get("messageOutbox:owner:room")).length, 1);
  assert.ok(!localSettings.has("messageOutbox:owner:room"));
  WebSocket.last.onmessage({data: JSON.stringify({type:"presence", members:["owner"]})});
  assert.deepEqual(WebSocket.last.sent.map(event => event.type), ["auth", "message:send"]);
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
  evaluate('fileCard({mediaId:"audio-a",filename:"a.mp3",ext:"mp3",size:1}); fileCard({mediaId:"audio-b",filename:"b.mp3",ext:"mp3",size:1})');
  await tick();
  evaluate('cards["audio-a"]({status:"COMPLETED",progress:100}); cards["audio-b"]({status:"COMPLETED",progress:100})');
  const playerA = evaluate('audioPlayers.get("audio-a")'), playerB = evaluate('audioPlayers.get("audio-b")');
  evaluate('cards["audio-a"]({status:"DELETED"})');
  assert.equal(playerA.pauses, 1);
  assert.equal(playerB.pauses, undefined);
  assert.equal(evaluate('audioPlayers.has("audio-b")'), true);
  elements.get("settingsPop").hidden = false;
  elements.get("roomMenu").open = true;
  evaluate('logout(false)');
  assert.equal(elements.get("settingsPop").hidden, true);
  assert.equal(elements.get("roomMenu").open, false);
  assert.ok(!sessionSettings.has("messageOutbox:owner:room"));
  assert.equal(evaluate('pendingTimers.size'), 0);
  console.log("Frontend checks passed: script loading, saved preferences, explicit read policy, direct upload completion, creator-only delete UI.");
})().catch(error => {console.error(error); process.exitCode = 1;});
