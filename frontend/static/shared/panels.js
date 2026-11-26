/* 왼쪽 방 목록 접기/펼치기, 오른쪽 요약 목록 패널. 패널은 상단의 회의 요약 버튼으로 열고 닫는다(별도 버튼 없음). 문서와 음성은 채팅의 파일 카드 바로 아래에서 본다. */
let sideCollapsed = false;
let docOpen = false;          // 오른쪽 요약 목록 패널이 열려 있는지 (닫으면 패널 자체가 사라진다)
let summaryGeneration = 0;
try { sideCollapsed = localStorage.getItem("sideCollapsed") === "1"; } catch { /* 저장소를 못 쓰면 기본값 */ }

function inRoom() { return typeof currentRoom !== "undefined" && !!currentRoom; }

function applyPanels() {
  const app = $("app"), open = inRoom() && docOpen;
  app.classList.toggle("side-collapsed", sideCollapsed);
  app.classList.toggle("doc-open", open);
  app.classList.toggle("summary-selecting", open);  // 패널이 열려 있는 동안에만 메시지마다 선택 칸이 보인다
  $("sideOpen").hidden = !sideCollapsed;
  $("sideOpen").title = t("sideOpen");
  $("docPanel").hidden = false;  // 숨김 속성은 쓰지 않는다. 닫힌 패널은 CSS로 서서히 사라지고, 아래 inert로 누르기·탭 이동을 막는다
  $("docPanel").inert = !open;  // 닫혀도 화면에는 남아 있어서 부드럽게 사라진다. 닫힌 동안에는 누르거나 탭으로 갈 수 없다
  $("docTitle").textContent = t("summaryList");
  $("roomSummary").ariaExpanded = String(open);
  refreshSelectionBar();
}

/* 요약 목록 위쪽의 선택 막대: 고른 메시지 수를 보여 주고, 고른 것으로 회의 요약을 만든다 */
function refreshSelectionBar() {
  const count = typeof aiSelections === "undefined" ? 0 : aiSelections.size;
  $("sumCount").textContent = t("summarySelected").replace("{n}", count);
  $("sumGenerate").disabled = $("sumClear").disabled = count === 0;
}

function clearSelection() {
  if (typeof aiSelections === "undefined") return;
  for (const input of document.querySelectorAll(".summary-choice")) input.checked = false;
  aiSelections.clear();
}

function closeDocPanel() {  // 방을 바꾸거나 나갈 때: 목록을 비우고 접고, 재생 중인 음성은 멈춘다
  if (typeof stopAllAudio === "function") stopAllAudio();
  if (typeof closeAllFloatWindows === "function") closeAllFloatWindows();
  summaryGeneration++;
  $("sumView").replaceChildren();
  clearSelection();
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
  title = title.trim();
  if (!title) return;
  if ([...title].length > 60) { toast(t("summaryTitleLimit")); return; }
  const result = await api(`/api/ai/rooms/${roomId}/summaries/${entryId}`, "PATCH", {title});
  toast(t("summaryRenamed"));
  refreshSummaries();
  return result;
}

