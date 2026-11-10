/* ---------- 채팅방 ---------- */
let readTimer, readMembers = [], acknowledgedRead = 0;
const readBadges = new Map();
let socketGeneration = 0;
const pendingMessages = new Map();
const pendingTimers = new Map();
const outboxKey = roomId => `messageOutbox:${me || "anonymous"}:${roomId}`;
function saveOutbox() {
  if (!currentRoom) return;
  try { localStorage.setItem(outboxKey(currentRoom.roomId), JSON.stringify([...pendingMessages.values()].map(({body, sourceLanguage, clientMessageId}) => ({body, sourceLanguage, clientMessageId})))); } catch {}
}
function loadOutbox(roomId) {
  pendingMessages.clear();
  for (const timer of pendingTimers.values()) clearTimeout(timer);
  pendingTimers.clear();
  try {
    const entries = JSON.parse(localStorage.getItem(outboxKey(roomId)) || "[]");
    for (const item of entries) if (item?.clientMessageId && typeof item.body === "string") pendingMessages.set(item.clientMessageId, {...item, lastSentGeneration: 0});
  } catch {}
  saveOutbox();
}
function acknowledgeMessage(clientMessageId) {
  const pending = pendingMessages.get(clientMessageId);
  if (!pending) return;
  pendingMessages.delete(clientMessageId);
  clearTimeout(pendingTimers.get(clientMessageId)); pendingTimers.delete(clientMessageId);
  saveOutbox();
  if ($("body").value === pending.body) $("body").value = "";
}
function transmitPending(sock, generation) {
  if (sock !== ws || generation !== socketGeneration || sock.readyState !== 1) return;
  for (const item of pendingMessages.values()) {
    if (item.lastSentGeneration === generation) continue;
    item.lastSentGeneration = generation;
    if (!send({type:"message:send", body:item.body, sourceLanguage:item.sourceLanguage, clientMessageId:item.clientMessageId})) {
      item.lastSentGeneration = 0;
      continue;
    }
    clearTimeout(pendingTimers.get(item.clientMessageId));
    pendingTimers.set(item.clientMessageId, setTimeout(() => {
      if (!pendingMessages.has(item.clientMessageId) || sock !== ws || generation !== socketGeneration) return;
      item.lastSentGeneration = 0;
      transmitPending(sock, generation);
    }, 5000));
  }
}

function renderReadReceipts() {
  for (const [seq, badge] of readBadges) {
    const count = readMembers.filter(member => member.user !== me && member.lastReadSeq >= seq).length;
    badge.textContent = count ? `${t("read")} ${count}` : "";
  }
}

function markVisibleRead() {
  clearTimeout(readTimer);
  if (!currentRoom || !lastSeq || document.visibilityState !== "visible" || !document.hasFocus()) return;
  const log = $("log");
  if (log.scrollHeight - log.scrollTop - log.clientHeight >= 60) return;
  const roomId = currentRoom.roomId, seq = lastSeq;
  if (seq <= acknowledgedRead) return;
  readTimer = setTimeout(async () => {
    if (currentRoom?.roomId !== roomId || document.visibilityState !== "visible" || !document.hasFocus()) return;
    try {
      const member = await api(`/api/rooms/${roomId}/read`, "POST", {lastReadSeq: seq});
      if (currentRoom?.roomId === roomId) acknowledgedRead = member.lastReadSeq;
    } catch {}
  }, 250);
}

$("log").addEventListener("scroll", markVisibleRead);
document.addEventListener("visibilitychange", markVisibleRead);
window.addEventListener("focus", markVisibleRead);

