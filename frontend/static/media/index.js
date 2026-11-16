/* ---------- 파일 카드 / 업로드 ---------- */
const cards = {};  // mediaId -> 카드를 갱신하는 함수 (WebSocket 이벤트가 호출)
const filePollers = new Map();
function clearFilePolling() { for (const timer of filePollers.values()) clearTimeout(timer); filePollers.clear(); }
const fmtSize = n => n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(1)} MB`;
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };

/* 문서 페이지 번역: 서버가 저장된 추출 글자를 직접 읽어 번역한다. 동의 확인 함수는 chat/index.js의 것을 쓴다. */
function pageTranslateButton(mediaId, page, pre) {
  const button = el("button", "page-translate", t("translate")); button.type = "button";
  button.onclick = async () => {
    const shown = pre.parentElement.querySelector(`.translation[data-page="${page}"]`);
    if (shown) { shown.remove(); button.textContent = t("translate"); return; }  // 다시 누르면 번역을 숨긴다
    if (!hasTranslateConsent()) {
      if (!confirm(t("translateConsent"))) return;
      rememberTranslateConsent();
    }
    const roomId = currentRoom?.roomId, target = targetLang, generation = viewGeneration, authToken = token;
    button.disabled = true; button.textContent = t("translating");
    try {
      const result = await api("/api/ai/translate-page", "POST", { mediaId, page, target });
      if (currentRoom?.roomId !== roomId || viewGeneration !== generation || token !== authToken) return;  // 이전 방·계정의 응답은 폐기
      const box = el("div", "translation"); box.dataset.page = String(page);
      box.append(el("small", null, `${t("translated")} · ${LANG_NAMES[target]}`), el("div", null, result.translated));  // textContent로 XSS 방지
      button.after(box);
      button.textContent = t("translateHide");
    } catch (error) {
      toast(t(error.code === "PAGE_TOO_LONG" ? "translatePageLong" : translateErrors[error.status] || "translateFail"));
    } finally {
      button.disabled = false;
      if (!button.nextElementSibling?.classList?.contains("translation")) button.textContent = t("translate");
    }
  };
  return button;
}

/* 문서 본문을 오른쪽 패널에 연다. 파일 카드의 본문 보기와 문서 목록이 함께 쓴다. 열 때마다 저장된 최신 추출 결과를 읽는다. */
const AUDIO_EXTENSIONS = ["mp3", "m4a", "wav", "ogg", "flac"];
const isAudioExt = ext => AUDIO_EXTENSIONS.includes((ext || "").toLowerCase());
const audioPlayers = new Set();
function stopAllAudio() { for (const player of audioPlayers) player.pause(); audioPlayers.clear(); }  // 방을 바꾸거나 나가면 재생을 멈춘다

/* 문서·이미지·영상 본문을 파일 카드 바로 아래 칸에 채운다. 열 때마다 저장된 최신 추출 결과를 읽는다. */
async function fillDocument(text, mediaId) {
  const r = await api(`/api/media/${mediaId}/transcript`);
  text.replaceChildren();
  (r.pages || []).forEach((page, i) => {
    if (r.pages.length > 1) text.append(el("h4", null, `${t("pageN")} ${i + 1}`));
    const pre = el("pre", null, page || t("emptyText"));
    // 번역 버튼은 본문 위에 둔다. 긴 페이지에서도 스크롤 없이 바로 보이고, 번역은 버튼 아래(원문 위)에 붙는다.
    if (page && page.trim()) text.append(pageTranslateButton(mediaId, i + 1, pre));
    text.append(pre);
    pageReview(mediaId, i + 1, pre, r);
  });
  (r.segments || []).forEach(segment => {
    const label = el("label", null, `${segment.start}–${segment.end}초 · ${segment.text}`);
    const source = {kind:"segment", mediaId, start:segment.start, end:segment.end};
    const select = document.createElement("input"); select.type = "checkbox";
    select.onchange = () => select.checked ? aiSelections.set(`${mediaId}:${segment.start}`, source) : aiSelections.delete(`${mediaId}:${segment.start}`);
    const translate = el("button", null, t("translate")); translate.type = "button"; translate.onclick = () => runAi([source], "translate");
    const summary = el("button", null, t("summarize")); summary.type = "button"; summary.onclick = () => runAi([source]);
    label.prepend(select); text.append(label, translate, summary);
  });
  if (!(r.pages || []).length && !(r.segments || []).length) text.append(el("pre", null, t("emptyText")));  // 말이 없는 음성 등
}

/* 음성: 파일 카드 바로 아래에 재생바와 지금 재생 중인 대사(원문과 번역)만 보여 준다. 번역은 처음 재생할 때 한 번 받는다. */
function audioInline(mediaId) {
  const box = el("div", "audio-inline");
  const player = document.createElement("audio");
  player.controls = true; player.preload = "metadata"; player.className = "audio-player";
  const now = el("div", "audio-now"), original = el("div", "seg-text"), translatedLine = el("div", "seg-translated");
  now.append(original, translatedLine); now.hidden = true;
  const status = el("small", "audio-status");
  box.append(player, now, status);
  let segments = [], translations = [], translatedFor = null, pending = false, active = -1;

  const show = index => {  // 지금 구간의 대사만 보인다. 소리가 없는 사이에는 숨긴다
    active = index;
    now.hidden = index < 0;
    if (index >= 0) { original.textContent = segments[index].text; translatedLine.textContent = translations[index] || ""; }
  };
  async function translateAll() {
    if (pending || !segments.length || translatedFor === targetLang) return;
    if (!hasTranslateConsent()) {
      if (!confirm(t("translateConsent"))) { status.textContent = t("segmentsNotTranslated"); return; }
      rememberTranslateConsent();
    }
    const target = targetLang, generation = viewGeneration, authToken = token;
    pending = true; status.textContent = t("translating");
    try {
      const result = await api("/api/ai/translate-segments", "POST", {mediaId, target});
      if (viewGeneration !== generation || token !== authToken) return;  // 다른 방·계정으로 바뀌었으면 버린다
      translations = result.segments.map(segment => segment.translated || "");
      translatedFor = target;
      status.textContent = `${t("translated")} · ${LANG_NAMES[target]}`;
      if (active >= 0) show(active);
    } catch (error) {
      status.textContent = t(translateErrors[error.status] || "translateFail");
    } finally { pending = false; }
  }
  player.onplay = () => {
    for (const other of audioPlayers) if (other !== player) other.pause();  // 한 번에 하나만 재생한다
    audioPlayers.add(player);
    translateAll();
  };
  player.ontimeupdate = () => {
    const nowTime = player.currentTime, index = segments.findIndex(segment => nowTime >= segment.start && nowTime < segment.end);
    if (index !== active) show(index);
  };
  player.onended = () => show(-1);
  api(`/api/media/${mediaId}/transcript`).then(r => { segments = r.segments || []; }).catch(() => { status.textContent = t("failed"); });
  api(`/api/media/${mediaId}/play-url`).then(r => { player.src = r.url; }).catch(() => { status.textContent = t("failed"); });
  return box;
}

function fileCard(m, sender) {
  const cardRoomId = currentRoom?.roomId, cardGeneration = viewGeneration;
  const box = el("div", "file");
  const top = el("div", "file-top");
  const info = el("div", "file-info");
  info.append(el("div", "file-name", m.filename), el("div", "file-sub", fmtSize(m.size)));  // textContent로 XSS 방지
  const st = el("span", "st");
  top.append(el("div", "file-ic", m.ext.toUpperCase()), info, st);
  const bar = el("div", "bar"), fill = el("i"); bar.append(fill);
  const msg = el("div", "file-msg");
  const act = el("div", "file-act");
  const text = el("div", "file-text"); text.hidden = true;  // 문서 본문은 이 칸(파일 카드 바로 아래)에서 본다
  const audioHost = el("div", "audio-host"); audioHost.hidden = true;  // 음성 재생바는 이 칸에 둔다
  box.append(top, bar, msg, act, audioHost, text);

  let loaded = false, audioBuilt = false, pollFailures = 0;
  const isCurrent = () => cards[m.mediaId] === update && currentRoom?.roomId === cardRoomId && viewGeneration === cardGeneration;
  function schedulePoll(delay) {
    clearTimeout(filePollers.get(m.mediaId));
    filePollers.set(m.mediaId, setTimeout(async () => {
      if (!isCurrent()) { filePollers.delete(m.mediaId); return; }
      try {
        const state = await api(`/api/media/${m.mediaId}`);
        if (!isCurrent()) return;
        pollFailures = 0; update(state);
      } catch (error) {
        if (!isCurrent() || error.status === 404 || error.status === 403) { filePollers.delete(m.mediaId); return; }
        pollFailures++;
        schedulePoll(Math.min(2000 * 2 ** Math.min(pollFailures, 4), 30000));
      }
    }, delay));
  }
  async function toggleText(btn) {
    if (!text.hidden) { text.hidden = true; btn.textContent = t("viewText"); return; }
    if (!loaded) {
      try { await fillDocument(text, m.mediaId); loaded = true; }
      catch (e) { toast(t("failed")); return; }
    }
    text.hidden = false; btn.textContent = t("hideText");
  }
  async function removeFile(btn) {
    if (!confirm(t("fileDeleteConfirm"))) return;
    btn.disabled = true;
    try {
      await api(`/api/files/${m.mediaId}`, "DELETE");
      toast(t("fileDeleted"));
      update({ status: "DELETED", progress: 100, errorCode: null, needsOcr: false });
    } catch { toast(t("failed")); btn.disabled = false; }
  }
  function update(s) {
    clearTimeout(filePollers.get(m.mediaId)); filePollers.delete(m.mediaId);
    if (["PENDING", "PROCESSING", "UPLOADING", "UPLOADED"].includes(s.status)) {
      schedulePoll(2000);
    }
    st.textContent = t("st" + s.status); st.dataset.s = s.status;
    const busy = s.status === "PENDING" || s.status === "PROCESSING";
    if (busy) loaded = false;
    bar.style.display = busy ? "" : "none"; fill.style.width = s.progress + "%";
    msg.className = "file-msg"; msg.textContent = "";
    act.replaceChildren();
    if (s.status === "FAILED") {
      msg.classList.add("err"); msg.textContent = t("fe_" + s.errorCode) || t("failed");
      const b = el("button", null, t("retry")); b.type = "button";
      b.onclick = async () => {
        b.disabled = true;
        try { update(await api(`/api/media/${m.mediaId}/process`, "POST")); }
        catch { toast(t("failed")); }
        finally { b.disabled = false; }
      };
      act.append(b);
    } else if (s.status === "PENDING" && s.errorCode === "RETRYING") {
      msg.textContent = t("fe_RETRYING");
    } else if (s.status === "DELETED") {
      msg.textContent = t("fileDeleted");
      text.hidden = true; audioHost.hidden = true; audioHost.replaceChildren();
      stopAllAudio();
    } else if (s.status === "COMPLETED") {
      if (s.needsOcr) msg.textContent = t("needsOcrMsg");
      if (m.ext === "pdf") {  // PDF: 원본 보기, 전체·페이지 번역과 요약, 본문 보기. 모두 떠 있는 창으로 열린다
        for (const button of pdfButtons(m)) act.append(button);
      } else if (isAudioExt(m.ext)) {
        if (!audioBuilt) { audioHost.replaceChildren(audioInline(m.mediaId)); audioBuilt = true; }
        audioHost.hidden = false;
        const all = el("button", null, t("summarizeAll")); all.type = "button";
        all.onclick = () => runAi([{kind: "transcript", mediaId: m.mediaId}], "summarize");  // 구간별이 아니라 음성 전체를 한 번에 요약한다
        act.append(all);
      } else {
        const b = el("button", null, text.hidden ? t("viewText") : t("hideText")); b.type = "button"; b.onclick = () => toggleText(b); act.append(b);
      }
      if (sender && sender === me) {  // 올린 사람만 지울 수 있다
        const remove = el("button", "danger", t("fileDelete")); remove.type = "button"; remove.onclick = () => removeFile(remove); act.append(remove);
      }
    }
  }
  cards[m.mediaId] = update;
  update({ status: "PENDING", progress: 0, errorCode: null, needsOcr: false });
  // 기록에서 다시 불러온 카드도 현재 처리 상태를 보여 준다.
  api(`/api/media/${m.mediaId}`).then(state => { if (isCurrent()) update(state); }).catch(error => {
    if (isCurrent() && error.status !== 404 && error.status !== 403) { pollFailures++; schedulePoll(Math.min(2000 * 2 ** Math.min(pollFailures, 4), 30000)); }
  });
  return box;
}

const uploadErrors = { UNSUPPORTED_TYPE: "eUpType", FILE_TOO_LARGE: "eUpSize", FILE_CONTENT_MISMATCH: "eUpMismatch" };
$("attDocBtn").onclick = () => $("fileInput").click();
$("attImageBtn").onclick = () => $("fileInput").click();
$("attAudioBtn").onclick = () => $("fileInput").click();
$("attVideoBtn").onclick = () => $("fileInput").click();
$("fileInput").onchange = async e => {
  const file = e.target.files[0]; e.target.value = "";
  if (!file || !currentRoom) return;
  try {
    const meta = await api("/api/files/upload-url", "POST", { roomId: currentRoom.roomId, filename: file.name, size: file.size });
    const res = await fetch(meta.uploadUrl, { method: meta.method, headers: meta.headers, body: file });
    if (!res.ok) throw Object.assign(new Error(), { code: (await res.json().catch(() => ({}))).detail });
    await api(meta.completeUrl, "POST");
    await api(`/api/media/${meta.mediaId}/process`, "POST");
  } catch (err) { toast(t(uploadErrors[err.code] || "eUpFail")); }
};