function renderSummaries(roomId, entries) {
  const box = $("sumView");
  const generation = summaryGeneration, session = token;
  const active = () => docOpen && currentRoom?.roomId === roomId && generation === summaryGeneration && session === token;
  box.replaceChildren();
  if (!entries.length) { box.append(el("div", "hint", t("summaryEmpty"))); return; }
  for (const entry of entries) {
    const item = el("div", "sum-item"), act = el("div", "sum-act");
    const detail = el("pre"); detail.hidden = true;
    const finder = el("div"); finder.hidden = true;
    const view = el("button", null, t("summaryView")); view.type = "button";
    view.onclick = async () => {
      if (!detail.hidden) {
        detail.hidden = finder.hidden = true;
        finder.replaceChildren();
        view.textContent = t("summaryView");
        return;
      }
      view.disabled = true;
      try {
        const full = await api(`/api/ai/rooms/${roomId}/summaries/${entry.entryId}`);
        if (!active()) return;
        detail.textContent = full.result.text;  // textContent로 XSS 방지
        detail.hidden = false; view.textContent = t("summaryHide");
        if (!full.stale && full.sources?.length) {
          addDeadlineFinder(finder, full.sources, () => active() && !detail.hidden);
          finder.hidden = false;
        }
      } catch { if (active()) toast(t("failed")); }
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
    item.append(el("strong", null, entry.title), el("small", null, summaryMeta(entry)), act, detail, finder);
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

/* 요약 창 폭: 왼쪽 가장자리를 끌거나 좌우 방향키로 조절하고, 두 번 누르면 기본 크기(상세 정보와 같은 폭)로 돌아간다. 마지막 폭은 기억한다.
   폭의 숫자는 여기에만 두고, CSS는 이 값(--doc-open-w)을 그대로 쓴다. */
const DOC = {base:300, min:260, keep:724};  // 기본 폭, 최소 폭, 레일·방 목록·채팅에 남길 폭
let docWidth = DOC.base, docDragging = false;
try { docWidth = Number(localStorage.getItem("docWidth")) || DOC.base; } catch { /* 저장소를 못 쓰면 기본값 */ }
const docMax = () => Math.max(DOC.min, (globalThis.innerWidth || 0) - DOC.keep);
const fitDocWidth = width => Math.round(Math.max(DOC.min, Math.min(width, docMax())));

function applyDocWidth() {  // 기억한 폭이 지금 화면보다 넓으면 줄여서 보여 준다(기억한 값은 그대로)
  const shown = fitDocWidth(docWidth), handle = $("docResize");
  $("app").style.setProperty?.("--doc-open-w", `${shown}px`);
  handle.ariaValueMin = String(DOC.min); handle.ariaValueMax = String(docMax()); handle.ariaValueNow = String(shown);
}

function saveDocWidth() {
  try { localStorage.setItem("docWidth", String(docWidth)); } catch { /* 저장 실패는 무시 */ }
}

function setDocWidth(width) { docWidth = fitDocWidth(width); applyDocWidth(); }

function stopDocResize() {
  if (!docDragging) return;
  docDragging = false; $("app").classList.remove("doc-resizing"); saveDocWidth();
}

$("docResize").onpointerdown = event => { event.preventDefault(); docDragging = true; $("docResize").setPointerCapture(event.pointerId); $("app").classList.add("doc-resizing"); };
$("docResize").onpointermove = event => { if (docDragging) setDocWidth(innerWidth - event.clientX); };  // 창 오른쪽 끝에서 마우스까지가 요약 창 폭
$("docResize").onpointerup = $("docResize").onpointercancel = $("docResize").onlostpointercapture = stopDocResize;
$("docResize").ondblclick = () => { setDocWidth(DOC.base); saveDocWidth(); };
$("docResize").onkeydown = event => {
  const step = {ArrowLeft:20, ArrowRight:-20}[event.key];
  if (step) { event.preventDefault(); setDocWidth(fitDocWidth(docWidth) + step); saveDocWidth(); }
};
globalThis.addEventListener?.("resize", applyDocWidth);
applyDocWidth();

/* ---------- 버튼 ---------- */
function toggleSideList() {  // 왼쪽 레일의 채팅 버튼(펼치기·접기)과 접힌 뒤 나타나는 → 버튼이 같이 쓴다
  sideCollapsed = !sideCollapsed;
  try { localStorage.setItem("sideCollapsed", sideCollapsed ? "1" : "0"); } catch { /* 저장 실패는 무시 */ }
  applyPanels();
}
$("sideOpen").onclick = toggleSideList;
function toggleSummaryPanel(open = !docOpen) {  // 회의 요약 버튼이 요약 목록 패널을 열고 닫는다
  if (!inRoom()) return;
  docOpen = open;
  if (!docOpen) clearSelection();
  applyPanels();
  if (docOpen) loadSummaries();  // 열 때마다 최신 요약 목록을 읽는다
}
$("roomSummary").onclick = () => toggleSummaryPanel();
$("docFold").onclick = () => toggleSummaryPanel(false);
$("sumGenerate").onclick = () => runAi([...aiSelections.values()], "meeting-summary");
$("sumClear").onclick = clearSelection;
applyPanels();