function enterRoom(r) {
  if (currentRoom && currentRoom.roomId === r.roomId && ws) { $("app").classList.add("in-room"); return; }
  closeSocket();
  currentRoom = r; viewGeneration++; lastSeq = 0; retry = 0; prevMsg = null;
  loadOutbox(r.roomId);
  clearFilePolling(); readBadges.clear(); readMembers = []; acknowledgedRead = r.lastReadSeq || 0;
  for (const k in cards) delete cards[k];
  $("log").replaceChildren(); $("members").textContent = ""; $("roomTitle").textContent = r.name;
  $("empty").style.display = "none"; $("chat").style.display = "flex";
  $("app").classList.add("in-room");
  $("deleteRoom").hidden = r.createdBy !== me;
  api(`/api/rooms/${r.roomId}/members`).then(members => {
    if (currentRoom?.roomId === r.roomId) { readMembers = members; renderReadReceipts(); }
  }).catch(() => {});
  renderRooms();
  connect();
  $("body").focus();
}
function closeSocket() {
  socketGeneration++;
  clearTimeout(readTimer);
  clearTimeout(reconnectTimer);
  if (ws) { ws.onclose = null; ws.close(); ws = null; }
}
function leaveView() {  // 방을 떠나지 않고 화면만 닫는다
  viewGeneration++;
  clearFilePolling();
  closeSocket(); currentRoom = null;
  $("chat").style.display = "none"; $("empty").style.display = "grid";
  $("app").classList.remove("in-room"); $("status").textContent = "";
  loadRooms();
}
$("back").onclick = leaveView;
$("leave").onclick = async () => {
  if (!confirm(t("confirmLeave"))) return;
  try { await api(`/api/rooms/${currentRoom.roomId}/leave`, "POST"); } catch {}
  leaveView();
};
$("deleteRoom").onclick = async () => {
  if (!currentRoom || currentRoom.createdBy !== me || !confirm(t("confirmDeleteRoom"))) return;
  const roomId = currentRoom.roomId;
  try {
    await api(`/api/rooms/${roomId}`, "DELETE");
    if (currentRoom?.roomId === roomId) { leaveView(); toast(t("roomDeleted")); }
  } catch { toast(t("failed")); }
};

function connect() {
  $("status").textContent = t(retry ? "reconnecting" : "connecting");
  const generation = ++socketGeneration, roomId = currentRoom?.roomId, authToken = token;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const sock = ws = new WebSocket(`${proto}://${location.host}/ws/${roomId}?lastSeq=${lastSeq}`);
  const isCurrent = () => sock === ws && generation === socketGeneration && currentRoom?.roomId === roomId && token === authToken;
  sock.onopen = () => {
    if (!isCurrent()) { sock.close(); return; }
    sock.send(JSON.stringify({ type:"auth", token }));  // 첫 메시지는 반드시 인증
    retry = 0; $("status").textContent = t("connected");
    transmitPending(sock, generation);
  };
  sock.onmessage = ev => {
    if (!isCurrent()) return;
    const d = JSON.parse(ev.data);
    if (d.roomId && d.roomId !== roomId) return;
    if (d.type === "message:new") { if (d.messageSeq > lastSeq) { lastSeq = d.messageSeq; addMessage(d); markVisibleRead(); } }
    else if (d.type === "message:ack") acknowledgeMessage(d.clientMessageId);
    else if (d.type === "media:update" || d.type === "media:ready") { if (cards[d.mediaId]) cards[d.mediaId](d); }
    else if (d.type === "presence") $("members").textContent = `${t("online")}: ${d.members.join(", ")}`;
    else if (d.type === "system") addSystem(`${d.user}${t(d.event === "joined" ? "joinedSys" : "leftSys")}`);
    else if (d.type === "room:deleted") { leaveView(); toast(t("roomDeleted")); }
    else if (d.type === "read:update") {
      readMembers = readMembers.filter(member => member.user !== d.user).concat(d);
      renderReadReceipts();
    } else if (d.type === "error") {
      if (d.clientMessageId && d.detail === "message too long") {
        pendingMessages.delete(d.clientMessageId); clearTimeout(pendingTimers.get(d.clientMessageId)); pendingTimers.delete(d.clientMessageId); saveOutbox();
      }
      toast(t("failed"));
    }
  };
  sock.onclose = ev => {
    if (!isCurrent()) return;
    if (ev.code === 4401) return logout(true);  // 토큰이 없거나 만료
    // 4000: 내가 나감 / 4403: 멤버 아님 / 4404: 방 없음 → 재접속하지 않고 닫는다
    if ([4000, 4400, 4403, 4404].includes(ev.code)) return leaveView();
    // 그 외에는 마지막 messageSeq 이후만 다시 받아 누락 메시지를 복구한다.
    retry++; $("status").textContent = t("reconnecting");
    reconnectTimer = setTimeout(connect, Math.min(1000 * retry, 5000));
  };
}

