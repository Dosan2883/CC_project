/* 채팅방 참여자 목록: 방의 모든 참여자와 방장을 보여 주고, 지금 접속 중인 사람은 초록 점으로 표시한다.
   목록은 떠 있는 창이라 위치와 크기를 바꿀 수 있다. 참여자가 들어오거나 나가고 접속 상태가 바뀌면 열려 있는 목록이 바로 갱신된다. */
let onlineUsers = [];        // 지금 접속 중인 닉네임(서버의 presence 이벤트)
let participantList = [];    // 방의 모든 참여자 [{user, role, lastReadSeq, joinedAt}]
let participantsWin = null;

function resetParticipants() {  // 방을 바꾸거나 나갈 때
  onlineUsers = []; participantList = []; participantsAgain = false;
  $("infoMembers").replaceChildren(); $("infoCount").textContent = ""; $("infoAbout").textContent = "";
  $("participants").textContent = t("participants");
  if (participantsWin) { participantsWin.close(); participantsWin = null; }
}

function setOnlineUsers(users) {
  onlineUsers = users || [];
  renderParticipants();
  loadParticipants();  // 들어오고 나간 사람이 상세 정보 목록에도 바로 반영된다
}

let participantsLoading = false, participantsAgain = false;  // 접속 알림이 연달아 와도 목록 요청은 한 번에 하나만 보내고, 그 사이 온 알림은 끝난 뒤 한 번 더 읽는다
async function loadParticipants() {
  if (!currentRoom) return;
  if (participantsLoading) { participantsAgain = true; return; }
  participantsLoading = true;
  const roomId = currentRoom.roomId, generation = viewGeneration, authToken = token;
  try {
    const members = await api(`/api/rooms/${roomId}/members`);
    if (currentRoom?.roomId !== roomId || viewGeneration !== generation || token !== authToken) return;  // 다른 방·계정으로 바뀌었으면 버린다
    participantList = members;
    renderParticipants();
  } catch { /* 목록을 못 받아도 채팅에는 영향이 없다. 다음 갱신 때 다시 시도한다 */ }
  finally {
    participantsLoading = false;
    if (participantsAgain) { participantsAgain = false; loadParticipants(); }
  }
}

function renderParticipants() {
  const online = new Set(onlineUsers.map(user => String(user).toLowerCase()));
  const isOnline = member => online.has(member.user.toLowerCase());
  $("participants").textContent = `${t("participants")} (${participantList.length})`;
  renderInfoPanel(isOnline);
  if (!participantsWin || !participantsWin.open) return;
  const onlineCount = participantList.filter(isOnline).length;
  participantsWin.title.textContent = `${t("participants")} ${participantList.length} · ${t("online")} ${onlineCount}`;
  const rows = [...participantList].sort((a, b) =>
    (b.role === "owner") - (a.role === "owner") || isOnline(b) - isOnline(a) || a.user.localeCompare(b.user));
  participantsWin.body.replaceChildren(...(rows.length ? rows.map(member => {
    const row = el("div", "part-row"), dot = el("span", isOnline(member) ? "part-dot on" : "part-dot");
    dot.title = t(isOnline(member) ? "online" : "participantOffline");
    const name = el("span", "part-name", member.user + (member.user === me ? ` (${t("participantMe")})` : ""));  // textContent로 XSS 방지
    row.append(dot, name);
    if (member.role === "owner") row.append(el("span", "part-role", t("participantOwner")));
    return row;
  }) : [el("div", "hint", t("participantEmpty"))]));
}

function renderInfoPanel(isOnline) {  // 오른쪽 상세 정보: 참여자(접속 중인 사람이 위)와 방 정보
  $("infoCount").textContent = participantList.length ? `${participantList.length} · ${t("online")} ${participantList.filter(isOnline).length}` : "";
  const rows = [...participantList].sort((a, b) => (b.role === "owner") - (a.role === "owner") || isOnline(b) - isOnline(a) || a.user.localeCompare(b.user));
  $("infoMembers").replaceChildren(...rows.map(member => {
    const row = el("div", "info-row"), av = el("div", isOnline(member) ? "av on" : "av", initial(member.user));
    av.style.background = color(member.user);
    row.append(av, el("span", "part-name", member.user + (member.user === me ? ` (${t("participantMe")})` : "")));  // textContent로 XSS 방지
    if (member.role === "owner") row.append(el("span", "part-role", t("participantOwner")));
    return row;
  }));
  if (currentRoom) $("infoAbout").textContent = t(currentRoom.visibility === "private" ? "vPrivate" : "vPublic");
}

function toggleParticipants() {
  if (participantsWin && participantsWin.open) { participantsWin.close(); participantsWin = null; return; }
  participantsWin = openFloatWindow(t("participants"), {width: 300, height: 360});
  participantsWin.onClose = () => { participantsWin = null; };
  renderParticipants();
  loadParticipants();  // 열 때마다 최신 참여자를 읽는다
}

$("participants").onclick = toggleParticipants;
