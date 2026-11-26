/* Selected server-side sources only; no browser-supplied source text. */
/* 요약 목록 위쪽 선택 막대가 개수를 바로 보여 주도록, 선택이 바뀔 때마다 막대를 갱신한다. */
class SelectionMap extends Map {
  set(key, value) { super.set(key, value); refreshSelectionBar(); return this; }
  delete(key) { const removed = super.delete(key); refreshSelectionBar(); return removed; }
  clear() { super.clear(); refreshSelectionBar(); }
}
const aiSelections = new SelectionMap();
const MAX_AI_SOURCES = 20;
/* chat/index.js와 auth/index.js가 부르는 함수. 방을 바꾸거나 나가면 선택을 비우고, 메시지 체크박스는 회의 요약 선택(aiSelections)에 반영한다. */
function resetAiPanel() { aiSelections.clear(); }
function updateSummarySelection(key, source, checked) {
  key = String(key);
  if (checked && !aiSelections.has(key) && aiSelections.size >= MAX_AI_SOURCES) {
    toast(t("summarySourceLimit"));
    return false;
  }
  if (checked) aiSelections.set(key, source); else aiSelections.delete(key);
  return checked;
}
/* 요약·번역 근거 원문을 보여 주고, 가능한 항목은 대상 언어로 번역해 바꿔 보여 준다. */
function evidenceLabel(e) {
  if (e.source.kind === "transcript") return t("audioWhole");
  return e.source.kind === "message" ? `메시지 #${e.source.messageSeq} · ${new Date(e.sentAt).toLocaleString()}`
    : e.source.kind === "page" ? `파일 ${e.source.mediaId}, ${e.source.page}쪽` : `녹취 ${e.source.start}–${e.source.end}초`;
}
async function translateEvidence(e, target) {
  const partial = e.source.startChar > 0 || e.source.endChar != null;  // 일부 선택이면 서버 번역(전체 대상)과 범위가 달라 번역하지 않는다
  if (partial) return null;
  if (e.source.kind === "message")
    return (await api("/api/ai/translate", "POST", {roomId: currentRoom.roomId, messageSeq: e.source.messageSeq, target})).translated;
  if (e.source.kind === "page")
    return (await api("/api/ai/translate-page", "POST", {mediaId: e.source.mediaId, page: e.source.page, target})).translated;
  return null;
}
function canTranslateEvidence(e) {
  const partial = e.source.startChar > 0 || e.source.endChar != null;
  return !partial && (e.source.kind === "message" || e.source.kind === "page");
}
/* 근거는 원문으로 보여 주고, 번역할 수 있는 근거에만 번역 버튼을 둔다. 누를 때만 번역을 부르므로 요약을 열 때 번역 호출이 나가지 않는다. */
function showEvidence(box, evidence, target, active) {
  box.textContent = "";
  const head = document.createElement("p"); head.textContent = "선택한 원문 근거:"; box.append(head);
  for (const e of evidence) {
    const row = document.createElement("div"), item = document.createElement("pre"); item.style.whiteSpace = "pre-wrap";
    item.textContent = `[${e.id}] ${evidenceLabel(e)}\n${e.text}`;
    row.append(item); box.append(row);
    if (!canTranslateEvidence(e)) continue;  // 일부 구간 선택, 녹취, 음성 전체는 번역 버튼이 없다
    const button = document.createElement("button"); button.type = "button"; button.textContent = t("translate");
    button.onclick = async () => {
      if (!await ensureAiConsent() || !active()) return;
      button.disabled = true;
      try {
        const translated = await translateEvidence(e, target);
        if (!active()) return;
        if (translated && translated.trim() !== e.text.trim()) item.textContent = `[${e.id}] ${evidenceLabel(e)}\n${translated}\n\n(원문)\n${e.text}`;
        else item.textContent += "\n\n(번역 결과가 원문과 같습니다)";
        button.remove();
      } catch (error) {
        if (active()) item.textContent += `\n\n(번역 실패: ${error.code || error.status || error.message})`;
        button.disabled = false;
      }
    };
    row.append(button);
  }
}
function summaryNameRow(roomId, entryId, active) {
  const row = document.createElement("div"), input = document.createElement("input"), save = document.createElement("button");
  row.className = "name-row"; input.maxLength = 60; input.placeholder = t("summaryNameLabel");
  save.type = "button"; save.textContent = t("summarySave");
  api(`/api/ai/rooms/${roomId}/summaries/${entryId}`).then(entry => { if (active() && !input.value) input.value = entry.title; }).catch(() => {});
  save.onclick = async () => {
    if (!input.value.trim()) return;
    save.disabled = true;
    try { await renameSummary(roomId, entryId, input.value); } catch { toast(t("failed")); }
    finally { save.disabled = false; }
  };
  row.append(input, save);
  return row;
}

