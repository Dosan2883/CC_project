/* 왼쪽 방 목록 접기/펼치기, 오른쪽 패널(문서 보기 · 요약 목록). 화면 배치와 요약 목록 조회만 다룬다. */
let sideCollapsed = false;
let docOpen = false;          // 오른쪽 패널이 펼쳐져 있는지 (접으면 오른쪽 가장자리 버튼 줄만 남는다)
let panelView = "doc";        // "doc" | "files" | "summaries"
let docTitleText = "";
let docAudio = null;          // 패널에서 재생 중인 음성. 패널 내용을 바꾸거나 닫으면 멈춘다
let docMediaId = null;        // 지금 패널에 열려 있는 문서
let summaryGeneration = 0;
let fileGeneration = 0;
try { sideCollapsed = localStorage.getItem("sideCollapsed") === "1"; } catch { /* 저장소를 못 쓰면 기본값 */ }

function inRoom() { return typeof currentRoom !== "undefined" && !!currentRoom; }

function applyPanels() {
  const app = $("app");
  const state = !inRoom() ? "closed" : docOpen ? "open" : "folded";
  app.classList.toggle("side-collapsed", sideCollapsed);
  app.classList.toggle("doc-open", state === "open");
  app.classList.toggle("doc-folded", state === "folded");
  $("sideOpen").hidden = !sideCollapsed;
  $("sideFold").title = t("sideFold");
  $("sideOpen").title = t("sideOpen");
  $("docPanel").hidden = state === "closed";
  $("docPanel").dataset.state = state;
  $("docFold").textContent = docOpen ? "▶" : "◀";
  $("docFold").title = t(docOpen ? "docFold" : "docUnfold");
  $("sumToggle").title = t("summaryList");
  $("fileToggle").title = t("fileList");
  $("docBody").hidden = panelView !== "doc";
  $("fileView").hidden = panelView !== "files";
  $("sumView").hidden = panelView !== "summaries";
  $("docTitle").textContent = panelView === "summaries" ? t("summaryList") : panelView === "files" ? t("fileList") : docTitleText;
  $("docHint").textContent = t("docHint");
  $("docHint").hidden = panelView !== "doc" || $("docBody").children.length > 0;
}

function stopDocAudio() {
  if (docAudio) { docAudio.pause(); docAudio = null; }
}

function openDocPanel(title, content, mediaId = null, audio = null) {
  stopDocAudio();
  docAudio = audio;
  docTitleText = title;
  docMediaId = mediaId;
  panelView = "doc";
  $("docBody").replaceChildren(content);
  content.hidden = false;
  docOpen = true;
  applyPanels();
}

function closeDocPanel() {  // 방을 바꾸거나 나갈 때: 내용을 비우고 접는다
  stopDocAudio();
  summaryGeneration++; fileGeneration++;
  $("docBody").replaceChildren();
  $("sumView").replaceChildren();
  $("fileView").replaceChildren();
  docTitleText = "";
  docMediaId = null;
  panelView = "doc";
  docOpen = false;
  applyPanels();
}

/* ---------- 문서 목록 ---------- */
async function deleteFile(file) {
  await api(`/api/files/${file.mediaId}`, "DELETE");
  toast(t("fileDeleted"));
  if (docMediaId === file.mediaId) { stopDocAudio(); $("docBody").replaceChildren(); docTitleText = ""; docMediaId = null; }  // 열려 있던 본문도 닫는다
  if (typeof cards !== "undefined" && cards[file.mediaId]) cards[file.mediaId]({status: "DELETED", progress: 100, errorCode: null, needsOcr: false});
  refreshFiles();
}

const FILE_KINDS = [["document", "kindDocument"], ["image", "kindImage"], ["audio", "kindAudio"], ["video", "kindVideo"]];
let fileKind = "document", fileKindChosen = false, lastFiles = [];

function renderFiles(files) {
  lastFiles = files;
  const box = $("fileView");
  box.replaceChildren();
  if (!fileKindChosen && files.length) fileKind = files[0].kind;  // 처음에는 가장 최근 파일의 종류를 보여 준다
  const tabs = el("div", "tabs file-tabs");
  for (const [kind, label] of FILE_KINDS) {
    const count = files.filter(file => file.kind === kind).length;
    const tab = el("button", kind === fileKind ? "on" : null, `${t(label)} (${count})`); tab.type = "button";
    tab.onclick = () => { fileKind = kind; fileKindChosen = true; renderFiles(lastFiles); };
    tabs.append(tab);
  }
  box.append(tabs);
  const shown = files.filter(file => file.kind === fileKind);
  if (!shown.length) { box.append(el("div", "hint", t("fileEmptyKind"))); return; }
  for (const file of shown) {
    const item = el("div", "sum-item"), act = el("div", "sum-act");
    const ready = file.status === "COMPLETED";
    const meta = [(file.ext || "").toUpperCase(), fmtSize(file.size), file.sender, new Date(file.sentAt).toLocaleString(), t("st" + file.status)];
    const open = el("button", null, t("viewText")); open.type = "button"; open.disabled = !ready;
    open.onclick = () => openDocument(file.mediaId, file.filename, file.ext);
    act.append(open);
    if (file.sender === me) {  // 올린 사람만 지울 수 있다
      const remove = el("button", "danger", t("fileDelete")); remove.type = "button";
      remove.onclick = async () => {
        if (!confirm(t("fileDeleteConfirm"))) return;
        try { await deleteFile(file); } catch { toast(t("failed")); }
      };
      act.append(remove);
    }
    item.append(el("strong", null, file.filename), el("small", null, meta.filter(Boolean).join(" · ")), act);
    box.append(item);
  }
}

