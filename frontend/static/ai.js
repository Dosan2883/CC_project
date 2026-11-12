/* Selected server-side sources only; no browser-supplied source text. */
const aiSelections = new Map();
async function runAi(sources, kind = "summarize") {
  if (!currentRoom || !sources.length) return toast("메시지 또는 문단을 먼저 선택하세요.");
  if (!hasTranslateConsent()) {
    if (!confirm(t("translateConsent"))) return;
    rememberTranslateConsent();
  }
  const roomId = currentRoom.roomId, generation = viewGeneration, authToken = token;
  const dialog = document.createElement("dialog"), state = document.createElement("p"), output = document.createElement("pre");
  output.style.whiteSpace = "pre-wrap";
  const close = document.createElement("button"); close.textContent = "닫기"; close.onclick = () => { dialog.close(); dialog.remove(); };
  dialog.append(state, output, close); document.body.append(dialog); dialog.showModal();
  let jobId;
  const active = () => dialog.open && currentRoom?.roomId === roomId && generation === viewGeneration && token === authToken;
  async function poll() {
    for (let i = 0; i < 180 && active(); i++) {
      const job = await api(`/api/ai/jobs/${jobId}`);
      if (!active()) return;
      state.textContent = `${job.status} · 시도 ${job.attempts}/3`;
      if (job.status === "COMPLETED") {
        output.textContent = job.result.text + "\n\n선택한 원문 근거:\n" + job.result.evidence.map(e =>
          `[${e.id}] ${e.source.kind === "message" ? `메시지 #${e.source.messageSeq} · ${new Date(e.sentAt).toLocaleString()}` : e.source.kind === "page" ? `파일 ${e.source.mediaId}, ${e.source.page}쪽` : `녹취 ${e.source.start}–${e.source.end}초`}\n${e.text}`).join("\n\n");
        return;
      }
      if (job.status === "FAILED") {
        state.textContent += ` · ${job.errorCode}`;
        const retryButton = document.createElement("button"); retryButton.textContent = t("retry");
        retryButton.onclick = async () => { retryButton.remove(); try { jobId = (await api(`/api/ai/jobs/${jobId}/retry`, "POST")).jobId; await poll(); } catch (e) { state.textContent = e.code || t("failed"); } };
        dialog.append(retryButton); return;
      }
      await new Promise(resolve => setTimeout(resolve, 1000));
    }
    if (active()) state.textContent = "대기 시간이 지났습니다. 작업 번호: " + jobId;
  }
  try {
    state.textContent = "선택 자료 처리 중…";
    jobId = (await api("/api/ai/jobs", "POST", {roomId, kind, sourceLanguage: $("srcLang").value || "auto", target: targetLang, sources, consent: true})).jobId;
    await poll();
  } catch (e) { if (active()) state.textContent = e.code || t("failed"); }
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