async function runAi(sources, kind = "summarize") {
  if (!currentRoom || !sources.length) return toast("메시지 또는 문단을 먼저 선택하세요.");
  if (sources.length > MAX_AI_SOURCES) return toast(t("summarySourceLimit"));
  const roomId = currentRoom.roomId, generation = viewGeneration, authToken = token;
  if (!await ensureAiConsent() || currentRoom?.roomId !== roomId || viewGeneration !== generation || token !== authToken) return;
  // 번역은 번역 대상 언어로, 요약은 앱 표시 언어로 만든다. 근거 원문은 번역 대상 언어로 보여 준다.
  const firstSource = sources[0];
  const selectedTarget = targetLang;
  const outputLanguage = kind === "translate" ? selectedTarget : locale, evidenceLanguage = selectedTarget;
  const win = openFloatWindow(kind === "translate" ? t("translate") : kind === "meeting-summary" ? t("roomSummary") : t("summarize"), {width: 480, height: 480});
  const state = document.createElement("p"), output = document.createElement("pre");
  output.style.whiteSpace = "pre-wrap";
  const evidenceBox = document.createElement("div");
  const finderSlot = document.createElement("div"); finderSlot.className = "finder-slot";  // 요약과 원문 근거 사이: 일정 찾기가 들어갈 자리
  win.body.append(state, output, finderSlot, evidenceBox);  // 떠 있는 창이라 결과를 열어 둔 채 채팅을 계속 볼 수 있다
  let jobId;
  const active = () => win.open && currentRoom?.roomId === roomId && generation === viewGeneration && token === authToken;
  async function showExisting(info) {  // 이미 만들어진 요약이면 새로 만들지 않고 저장된 내용을 보여 준다
    const full = await api(`/api/ai/rooms/${roomId}/summaries/${info.entryId}`);
    if (!active()) return;
    setBusy(state, false);
    state.hidden = true;  // 저장된 요약은 안내 문구 없이 바로 보여 준다
    output.textContent = [full.title, full.result.text].join("\n\n");
    showEvidence(evidenceBox, full.result.evidence, evidenceLanguage, active);
    refreshSummaries();
    if (!full.stale && full.sources?.length) addDeadlineFinder(finderSlot, full.sources, active);
  }
  async function poll() {
    for (let i = 0; i < 180 && active(); i++) {
      const job = await api(`/api/ai/jobs/${jobId}`);
      if (!active()) return;
      state.textContent = `${job.status} · 시도 ${job.attempts}/3`;
      setBusy(state, job.status !== "COMPLETED" && job.status !== "FAILED");
      if (job.status === "COMPLETED") {
        output.textContent = job.result.text;
        if (job.entryId) output.after(summaryNameRow(roomId, job.entryId, active));  // 요약 이름 정하기 (목록에 이 이름으로 보인다)
        showEvidence(evidenceBox, job.result.evidence, evidenceLanguage, active);
        refreshSummaries();
        if (kind !== "translate" && typeof addDeadlineFinder === "function") addDeadlineFinder(finderSlot, sources, active);  // 요약에 쓴 자료에서 마감·일정 후보 찾기
        return;
      }
      if (job.status === "FAILED") {
        state.textContent += ` · ${aiErrorText(job.errorCode) || job.errorCode}`;
        const retryButton = document.createElement("button"); retryButton.textContent = t("retry");
        retryButton.onclick = async () => { retryButton.remove(); setBusy(state, true); try { const again = await api(`/api/ai/jobs/${jobId}/retry`, "POST"); if (again.existing) return await showExisting(again); jobId = again.jobId; await poll(); } catch (e) { state.textContent = e.code || t("failed"); setBusy(state, false); } };
        win.body.append(retryButton); return;
      }
      await new Promise(resolve => setTimeout(resolve, 1000));
    }
    if (active()) { state.textContent = "대기 시간이 지났습니다. 작업 번호: " + jobId; setBusy(state, false); }
  }
  try {
    state.textContent = "선택 자료 처리 중…"; setBusy(state, true);
    const created = await api("/api/ai/jobs", "POST", {roomId, kind, sourceLanguage: $("srcLang").value || "auto", target: outputLanguage, sources, consent: true});
    if (created.existing) return await showExisting(created);
    jobId = created.jobId;
    await poll();
  } catch (e) { if (active()) state.textContent = t("aiErr_" + e.code) || e.code || t("failed"); }
  finally { setBusy(state, false); }
}


