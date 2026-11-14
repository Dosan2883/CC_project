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
const fmtClock = seconds => { const whole = Math.max(0, Math.floor(seconds)); return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`; };

/* 음성: 재생기 + 구간 목록. 재생 중인 구간에 불이 들어오고, 각 구간 아래에 번역 대상 언어의 번역이 항상 보인다. */
function audioView(mediaId, transcript) {
  const box = el("div", "audio-view");
  const player = document.createElement("audio");
  player.controls = true; player.preload = "metadata"; player.className = "audio-player";
  const status = el("small", "audio-status"), retranslate = el("button", "audio-retry", t("retranslate")); retranslate.type = "button";
  const summarizeAll = el("button", "audio-retry", t("summarizeAll")); summarizeAll.type = "button";
  summarizeAll.onclick = () => runAi([{kind: "transcript", mediaId}], "summarize");  // 구간별이 아니라 음성 전체를 한 번에 요약한다
  const actions = el("div", "audio-actions"); actions.append(summarizeAll, retranslate);
  const bar = el("div", "audio-bar"); bar.append(status, actions);
  const list = el("div", "seg-list");
  const segments = transcript.segments, rows = [], translations = [];
  segments.forEach(segment => {
    const row = el("div", "seg-row"), head = el("div", "seg-head");
    head.append(el("span", "seg-time", `${fmtClock(segment.start)}–${fmtClock(segment.end)}`));
    const translated = el("div", "seg-translated"); translations.push(translated);
    row.append(head, el("div", "seg-text", segment.text), translated);  // textContent로 XSS 방지
    row.onclick = () => { player.currentTime = segment.start; player.play().catch(() => {}); };  // 구간을 누르면 그 시점부터 재생
    rows.push(row); list.append(row);
  });
  let active = -1;
  const highlight = index => {
    if (index === active) return;
    if (active >= 0) rows[active].classList.remove("playing");
    active = index;
    if (index >= 0) { rows[index].classList.add("playing"); rows[index].scrollIntoView({block: "nearest", behavior: "smooth"}); }
  };
  player.ontimeupdate = () => { const now = player.currentTime; highlight(segments.findIndex(segment => now >= segment.start && now < segment.end)); };
  player.onended = () => highlight(-1);
  api(`/api/media/${mediaId}/play-url`).then(result => { player.src = result.url; }).catch(() => { status.textContent = t("failed"); });

  async function translateAll() {
    if (!hasTranslateConsent()) {
      if (!confirm(t("translateConsent"))) { status.textContent = t("segmentsNotTranslated"); return; }
      rememberTranslateConsent();
    }
    const target = targetLang, generation = viewGeneration, authToken = token;
    status.textContent = t("translating"); retranslate.disabled = true;
    try {
      const result = await api("/api/ai/translate-segments", "POST", {mediaId, target});
      if (viewGeneration !== generation || token !== authToken) return;  // 다른 방·계정으로 바뀌었으면 버린다
      result.segments.forEach((segment, index) => { translations[index].textContent = segment.translated || ""; });
      status.textContent = `${t("translated")} · ${LANG_NAMES[target]}`;
    } catch (error) {
      status.textContent = t(translateErrors[error.status] || "translateFail");
    } finally { retranslate.disabled = false; }
  }
  retranslate.onclick = translateAll;
  translateAll();
  box.append(player, bar, list);
  return {box, player};
}

async function openDocument(mediaId, filename, ext) {
  try {
    const r = await api(`/api/media/${mediaId}/transcript`);
    const text = el("div", "file-text");
    (r.pages || []).forEach((page, i) => {
      if (r.pages.length > 1) text.append(el("h4", null, `${t("pageN")} ${i + 1}`));
      const pre = el("pre", null, page || t("emptyText"));
      // 번역 버튼은 본문 위에 둔다. 긴 페이지에서도 스크롤 없이 바로 보이고, 번역은 버튼 아래(원문 위)에 붙는다.
      if (page && page.trim()) text.append(pageTranslateButton(mediaId, i + 1, pre));
      text.append(pre);
      pageReview(mediaId, i + 1, pre, r);
    });
    let audio = null;
    if (AUDIO_EXTENSIONS.includes((ext || "").toLowerCase()) && (r.segments || []).length) {
      const view = audioView(mediaId, r);  // 음성: 재생기와 구간별 번역을 함께 보여 준다
      text.append(view.box);
      audio = view.player;
    } else {
      (r.segments || []).forEach(segment => {
        const label = el("label", null, `${segment.start}–${segment.end}초 · ${segment.text}`);
        const source = {kind:"segment", mediaId, start:segment.start, end:segment.end};
        const select = document.createElement("input"); select.type = "checkbox";
        select.onchange = () => select.checked ? aiSelections.set(`${mediaId}:${segment.start}`, source) : aiSelections.delete(`${mediaId}:${segment.start}`);
        const translate = el("button", null, t("translate")); translate.type = "button"; translate.onclick = () => runAi([source], "translate");
        const summary = el("button", null, t("summarize")); summary.type = "button"; summary.onclick = () => runAi([source]);
        label.prepend(select); text.append(label, translate, summary);
      });
    }
    if (!(r.pages || []).length && !(r.segments || []).length) text.append(el("pre", null, t("emptyText")));  // 말이 없는 음성 등
    openDocPanel(filename, text, mediaId, audio);
  } catch (e) { toast(t("failed")); }
}

function fileCard(m) {
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
  box.append(top, bar, msg, act);

  let pollFailures = 0;
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
  function toggleText() { openDocument(m.mediaId, m.filename, m.ext); }
  function update(s) {
    clearTimeout(filePollers.get(m.mediaId)); filePollers.delete(m.mediaId);
    if (["PENDING", "PROCESSING", "UPLOADING", "UPLOADED"].includes(s.status)) {
      schedulePoll(2000);
    }
    st.textContent = t("st" + s.status); st.dataset.s = s.status;
    const busy = s.status === "PENDING" || s.status === "PROCESSING";
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
    } else if (s.status === "COMPLETED") {
      refreshFiles();
      if (s.needsOcr) msg.textContent = t("needsOcrMsg");
      const b = el("button", null, t("viewText")); b.type = "button"; b.onclick = () => toggleText(); act.append(b);
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
