/* ---------- 사이드바: 방 입장 / 내 방 목록 ---------- */
let lastPublic = [];
let roomLoadGeneration = 0, roomLoadController, roomLoadPromise;
function cancelRoomLoads() {
  roomLoadGeneration++;
  roomLoadController?.abort(); roomLoadController = null; roomLoadPromise = null;
}
function loadRooms() {
  if (!token) return Promise.resolve();
  if (roomLoadPromise) return roomLoadPromise;
  const generation = ++roomLoadGeneration, requestToken = token;
  const controller = roomLoadController = new AbortController();
  const current = () => token === requestToken && generation === roomLoadGeneration;
  roomLoadPromise = Promise.allSettled([["/api/rooms", false], ["/api/rooms/public", true]].map(async ([url, publicList]) => {
    try {
      const rooms = await api(url, "GET", undefined, {signal:controller.signal});
      if (!current()) return;
      if (publicList) lastPublic = rooms; else lastRooms = rooms;
      renderRooms();
    } catch (e) {
      if (!current() || e.name === "AbortError") return;
      $("err").textContent = `${publicList ? "공개방" : "내 방"} 목록 갱신 실패 — 다시 시도하세요.`;
      $("roomsRetry").hidden = false;
    }
  })).finally(() => { if (current()) { roomLoadPromise = null; roomLoadController = null; } });
  return roomLoadPromise;
}
$("roomsRetry").onclick = () => { cancelRoomLoads(); $("roomsRetry").hidden = true; $("err").textContent = ""; loadRooms(); };
function openSavedRoom(room) {
  cancelRoomLoads();
  lastRooms = [room, ...lastRooms.filter(item => item.roomId !== room.roomId)];
  if (room.visibility === "public") lastPublic = [room, ...lastPublic.filter(item => item.roomId !== room.roomId)];
  renderRooms(); enterRoom(room); loadRooms();
}
function showErr(e, map = {}) { $("err").textContent = map[e.status] || e.message; }

function renderList(box, list, emptyKey, action) {
  box.replaceChildren();
  if (!list.length) { const d = document.createElement("div"); d.className = "hint"; d.style.whiteSpace = "pre-line"; d.textContent = t(emptyKey); box.append(d); return; }
  for (const r of list) {
    const row = document.createElement("div");
    row.className = "room" + (currentRoom && currentRoom.roomId === r.roomId ? " active" : "");
    const ic = document.createElement("div"); ic.className = "ic"; ic.style.background = color(r.name); ic.textContent = initial(r.name);
    const info = document.createElement("div"); info.className = "info";
    const name = document.createElement("div"); name.className = "name"; name.textContent = (r.visibility === "private" ? "🔒 " : "") + r.name;  // textContent로 XSS 방지
    const sub = document.createElement("div"); sub.className = "sub";
    sub.textContent = `${r.memberCount}${t("memberUnit")} · ${r.online}${t("onlineUnit")}`;
    if (r.joined && r.unreadCount) sub.textContent += ` · ${t("unread")} ${r.unreadCount}`;
    info.append(name, sub);
    row.append(ic, info);
    if (action) {
      const b = document.createElement("button"); b.type = "button"; b.className = r.joined ? "" : "primary";
      b.textContent = t(r.joined ? "open" : "joinBtn");
      b.onclick = e => { e.stopPropagation(); action(r); };
      row.append(b);
    }
    row.onclick = () => (action || enterRoom)(r);
    box.append(row);
  }
}
function renderRooms() {
  renderList($("publicRooms"), lastPublic, "noPublic", joinPublic);
  renderList($("rooms"), lastRooms, "noRooms");
}
// 공개방: 비밀번호 없이 참가
async function joinPublic(r) {
  const requestToken = token;
  try { if (!r.joined) r = await api(`/api/rooms/${r.roomId}/join`, "POST"); if (token !== requestToken) return; $("err").textContent = ""; openSavedRoom(r); }
  catch (e) { showErr(e); }
}

/* 탭: 방 만들기 / 비공개방 입장 */
function setTab(name) {
  $("tabCreate").classList.toggle("on", name === "create"); $("tabPrivate").classList.toggle("on", name === "private");
  $("createForm").style.display = name === "create" ? "grid" : "none"; $("privateForm").style.display = name === "private" ? "grid" : "none";
  $("err").textContent = "";
}
$("tabCreate").onclick = () => setTab("create");
$("tabPrivate").onclick = () => setTab("private");
const visibility = () => document.querySelector('input[name="vis"]:checked').value;
document.querySelectorAll('input[name="vis"]').forEach(r => r.onchange = () => {
  const priv = visibility() === "private";
  $("roomPw").style.display = priv ? "" : "none";
  if (priv) $("roomPw").focus(); else $("roomPw").value = "";
});
$("createForm").onsubmit = async e => {
  e.preventDefault();
  const name = $("roomName").value.trim(), vis = visibility(), password = $("roomPw").value;
  if (!name) return;
  if (vis === "private" && password.length < 4) { $("err").textContent = t("ePw"); return; }
  const requestToken = token;
  try {
    const r = await api("/api/rooms", "POST", { name, visibility: vis, password });
    if (token !== requestToken) return;
    $("roomName").value = ""; $("roomPw").value = ""; $("err").textContent = "";
    openSavedRoom(r);
  } catch (err) { showErr(err, { 409: t("eDup"), 400: t("ePw") }); }
};
// 비공개방: 방 이름 + 비밀번호가 모두 맞아야 입장
$("privateForm").onsubmit = async e => {
  e.preventDefault();
  const name = $("privName").value.trim(), password = $("privPw").value;
  if (!name || !password) return;
  const requestToken = token;
  try {
    const r = await api("/api/rooms/enter-private", "POST", { name, password });
    if (token !== requestToken) return;
    $("privName").value = ""; $("privPw").value = ""; $("err").textContent = "";
    openSavedRoom(r);
  } catch (err) { $("privPw").value = ""; showErr(err, { 403: t("eBad"), 429: t("eMany") }); }
};
