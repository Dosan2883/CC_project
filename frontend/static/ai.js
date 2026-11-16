/* Selected server-side sources only; no browser-supplied source text. */
const aiSelections = new Map();
/* 요약·번역 근거 원문을 보여 주고, 가능한 항목은 대상 언어로 번역해 바꿔 보여 준다. */
function evidenceLabel(e) {
  return e.source.kind === "message" ? `메시지 #${e.source.messageSeq} · ${new Date(e.sentAt).toLocaleString()}`
    : e.source.kind === "page" ? `파일 ${e.source.mediaId}, ${e.source.page}쪽`
    : e.source.kind === "transcript" ? `전체 녹취 · 파일 ${e.source.mediaId}` : `녹취 ${e.source.start}–${e.source.end}초`;
}
async function translateEvidence(e, target) {
  const partial = e.source.startChar > 0 || e.source.endChar != null;  // 일부 선택이면 서버 번역(전체 대상)과 범위가 달라 번역하지 않는다
  if (partial) return null;
  if (e.source.kind === "message")
    return (await api("/api/ai/translate", "POST", {roomId: currentRoom.roomId, messageSeq: e.source.messageSeq, target})).translated;
  if (e.source.kind === "page")
    return (await api("/api/ai/translate-page", "POST", {mediaId: e.source.mediaId, page: e.source.page, target})).translated;
  if (e.source.kind === "transcript") {
    const result = await api("/api/ai/translate-segments", "POST", {mediaId:e.source.mediaId, target});
    return result.segments.map(segment => segment.translated).join(" ");
  }
  return null;
}
function showEvidence(box, evidence, target, active, allowTranslation = true) {
  box.textContent = "";
  const head = document.createElement("p"); head.textContent = "선택한 원문 근거:"; box.append(head);
  for (const e of evidence) {
    const item = document.createElement("pre"); item.style.whiteSpace = "pre-wrap";
    item.textContent = `[${e.id}] ${evidenceLabel(e)}\n${e.text}`;
    box.append(item);
    const translate = document.createElement("button"); translate.type = "button"; translate.textContent = "근거 번역 보기";
    translate.onclick = async () => { if (!active()) return; translate.disabled = true; try { const translated = await translateEvidence(e, target);
      if (translated && translated.trim() !== e.text.trim() && active())
        item.textContent = `[${e.id}] ${evidenceLabel(e)}\n${translated}\n\n(원문)\n${e.text}`;
      else if (translated && active()) item.textContent += "\n\n(번역 결과가 원문과 같습니다)";
      else if (!translated && active()) item.textContent += "\n\n(이 근거는 번역하지 않습니다: 일부 구간 선택 또는 녹취)";
    } catch (error) {
      if (active()) item.textContent += `\n\n(번역 실패: ${error.code || error.status || error.message})`;
    } finally { translate.disabled = false; } };
    if (allowTranslation) box.append(translate);
  }
}
async function runAi(sources, kind = "summarize") {
  if (!currentRoom || !sources.length) return toast("메시지 또는 문단을 먼저 선택하세요.");
  if (!hasTranslateConsent()) {
    if (!confirm(t("translateConsent"))) return;
    rememberTranslateConsent();
  }
  const roomId = currentRoom.roomId, generation = viewGeneration, authToken = token;
  const panelMode = kind === "meeting-summary";
  const panelSession = panelMode ? ++summarySession : summarySession;
  const dialog = panelMode ? $("summaryResult") : document.createElement("dialog"), state = document.createElement("p"), output = document.createElement("pre");
  if (panelMode) { $("summaryPanel").hidden = false; dialog.replaceChildren(); }
  output.style.whiteSpace = "pre-wrap";
  const close = document.createElement("button"); close.textContent = "닫기"; close.onclick = () => { dialog.close(); dialog.remove(); };
  const evidenceBox = document.createElement("div");
  if (panelMode) dialog.append(state, output, evidenceBox);
  else { dialog.append(state, output, evidenceBox, close); document.body.append(dialog); dialog.showModal(); }
  let jobId;
  const active = () => (panelMode ? !$("summaryPanel").hidden && summarySession === panelSession : dialog.open) && currentRoom?.roomId === roomId && generation === viewGeneration && token === authToken;
  async function poll() {
    for (let i = 0; i < 900 && active(); i++) {
      const job = await api(`/api/ai/jobs/${jobId}`);
      if (!active()) return;
      state.textContent = `${job.status} · 시도 ${job.attempts}/3`;
      if (job.status === "COMPLETED") {
        output.textContent = job.result.text;
        showEvidence(evidenceBox, job.result.evidence, job.target, active);
        if (panelMode) { stopSummarySelection(); loadSummaryList(); }
        return;
      }
      if (job.status === "FAILED") {
        state.textContent += ` · ${aiError({code:job.errorCode})}`;
        const retryButton = document.createElement("button"); retryButton.textContent = t("retry");
        retryButton.onclick = async () => { retryButton.remove(); try { jobId = (await api(`/api/ai/jobs/${jobId}/retry`, "POST")).jobId; await poll(); } catch (e) { state.textContent = e.code || t("failed"); } };
        dialog.append(retryButton); return;
      }
      await new Promise(resolve => setTimeout(resolve, 1000));
    }
    if (active()) state.textContent = "대기 시간이 지났습니다. 작업 번호: " + jobId;
  }
  try {
    state.textContent = "필요한 원문 확인·추출 중…";
    for (const mediaId of new Set(sources.filter(s => s.kind !== "message").map(s => s.mediaId))) {
      await ensureExtracted(mediaId, active);
      if (!active()) return;
    }
    state.textContent = "저장 결과 확인·선택 자료 처리 중…";
    jobId = (await api("/api/ai/jobs", "POST", {roomId, kind, sourceLanguage:"auto", target:targetLang, sources, consent:true})).jobId;
    await poll();
  } catch (e) { if (active()) state.textContent = aiError(e); }
}

