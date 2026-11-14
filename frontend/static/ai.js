/* Selected server-side sources only; no browser-supplied source text. */
const aiSelections = new Map();
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
function showEvidence(box, evidence, target, active, translate = true) {
  box.textContent = "";
  const head = document.createElement("p"); head.textContent = "선택한 원문 근거:"; box.append(head);
  for (const e of evidence) {
    const item = document.createElement("pre"); item.style.whiteSpace = "pre-wrap";
    item.textContent = `[${e.id}] ${evidenceLabel(e)}\n${e.text}`;
    box.append(item);
    if (!translate) continue;  // 번역 작업은 결과 자체가 번역이라 근거를 또 번역하지 않는다
    translateEvidence(e, target).then(translated => {
      if (translated && translated.trim() !== e.text.trim() && active())
        item.textContent = `[${e.id}] ${evidenceLabel(e)}\n${translated}\n\n(원문)\n${e.text}`;
      else if (translated && active()) item.textContent += "\n\n(번역 결과가 원문과 같습니다)";
      // 번역할 수 없는 근거(일부 구간 선택, 녹취)는 안내 없이 원문만 보여 준다
    }).catch(error => {  // 번역에 실패하면 원문을 그대로 두고 이유를 표시한다
      if (active()) item.textContent += `\n\n(번역 실패: ${error.code || error.status || error.message})`;
    });
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
  if (!hasTranslateConsent()) {
    if (!confirm(t("translateConsent"))) return;
    rememberTranslateConsent();
  }
  const roomId = currentRoom.roomId, generation = viewGeneration, authToken = token;
  // 번역은 번역 대상 언어로, 요약은 앱 표시 언어로 만든다. 근거 원문은 번역 대상 언어로 보여 준다.
  const outputLanguage = kind === "translate" ? targetLang : locale, evidenceLanguage = targetLang;
  const dialog = document.createElement("dialog"), state = document.createElement("p"), output = document.createElement("pre");
  output.style.whiteSpace = "pre-wrap";
  const close = document.createElement("button"); close.textContent = "닫기"; close.onclick = () => { dialog.close(); dialog.remove(); };
  const evidenceBox = document.createElement("div");
  dialog.append(state, output, evidenceBox, close); document.body.append(dialog); dialog.showModal();
  let jobId;
  const active = () => dialog.open && currentRoom?.roomId === roomId && generation === viewGeneration && token === authToken;
  async function showExisting(info) {  // 이미 만들어진 요약이면 새로 만들지 않고 저장된 내용을 보여 준다
    state.textContent = t("summaryExists");
    const full = await api(`/api/ai/rooms/${roomId}/summaries/${info.entryId}`);
    if (!active()) return;
    output.textContent = [full.title, full.result.text].join("\n\n");
    showEvidence(evidenceBox, full.result.evidence, evidenceLanguage, active);
  }
  async function poll() {
    for (let i = 0; i < 180 && active(); i++) {
      const job = await api(`/api/ai/jobs/${jobId}`);
      if (!active()) return;
      state.textContent = `${job.status} · 시도 ${job.attempts}/3`;
      if (job.status === "COMPLETED") {
        output.textContent = job.result.text;
        if (job.entryId) output.after(summaryNameRow(roomId, job.entryId, active));  // 요약 이름 정하기 (목록에 이 이름으로 보인다)
        showEvidence(evidenceBox, job.result.evidence, evidenceLanguage, active, kind !== "translate");
        refreshSummaries();
        return;
      }
      if (job.status === "FAILED") {
        state.textContent += ` · ${job.errorCode}`;
        const retryButton = document.createElement("button"); retryButton.textContent = t("retry");
        retryButton.onclick = async () => { retryButton.remove(); try { const again = await api(`/api/ai/jobs/${jobId}/retry`, "POST"); if (again.existing) return await showExisting(again); jobId = again.jobId; await poll(); } catch (e) { state.textContent = e.code || t("failed"); } };
        dialog.append(retryButton); return;
      }
      await new Promise(resolve => setTimeout(resolve, 1000));
    }
    if (active()) state.textContent = "대기 시간이 지났습니다. 작업 번호: " + jobId;
  }
  try {
    state.textContent = "선택 자료 처리 중…";
    const created = await api("/api/ai/jobs", "POST", {roomId, kind, sourceLanguage: $("srcLang").value || "auto", target: outputLanguage, sources, consent: true});
    if (created.existing) return await showExisting(created);
    jobId = created.jobId;
    await poll();
  } catch (e) { if (active()) state.textContent = t("aiErr_" + e.code) || e.code || t("failed"); }
}

$("roomSummary").classList.remove("soon");
$("roomSummary").onclick = () => runAi([...aiSelections.values()], "meeting-summary");

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
  include.onchange = () => include.checked ? aiSelections.set(`${mediaId}:${page}`, {kind:"page", mediaId, page}) : aiSelections.delete(`${mediaId}:${page}`);
  controls.append(include, document.createTextNode("회의 요약에 이 페이지 포함"));
  controls.append(button("수정 저장", async () => {
    try { const r = await api(`/api/media/${mediaId}/pages/${page}`, "PATCH", {version:transcript.version || 1, text:editor.value, language:language.value}); Object.assign(transcript, r); pre.textContent = editor.value; for (const old of pre.parentElement.querySelectorAll(".translation")) old.remove(); toast("저장했습니다."); }
    catch (e) { toast(e.code || t("failed")); }
  }));
  for (const [label, kind] of [["선택 범위 번역", "translate"], ["선택 범위 요약", "summarize"]]) controls.append(button(label, () => {
    if (editor.value !== transcript.pages[page - 1]) return toast("수정한 원문을 먼저 저장하세요.");
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