function pushToLog(el, force) {
  const log = $("log");
  const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 60;
  log.appendChild(el);
  if (atBottom || force) log.scrollTop = log.scrollHeight;
}
function addSystem(text) { const d = document.createElement("div"); d.className = "sys"; d.textContent = text; pushToLog(d); prevMsg = null; }
function addMessage(m) {
  const mine = m.sender === me;
  // 같은 사람이 2분 안에 이어서 보내면 아바타·이름을 생략하고 묶어서 보여 준다.
  const cont = prevMsg && prevMsg.sender === m.sender && m.sentAt - prevMsg.sentAt < 120000;
  prevMsg = m;

  const row = document.createElement("div");
  row.className = "m" + (mine ? " mine" : "") + (cont ? " cont" : "");
  const av = document.createElement("div"); av.className = "av"; av.style.background = color(m.sender); av.textContent = initial(m.sender);
  const body = document.createElement("div"); body.className = "body";
  const meta = document.createElement("div"); meta.className = "meta";
  const time = new Date(m.sentAt).toLocaleTimeString(locale, { hour:"2-digit", minute:"2-digit" });
  meta.append(document.createTextNode(mine ? time : `${m.sender} · ${time}`));
  const badge = document.createElement("span"); badge.className = "badge"; badge.textContent = m.sourceLanguage;
  if (!m.media) meta.append(badge);
  const bubble = m.media ? fileCard(m.media) : Object.assign(document.createElement("div"), { className: "bubble", textContent: m.bodyOriginal });  // 원문 그대로 표시
  // 번역: 서버가 저장된 메시지를 직접 읽어 번역한다. 요약은 아직 자리만 만들어 둔다.
  const acts = document.createElement("div"); acts.className = "acts";
  const translateButton = document.createElement("button"); translateButton.type = "button"; translateButton.textContent = t("translate");
  translateButton.onclick = () => translateMessage(m, bubble, translateButton);
  const summarizeButton = document.createElement("button"); summarizeButton.type = "button"; summarizeButton.className = "soon"; summarizeButton.textContent = t("summarize");
  acts.append(translateButton, summarizeButton);
  body.append(meta, bubble);
  if (mine) {
    const receipt = document.createElement("span"); receipt.className = "read-receipt";
    readBadges.set(m.messageSeq, receipt); body.append(receipt); renderReadReceipts();
  }
  if (!m.media) body.append(acts);
  row.append(av, body);
  pushToLog(row, mine);
}

/* ---------- 메시지 번역 ---------- */
const TRANSLATE_CONSENT_KEY = "translateConsent";
const hasTranslateConsent = () => { try { return localStorage.getItem(TRANSLATE_CONSENT_KEY) === "1"; } catch { return false; } };
const rememberTranslateConsent = () => { try { localStorage.setItem(TRANSLATE_CONSENT_KEY, "1"); } catch {} };
const translateErrors = { 429: "translateLimit", 503: "translateOff", 502: "translateFail" };

async function translateMessage(m, bubble, button) {
  const shown = bubble.parentElement.querySelector(".translation");
  if (shown) { shown.remove(); button.textContent = t("translate"); return; }  // 다시 누르면 번역을 숨긴다
  if (!hasTranslateConsent()) {  // 처음 한 번, 텍스트가 외부 AI 서비스로 나간다는 것을 알린다
    if (!confirm(t("translateConsent"))) return;
    rememberTranslateConsent();
  }
  const roomId = currentRoom.roomId, target = targetLang, generation = viewGeneration, authToken = token;
  button.disabled = true; button.textContent = t("translating");
  try {
    const result = await api("/api/ai/translate", "POST", { roomId, messageSeq: m.messageSeq, target });
    if (currentRoom?.roomId !== roomId || viewGeneration !== generation || token !== authToken) return;  // 이전 방·계정의 응답은 폐기
    const box = document.createElement("div"); box.className = "translation";
    const label = document.createElement("small"); label.textContent = `${t("translated")} · ${LANG_NAMES[target]}`;
    const text = document.createElement("div"); text.textContent = result.translated;  // textContent로 XSS 방지
    box.append(label, text);
    bubble.after(box);
    button.textContent = t("translateHide");
  } catch (error) {
    button.textContent = t("translate");
    toast(t(error.code === "TEXT_TOO_LONG" ? "translateLong" : translateErrors[error.status] || "translateFail"));
  } finally {
    button.disabled = false;
  }
}

$("form").onsubmit = e => {
  e.preventDefault();
  const body = $("body").value.trim();
  if (!body) return;
  if (!currentRoom) return;
  const clientMessageId = crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  pendingMessages.set(clientMessageId, {body, sourceLanguage: $("srcLang").value, clientMessageId, lastSentGeneration: 0});
  saveOutbox();
  if (!ws || ws.readyState !== 1) $("status").textContent = t("reconnecting");
  else transmitPending(ws, socketGeneration);
};
