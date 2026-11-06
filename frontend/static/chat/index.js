/* ---------- 채팅방 ---------- */
let readTimer, readMembers = [], acknowledgedRead = 0;
const readBadges = new Map();

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
  currentRoom = r; lastSeq = 0; retry = 0; prevMsg = null;
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
  clearTimeout(readTimer);
  clearTimeout(reconnectTimer);
  if (ws) { ws.onclose = null; ws.close(); ws = null; }
}
function leaveView() {  // 방을 떠나지 않고 화면만 닫는다
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
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const sock = ws = new WebSocket(`${proto}://${location.host}/ws/${currentRoom.roomId}?lastSeq=${lastSeq}`);
  sock.onopen = () => {
    sock.send(JSON.stringify({ type:"auth", token }));  // 첫 메시지는 반드시 인증
    retry = 0; $("status").textContent = t("connected");
  };
  sock.onmessage = ev => {
    const d = JSON.parse(ev.data);
    if (d.type === "message:new") { if (d.messageSeq > lastSeq) { lastSeq = d.messageSeq; addMessage(d); markVisibleRead(); } }
    else if (d.type === "media:update" || d.type === "media:ready") { if (cards[d.mediaId]) cards[d.mediaId](d); }
    else if (d.type === "presence") $("members").textContent = `${t("online")}: ${d.members.join(", ")}`;
    else if (d.type === "system") addSystem(`${d.user}${t(d.event === "joined" ? "joinedSys" : "leftSys")}`);
    else if (d.type === "room:deleted") { leaveView(); toast(t("roomDeleted")); }
    else if (d.type === "read:update") {
      readMembers = readMembers.filter(member => member.user !== d.user).concat(d);
      renderReadReceipts();
    } else if (d.type === "error") toast(t("failed"));
  };
  sock.onclose = ev => {
    if (sock !== ws) return;
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
  // 번역/요약 버튼: 기능은 아직 없고 자리만 만들어 둔다.
  const acts = document.createElement("div"); acts.className = "acts";
  for (const k of ["translate", "summarize"]) {
    const b = document.createElement("button"); b.type = "button"; b.className = "soon"; b.textContent = t(k); acts.append(b);
  }
  body.append(meta, bubble);
  if (mine) {
    const receipt = document.createElement("span"); receipt.className = "read-receipt";
    readBadges.set(m.messageSeq, receipt); body.append(receipt); renderReadReceipts();
  }
  if (!m.media) body.append(acts);
  row.append(av, body);
  pushToLog(row, mine);
}

$("form").onsubmit = e => {
  e.preventDefault();
  const body = $("body").value.trim();
  if (!body) return;
  send({ type:"message:send", body, sourceLanguage: $("srcLang").value });
  $("body").value = "";
};