async function loadFiles() {
  if (!inRoom()) return;
  const roomId = currentRoom.roomId, generation = ++fileGeneration, authToken = token;
  try {
    const files = await api(`/api/rooms/${roomId}/files`);
    if (generation !== fileGeneration || !inRoom() || currentRoom.roomId !== roomId || token !== authToken) return;
    renderFiles(files);
  } catch {
    if (generation === fileGeneration) $("fileView").replaceChildren(el("div", "hint", t("failed")));
  }
}

function refreshFiles() { if (docOpen && panelView === "files") loadFiles(); }

/* ---------- 요약 목록 ---------- */
function summaryMeta(entry) {
  const kind = entry.kind === "meeting-summary" ? t("roomSummary") : t("summarize");
  return `${kind} · ${LANG_NAMES[entry.target] || entry.target} · ${entry.requester} · ${new Date(entry.updatedAt).toLocaleString()}`;
}

async function deleteSummary(roomId, entryId) {
  await api(`/api/ai/rooms/${roomId}/summaries/${entryId}`, "DELETE");
  toast(t("summaryDeleted"));
  refreshSummaries();
}

async function renameSummary(roomId, entryId, title) {
  const result = await api(`/api/ai/rooms/${roomId}/summaries/${entryId}`, "PATCH", {title});
  toast(t("summaryRenamed"));
  refreshSummaries();
  return result;
}

function renderSummaries(roomId, entries) {
  const box = $("sumView");
  box.replaceChildren();
  if (!entries.length) { box.append(el("div", "hint", t("summaryEmpty"))); return; }
  for (const entry of entries) {
    const item = el("div", "sum-item"), act = el("div", "sum-act");
    const detail = el("pre"); detail.hidden = true;
    const view = el("button", null, t("summaryView")); view.type = "button";
    view.onclick = async () => {
      if (!detail.hidden) { detail.hidden = true; view.textContent = t("summaryView"); return; }
      view.disabled = true;
      try {
        const full = await api(`/api/ai/rooms/${roomId}/summaries/${entry.entryId}`);
        detail.textContent = full.result.text;  // textContent로 XSS 방지
        detail.hidden = false; view.textContent = t("summaryHide");
      } catch { toast(t("failed")); }
      finally { view.disabled = false; }
    };
    act.append(view);
    if (entry.requester === me) {
      const rename = el("button", null, t("summaryRename")); rename.type = "button";
      rename.onclick = async () => {
        const next = prompt(t("summaryNamePrompt"), entry.title);
        if (next === null || !next.trim()) return;
        try { await renameSummary(roomId, entry.entryId, next); } catch { toast(t("failed")); }
      };
      act.append(rename);
      const remove = el("button", "danger", t("summaryDelete")); remove.type = "button";
      remove.onclick = async () => {
        if (!confirm(t("summaryDeleteConfirm"))) return;
        try { await deleteSummary(roomId, entry.entryId); } catch { toast(t("failed")); }
      };
      act.append(remove);
    }
    item.append(el("strong", null, entry.title), el("small", null, summaryMeta(entry)), act, detail);
    box.append(item);
  }
}

async function loadSummaries() {
  if (!inRoom()) return;
  const roomId = currentRoom.roomId, generation = ++summaryGeneration, authToken = token;
  try {
    const entries = await api(`/api/ai/rooms/${roomId}/summaries`);
    if (generation !== summaryGeneration || !inRoom() || currentRoom.roomId !== roomId || token !== authToken) return;
    renderSummaries(roomId, entries);
  } catch {
    if (generation === summaryGeneration) $("sumView").replaceChildren(el("div", "hint", t("failed")));
  }
}

function refreshSummaries() { if (docOpen && panelView === "summaries") loadSummaries(); }

/* ---------- 버튼 ---------- */
$("sideFold").onclick = $("sideOpen").onclick = () => {
  sideCollapsed = !sideCollapsed;
  try { localStorage.setItem("sideCollapsed", sideCollapsed ? "1" : "0"); } catch { /* 저장 실패는 무시 */ }
  applyPanels();
};
$("docFold").onclick = () => { docOpen = !docOpen; applyPanels(); };
$("fileToggle").onclick = () => {
  if (docOpen && panelView === "files") { docOpen = false; applyPanels(); return; }
  panelView = "files"; docOpen = true;
  applyPanels();
  loadFiles();
};
$("sumToggle").onclick = () => {
  if (docOpen && panelView === "summaries") { docOpen = false; applyPanels(); return; }
  panelView = "summaries"; docOpen = true;
  applyPanels();
  loadSummaries();
};
applyPanels();