$("roomSummary").classList.remove("soon");
$("roomSummary").onclick = () => { if (!$("summaryPanel").hidden) return resetAiPanel(); $("summaryPanel").hidden = false; loadSummaryList(); };

const aiError = error => ({SELECTION_TOO_LONG:"선택량은 최대 40,000자입니다. 페이지나 문단을 나눠 선택하세요.", TOO_MANY_CHUNKS:"최대 16개 조각까지 처리합니다. 범위를 줄여 주세요.", OCR_REQUIRED:"스캔 페이지 인식이 필요합니다. OCR 결과를 확인·수정한 뒤 다시 요청하세요.", EMPTY_SELECTION:"추출된 텍스트가 없습니다. OCR 또는 원문 수정을 확인하세요.", SOURCE_VERSION_CHANGED:"원문이 변경됐습니다. 최신 원문으로 다시 요청하세요.", AI_CALL_UNCERTAIN:"외부 호출 후 저장 상태를 확인해야 합니다. 중복 호출을 막기 위해 자동 재생성을 중단했습니다.", AI_UNAVAILABLE:"AI 응답 처리에 실패했습니다. 실패한 조각만 재시도합니다."}[error.code] || error.code || t("failed"));
async function ensureExtracted(mediaId, active) {
  let state = await api(`/api/media/${mediaId}`);
  if (!active()) return;
  if (state.status === "UPLOADED") state = await api(`/api/media/${mediaId}/process`, "POST");
  for (let i = 0; i < 300 && active(); i++) {
    cards[mediaId]?.(state);
    if (state.status === "COMPLETED") return;
    if (state.status === "FAILED") throw {code:state.errorCode || "EXTRACTION_FAILED"};
    await new Promise(resolve => setTimeout(resolve, 1000));
    state = await api(`/api/media/${mediaId}`);
  }
  if (active()) throw {code:"EXTRACTION_PENDING"};
}
let summaryGeneration = 0, summarySession = 0, summaryController;
const selectionSizes = new Map();
function updateSummarySelection(key, source, size, checked) {
  if (checked) { aiSelections.set(key, source); selectionSizes.set(key, size); }
  else { aiSelections.delete(key); selectionSizes.delete(key); }
  $("summaryCount").textContent = `${aiSelections.size}개 · ${[...selectionSizes.values()].reduce((a,b)=>a+b,0)}자 · ${LANG_NAMES[targetLang] || targetLang}`;
}
function stopSummarySelection() {
  $("app").classList.remove("summary-selecting"); aiSelections.clear(); selectionSizes.clear();
  for (const input of document.querySelectorAll(".summary-choice")) input.checked = false;
  $("summarySelection").hidden = true;
}
function resetAiPanel() { summaryGeneration++; summarySession++; summaryController?.abort(); $("summaryPanel").hidden = true; $("summaryList").replaceChildren(); $("summaryResult").replaceChildren(); stopSummarySelection(); }
$("summaryClose").onclick = resetAiPanel;
$("summaryAdd").onclick = () => { stopSummarySelection(); $("app").classList.add("summary-selecting"); $("summarySelection").hidden = false; $("summaryCount").textContent = `0개 · ${targetLang}`; };
$("summaryCancel").onclick = stopSummarySelection;
$("summaryGenerate").onclick = () => runAi([...aiSelections.values()], "meeting-summary");
async function loadSummaryList() {
  if (!currentRoom) return;
  summaryController?.abort(); summaryController = new AbortController();
  const generation = ++summaryGeneration, roomId = currentRoom.roomId, authToken = token;
  try {
    const results = await api(`/api/rooms/${roomId}/ai-results`, "GET", undefined, {signal:summaryController.signal});
    if (generation !== summaryGeneration || currentRoom?.roomId !== roomId || token !== authToken) return;
    $("summaryList").replaceChildren();
    for (const result of results.filter(r=>r.kind !== "translate")) {
      const button = document.createElement("button"); button.type = "button";
      button.textContent = `${result.title} · ${result.target} · ${new Date(result.createdAt).toLocaleString()} · 원문 v${[...new Set((result.sourceVersions || []).map(s=>s.version))].join(",") || "이전"}${result.stale ? " · 이전 원문 버전" : ""}`;
      button.onclick = async () => {
        try {
          const job = await api(`/api/rooms/${roomId}/ai-results/${result.jobId}?historical=true`);
          if (generation !== summaryGeneration || currentRoom?.roomId !== roomId || token !== authToken) return;
          $("summaryResult").replaceChildren();
          const output = document.createElement("pre"); output.textContent = (job.stale ? "이전 원문 버전의 저장 결과\n\n" : "") + job.result.text;
          $("summaryResult").append(output); const evidence = document.createElement("div"); $("summaryResult").append(evidence);
          showEvidence(evidence, job.result.evidence, job.target, ()=>generation === summaryGeneration && currentRoom?.roomId === roomId, !job.stale);
        } catch(e) { toast(aiError(e)); }
      };
      $("summaryList").append(button);
    }
  } catch(e) { if (e.name !== "AbortError" && generation === summaryGeneration) toast(aiError(e)); }
}