function pageReview(mediaId, page, pre, transcript, {active = () => true, onCorrect, canOcr = false} = {}) {
  const controls = document.createElement("div");
  const language = document.createElement("select");
  for (const code of ["ko", "en", "ja", "zh-CN", "es", "fr"]) language.add(new Option(LANG_NAMES[code] || code, code));
  const detail = transcript.pageDetails?.[page - 1];
  language.value = transcript.corrections?.[String(page)]?.language || detail?.language || "ko";
  const note = document.createElement("p"); note.textContent = detail ? `${detail.method} · ${detail.model || "텍스트 추출"} · ${detail.language || "미지정"}${detail.needsReview ? " · 원문 확인 필요" : ""}${detail.error ? " · " + (t("fe_" + detail.error) || detail.error) : ""}` : "추출 원문";
  const editor = document.createElement("textarea"); editor.value = transcript.pages[page - 1] || ""; editor.rows = 8; editor.style.width = "100%";
  editor.maxLength = 12000;
  const button = (label, action) => { const b = document.createElement("button"); b.type = "button"; b.textContent = label; b.onclick = action; return b; };
  const source = () => ({kind:"page", mediaId, page, startChar:[...editor.value.slice(0, editor.selectionStart)].length, ...(editor.selectionEnd > editor.selectionStart ? {endChar:[...editor.value.slice(0, editor.selectionEnd)].length} : {startChar:0})});
  controls.append(note, language, editor);
  const include = document.createElement("input"); include.type = "checkbox";
  include.className = "summary-choice";
  const selectionKey = `${mediaId}:${page}`;
  include.checked = aiSelections.has(selectionKey);
  include.onchange = () => { include.checked = updateSummarySelection(selectionKey, {kind:"page", mediaId, page}, include.checked); };
  controls.append(include, document.createTextNode("회의 요약에 이 페이지 포함"));
  controls.append(button("수정 저장", async () => {
    try {
      const r = await api(`/api/media/${mediaId}/pages/${page}`, "PATCH", {version:transcript.version || 1, text:editor.value, language:language.value});
      if (!active()) return;
      Object.assign(transcript, r);
      onCorrect?.(r);
      document.dispatchEvent(new CustomEvent("source:updated", {detail:{mediaId}}));
      toast("저장했습니다.");
    } catch (e) { if (active()) toast(e.code || t("failed")); }
  }));
  for (const [label, kind] of [["선택 범위 번역", "translate"], ["선택 범위 요약", "summarize"]]) controls.append(button(label, () => {
    if (editor.value !== (transcript.pages[page - 1] || "")) return toast("수정한 원문을 먼저 저장하세요.");
    runAi([source()], kind);
  }));
  if (canOcr) controls.append(button("이 언어로 OCR 다시 실행", async () => {
    try {
      const state = await api(`/api/media/${mediaId}/process?language=${encodeURIComponent(language.value)}`, "POST");
      if (active()) cards[mediaId]?.(state);
    } catch (e) { if (active()) toast(t("fe_" + e.code) || e.code || t("failed")); }
  }));
  if (canOcr) controls.append(button("페이지 이미지·인식 영역 확인", async () => {
    let dispose;
    const generation = viewGeneration, authToken = token;
    try {
      const response = await fetch(`/api/media/${mediaId}/pages/${page}/image`, {headers:{Authorization:"Bearer " + authToken}});
      if (!response.ok) throw new Error();
      const blob = await response.blob();
      if (!active() || viewGeneration !== generation || token !== authToken) return;
      const dialog = document.createElement("dialog"), image = document.createElement("img"), wrap = document.createElement("div");
      const url = URL.createObjectURL(blob);
      dispose = () => {
        imagePreviews.delete(dispose);
        dialog.onclose = null;
        if (dialog.open) dialog.close();
        image.removeAttribute("src");
        URL.revokeObjectURL(url);
        dialog.remove();
      };
      imagePreviews.set(dispose, mediaId);
      wrap.style.cssText = "position:relative;max-width:80vw"; image.style.width = "100%"; image.src = url; wrap.append(image);
      for (const region of detail?.regions || []) {
        const xs = region.polygon.map(p=>p[0]), ys = region.polygon.map(p=>p[1]), mark = document.createElement("span");
        mark.title = `${region.text} (${Math.round(region.confidence * 100)}%)`;
        mark.style.cssText = `position:absolute;border:1px solid red;left:${Math.min(...xs)/detail.width*100}%;top:${Math.min(...ys)/detail.height*100}%;width:${(Math.max(...xs)-Math.min(...xs))/detail.width*100}%;height:${(Math.max(...ys)-Math.min(...ys))/detail.height*100}%`;
        wrap.append(mark);
      }
      dialog.append(wrap, button("닫기", () => dialog.close())); dialog.onclose = dispose;
      document.body.append(dialog); dialog.showModal();
    } catch { dispose?.(); if (active()) toast(t("pdfImageFail")); }
  }));
  const original = button("원문/수정본", () => { pre.textContent = pre.textContent === transcript.pages[page - 1] ? (transcript.originalPages?.[page - 1] ?? transcript.pages[page - 1]) : transcript.pages[page - 1]; });
  controls.append(original);
  return controls;
}
