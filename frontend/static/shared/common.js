const $ = id => document.getElementById(id);
let locale = (navigator.language || "ko").slice(0,2);
if (!I18N[locale]) locale = "en";
let token = null, ws, me, currentRoom, lastSeq = 0, retry = 0, reconnectTimer, roomsTimer, lastRooms = [], prevMsg = null;
let targetLang = locale;

const hue = s => { let h = 0; for (const c of s) h = (h * 31 + c.codePointAt(0)) % 360; return h; };
const color = s => `hsl(${hue(s)} 50% 42%)`;
const initial = s => [...s][0]?.toUpperCase() || "?";

async function api(path, method = "GET", body) {
  const headers = { "Content-Type": "application/json" };
  if (token) headers.Authorization = "Bearer " + token;  // 서버는 이 토큰으로만 "누구인지" 판단한다
  const res = await fetch(path, { method, headers, body: body ? JSON.stringify(body) : undefined });
  if (!res.ok) {
    const e = new Error(t("failed")); e.status = res.status; e.code = (await res.json().catch(() => ({}))).detail;
    if (res.status === 401 && token && !path.startsWith("/api/auth/")) logout(true);  // 토큰 만료·무효
    throw e;
  }
  return res.json();
}
function send(obj) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj)); }