function pageReview(mediaId, page, pre, transcript) {
  const controls = document.createElement("div");
  const language = document.createElement("select");
  for (const code of ["ko", "en", "ja", "zh-CN", "es", "fr"]) language.add(new Option(LANG_NAMES[code] || code, code));
  const detail = transcript.pageDetails?.[page - 1];
  language.value = transcript.corrections?.[String(page)]?.language || detail?.language || "ko";
  const note = document.createElement("p"); note.textContent = detail ? `${detail.method} · ${detail.model || "텍스트 추출"} · ${detail.language || "미지정"}${detail.needsReview ? " · 원문 확인 필요" : ""}${detail.error ? " · " + detail.error : ""}` : "추출 원문";
  const editor = document.createElement("textarea"); editor.value = transcript.pages[page - 1] || ""; editor.rows = 8; editor.style.width = "100%";
  const button = (label, action) => { const b = document.createElement("button"); b.type = "button"; b.textContent = label; b.onclick = action; return b; };
  const source = () => ({kind:"page", mediaId, page, startChar:[...editor.value.slice(0, editor.selectionStart)].length, ...(editor.selectionEnd > editor.selectionStart ? {endChar:[...editor.value.slice(0, editor.selectionEnd)].length} : {startChar:0})});
  controls.append(note, language, editor);
  const include = document.createElement("input"); include.type = "checkbox";
  include.className = "summary-choice";
  include.onchange = () => updateSummarySelection(`${mediaId}:${page}`, {kind:"page", mediaId, page}, transcript.pages[page-1].length, include.checked);
  controls.append(include, document.createTextNode("회의 요약에 이 페이지 포함"));
  controls.append(button("수정 저장", async () => {
    try { const r = await api(`/api/media/${mediaId}/pages/${page}`, "PATCH", {version:transcript.version || 1, text:editor.value, language:language.value}); Object.assign(transcript, r); pre.textContent = editor.value; for (const old of pre.parentElement.querySelectorAll(".translation")) old.remove(); toast("저장했습니다."); }
    catch (e) { toast(e.code || t("failed")); }
  }));
  for (const [label, kind] of [["선택 범위 번역", "translate"], ["선택 범위 요약", "summarize"]]) controls.append(button(label, () => {
    if (editor.value !== transcript.pages[page - 1]) return toast("수정한 원문을 먼저 저장하세요.");
    if (editor.selectionStart === editor.selectionEnd) return toast("처리할 문단을 선택하세요. 페이지 전체는 파일 카드의 페이지 버튼을 이용하세요.");
    runAi([source()], kind);
  }));
  controls.append(button("이 언어로 OCR 다시 실행", async () => {
    try { const state = await api(`/api/media/${mediaId}/process?language=${encodeURIComponent(language.value)}`, "POST"); cards[mediaId]?.(state); toast("OCR 재처리를 요청했습니다. 완료 후 본문을 다시 여세요."); }
    catch (e) { toast(e.code || t("failed")); }
  }));
  controls.append(button("페이지 이미지·인식 영역 확인", async () => {
    let url;
    const generation = viewGeneration, authToken = token;
    try {
      const response = await fetch(`/api/media/${mediaId}/pages/${page}/image`, {headers:{Authorization:"Bearer " + authToken}});
      if (!response.ok) throw new Error();
      const blob = await response.blob();
      if (viewGeneration !== generation || token !== authToken) return;
      const dialog = document.createElement("dialog"), image = document.createElement("img"), wrap = document.createElement("div");
      wrap.style.cssText = "position:relative;max-width:80vw"; image.style.width = "100%"; image.src = url = URL.createObjectURL(blob); wrap.append(image);
      for (const region of detail?.regions || []) {
        const xs = region.polygon.map(p=>p[0]), ys = region.polygon.map(p=>p[1]), mark = document.createElement("span");
        mark.title = `${region.text} (${Math.round(region.confidence * 100)}%)`;
        mark.style.cssText = `position:absolute;border:1px solid red;left:${Math.min(...xs)/detail.width*100}%;top:${Math.min(...ys)/detail.height*100}%;width:${(Math.max(...xs)-Math.min(...xs))/detail.width*100}%;height:${(Math.max(...ys)-Math.min(...ys))/detail.height*100}%`;
        wrap.append(mark);
      }
      dialog.append(wrap, button("닫기", () => dialog.close())); dialog.onclose = () => { URL.revokeObjectURL(url); dialog.remove(); };
      document.body.append(dialog); dialog.showModal();
    } catch { if (url) URL.revokeObjectURL(url); toast("이 파일의 페이지 이미지를 열 수 없습니다."); }
  }));
  pre.before(controls);
}

