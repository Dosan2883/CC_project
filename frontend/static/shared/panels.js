/* 왼쪽 방 목록 접기/펼치기, 오른쪽 요약 목록 패널(접기/펼치기). 문서와 음성은 채팅의 파일 카드 바로 아래에서 보므로 패널에는 요약 목록만 둔다. */
let sideCollapsed = false;
let docOpen = false;          // 오른쪽 요약 목록 패널이 펼쳐져 있는지 (접으면 오른쪽 가장자리 버튼 줄만 남는다)
let summaryGeneration = 0;
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
  $("docFold").textContent = docOpen ? "→" : "←";  // 열려 있으면 오른쪽으로 접고, 접혀 있으면 왼쪽으로 펼친다
  $("docFold").title = t(docOpen ? "docFold" : "docUnfold");
  $("docTitle").textContent = t("summaryList");
}

function closeDocPanel() {  // 방을 바꾸거나 나갈 때: 목록을 비우고 접고, 재생 중인 음성은 멈춘다
  if (typeof stopAllAudio === "function") stopAllAudio();
  if (typeof closeAllFloatWindows === "function") closeAllFloatWindows();
  summaryGeneration++;
  $("sumView").replaceChildren();
  docOpen = false;
  applyPanels();
}

/* ---------- 요약 목록 ---------- */
function summaryMeta(entry) {
  const kind = entry.kind === "meeting-summary" ? t("roomSummary") : t("summarize");
  return `${entry.stale ? "이전 버전 · " : ""}${kind} · ${LANG_NAMES[entry.target] || entry.target} · ${entry.requester} · ${new Date(entry.updatedAt).toLocaleString()}`;
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

function refreshSummaries() { if (docOpen) loadSummaries(); }

/* ---------- 버튼 ---------- */
$("sideFold").onclick = $("sideOpen").onclick = () => {
  sideCollapsed = !sideCollapsed;
  try { localStorage.setItem("sideCollapsed", sideCollapsed ? "1" : "0"); } catch { /* 저장 실패는 무시 */ }
  applyPanels();
};
$("docFold").onclick = () => {
  docOpen = !docOpen;
  applyPanels();
  if (docOpen) loadSummaries();  // 펼칠 때마다 최신 요약 목록을 읽는다
};
applyPanels();
