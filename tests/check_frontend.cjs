/* DOM/HTTP 모형으로 스크립트 연결과 사용자 흐름을 검증한다. 실제 브라우저 렌더링 검사는 아니다. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(root, "frontend/index.html"), "utf8");
class Element {
  constructor() {
    this.style = {}; this.dataset = {}; this.value = ""; this.children = [];
    this.classList = {add() {}, remove() {}, toggle() {}};
    this.scrollHeight = this.scrollTop = this.clientHeight = 0;
  }
  append(...children) { this.children.push(...children); }
  appendChild(child) { this.append(child); }
  replaceChildren(...children) { this.children = children; }
  addEventListener() {}
  focus() {}
}
const elements = new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(match => [match[1], new Element()]));
const timers = new Map();
let timerId = 0, focused = false;
const requests = [];
let saved = {uiLocale: "en", sourceLanguage: "ja", defaultTargetLanguage: "fr", preferredSubtitleLanguage: "es"};
const document = {
  documentElement: {}, visibilityState: "visible", hasFocus: () => focused,
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
  await elements.get("deleteRoom").onclick();
  assert.ok(requests.some(request => request.url === "/api/rooms/room" && request.method === "DELETE"));
  evaluate('enterRoom({roomId: "other", name: "Other", createdBy: "someone-else"})');
  assert.equal(elements.get("deleteRoom").hidden, true);
  console.log("Frontend checks passed: script loading, saved preferences, explicit read policy, direct upload completion, creator-only delete UI.");
})().catch(error => {console.error(error); process.exitCode = 1;});