async function previewOriginal(mediaId, ext, filename) {
  const generation = viewGeneration, authToken = token;
  try {
    const response = await fetch(`/api/media/${mediaId}/original`, {headers:{Authorization:"Bearer " + authToken}});
    if (!response.ok) throw new Error();
    const blob = await response.blob();
    if (viewGeneration !== generation || token !== authToken) return;
    const url = URL.createObjectURL(blob), dialog = document.createElement("dialog"), close = document.createElement("button");
    close.textContent = "닫기"; close.onclick = () => dialog.close();
    if (ext === "pdf") {
      const frame = document.createElement("iframe"); frame.title = filename; frame.src = url; frame.style.cssText = "display:block;width:80vw;height:75vh;border:0"; dialog.append(frame);
    } else if (["png", "jpg", "jpeg"].includes(ext)) {
      const image = document.createElement("img"); image.src = url; image.alt = filename; image.style.maxWidth = "80vw"; dialog.append(image);
    } else {
      const link = document.createElement("a"); link.href = url; link.download = filename; link.textContent = "원본 내려받기"; dialog.append(link);
    }
    dialog.append(close); dialog.onclose = () => { URL.revokeObjectURL(url); dialog.remove(); };
    document.body.append(dialog); dialog.showModal();
  } catch { toast("원본을 불러오지 못했습니다."); }
}
