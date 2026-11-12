const $ = id => document.getElementById(id);
let locale = (navigator.language || "ko").slice(0,2);
if (!I18N[locale]) locale = "en";
let token = null, ws, me, currentRoom, lastSeq = 0, retry = 0, reconnectTimer, roomsTimer, lastRooms = [], prevMsg = null, viewGeneration = 0;
let targetLang = locale;

const hue = s => { let h = 0; for (const c of s) h = (h * 31 + c.codePointAt(0)) % 360; return h; };
const color = s => `hsl(${hue(s)} 50% 42%)`;
const initial = s => [...s][0]?.toUpperCase() || "?";

const activeApiRequests = new Set();
function cancelApiRequests() { for (const controller of activeApiRequests) controller.abort(); }
async function api(path, method = "GET", body, options = {}) {
  const requestToken = token, controller = new AbortController();
  activeApiRequests.add(controller);
  const abort = () => controller.abort();
  options.signal?.addEventListener("abort", abort, {once:true});
  if (options.signal?.aborted) controller.abort();
  const headers = { "Content-Type": "application/json" };
  if (requestToken) headers.Authorization = "Bearer " + requestToken;
  let timer, onAbort;
  const cancelled = new Promise((_, reject) => {
    onAbort = () => reject(Object.assign(new Error("Request cancelled"), {name:"AbortError"}));
    controller.signal.addEventListener("abort", onAbort, {once:true});
    if (controller.signal.aborted) onAbort();
    timer = setTimeout(() => { reject(Object.assign(new Error(t("failed")), {code:"REQUEST_TIMEOUT"})); controller.abort(); }, options.timeout || (path.startsWith("/api/ai/translate") ? 120000 : 10000));
  });
  try {
  return await Promise.race([cancelled, (async () => {
  const res = await fetch(path, { method, headers, signal:controller.signal, body: body ? JSON.stringify(body) : undefined });
  if (!res.ok) {
    const e = new Error(t("failed")); e.status = res.status; e.code = (await res.json().catch(() => ({}))).detail;
    if (res.status === 401 && token === requestToken && token && !path.startsWith("/api/auth/")) logout(true);
    throw e;
  }
  return res.json();
  })()]);
  } finally {
    clearTimeout(timer); activeApiRequests.delete(controller);
    controller.signal.removeEventListener("abort", onAbort);
    options.signal?.removeEventListener("abort", abort);
  }
}
function send(obj) {
  if (!ws || ws.readyState !== 1) return false;
  try { ws.send(JSON.stringify(obj)); return true; } catch { return false; }
}
