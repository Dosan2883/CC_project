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
    const roomId = currentRoom?.roomId, target = targetLang, epoch = itemEpoch(`file:${mediaId}`), generation = viewGeneration, authToken = token;
    button.disabled = true; button.textContent = t("translating");
    try {
      const result = await api("/api/ai/translate-page", "POST", { mediaId, page, target });
      if (currentRoom?.roomId !== roomId || viewGeneration !== generation || token !== authToken || epoch !== itemEpoch(`file:${mediaId}`)) return;  // 이전 방·계정의 응답은 폐기
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
function stopAllAudio() { for (const player of audioPlayers) player.pause(); for (const player of audioPlayers) player.dispose?.(); audioPlayers.clear(); }  // 방을 바꾸거나 나가면 재생을 멈춘다

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
  (r.segments || []).forEach(segment => {  // 대사 한 줄마다 시간·글·번역 버튼만 둔다
    const source = {kind:"segment", mediaId, start:segment.start, end:segment.end};
    const row = el("div", "seg-row");
    const translate = el("button", null, t("translate")); translate.type = "button"; translate.onclick = () => runAi([source], "translate");
    row.append(el("small", "seg-time", `${segment.start}–${segment.end}초`), el("span", "seg-text", segment.text), translate);
    text.append(row);
  });
  if (!(r.pages || []).length && !(r.segments || []).length) text.append(el("pre", null, t("emptyText")));  // 말이 없는 음성 등
}

/* 음성: 파일 카드 바로 아래에 재생바와 지금 재생 중인 대사(원문과 번역)만 보여 준다. 번역은 처음 재생할 때 한 번 받는다. */
function audioInline(mediaId, ext) {
  const box = el("div", "audio-inline"), video = ["mp4", "webm", "mov"].includes(ext);
  const player = document.createElement(video ? "video" : "audio");
  player.controls = true; player.preload = "metadata"; player.className = "audio-player";
  const language = document.createElement("select"); language.title = "자막 언어";
  language.add(new Option("원어 자막", "original")); language.add(new Option("자막 끄기", "off"));
  for (const code of ["ko", "en", "ja", "zh-CN", "es", "fr"]) language.add(new Option(LANG_NAMES[code] || code, code));
  language.value = $("prefSubtitle").value || "original";
  const status = el("small", "audio-status"), now = el("div", "audio-now");
  box.append(language, player, now, status);
  let segments = [], urls = [], generation = 0, loaded = false;
  const active = () => currentRoom?.roomId === room && token === session && viewGeneration === view;
  const room = currentRoom?.roomId, session = token, view = viewGeneration;
  function clearTracks() {
    player.querySelectorAll("track").forEach(track => track.remove());
    urls.forEach(url => URL.revokeObjectURL(url)); urls = [];
  }
  async function load() {
    const request = ++generation; clearTracks(); loaded = false;
    status.textContent = "자막 준비 중…";
    try {
      const transcript = await api(`/api/media/${mediaId}/transcript`);
      if (!active() || generation !== request) return;
      segments = transcript.segments || [];
      const target = language.value;
      if (target === "off") { loaded = true; status.textContent = ""; return; }
      if (target !== "original" && !hasTranslateConsent()) {
        if (!confirm(t("translateConsent"))) { language.value = "original"; return load(); }
        rememberTranslateConsent();
      }
      const response = await fetch(`/api/media/${mediaId}/subtitles?target=${encodeURIComponent(target)}&consent=${target !== "original"}`, {headers:{Authorization:"Bearer " + session}});
      if (!response.ok) throw new Error((await response.json()).detail || "자막 생성 실패");
      const blob = await response.blob();
      if (!active() || generation !== request) return;
      const track = document.createElement("track"), url = URL.createObjectURL(blob); urls.push(url);
      track.kind = "subtitles"; track.label = target === "original" ? "원어" : LANG_NAMES[target];
      track.srclang = target === "original" ? (transcript.language || "und") : target;
      track.src = url; track.default = true;
      track.onload = () => { track.track.mode = video ? "showing" : "hidden"; };
      player.append(track); loaded = true;
      status.textContent = `자막 · ${track.label} · v${transcript.version || 1}`;
    } catch (error) { if (active() && generation === request) status.textContent = error.message || t("failed"); }
  }
  language.onchange = () => { load(); };
  player.onplay = () => {
    for (const other of audioPlayers) if (other !== player) other.pause();
    audioPlayers.add(player);
    if (!loaded) load();
  };
  player.ontimeupdate = () => {
    const segment = segments.find(s => player.currentTime >= s.start && player.currentTime < s.end);
    const captions = [...player.textTracks].flatMap(track => [...(track.activeCues || [])].map(cue => cue.text));
    const lines = language.value === "off" ? [] : [segment?.text, ...(language.value === "original" ? [] : captions)].filter(Boolean);
    now.replaceChildren(...lines.map(line => el("div", null, line)));  // 원문 한 줄, 그 아래 줄에 번역
    now.hidden = !lines.length;
  };
  player.onended = () => { now.textContent = ""; };
  const changed = event => {
    if (!active() || !box.isConnected) { document.removeEventListener("source:updated", changed); clearTracks(); return; }
    if (event.detail.mediaId === mediaId) { loaded = false; now.textContent = ""; load(); }
  };
  player.dispose = () => { generation++; clearTracks(); document.removeEventListener("source:updated", changed); };
  audioPlayers.add(player);
  document.addEventListener("source:updated", changed);
  api(`/api/media/${mediaId}/play-url`).then(r => { if (active()) player.src = r.url; }).catch(() => { status.textContent = t("failed"); });
  return box;
}

function fileCard(m, sender) {
  const cardRoomId = currentRoom?.roomId, cardGeneration = viewGeneration;
  const box = el("div", "file");
  const top = el("div", "file-top");
  const info = el("div", "file-info");
  info.append(el("div", "file-name", m.filename), el("div", "file-sub", fmtSize(m.size)));
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
      if (m.ext === "pdf") {  // PDF: 원본 보기, 전체 번역, 전체 요약. 모두 떠 있는 창으로 열린다
        for (const button of pdfButtons(m)) act.append(button);
      } else if (isAudioExt(m.ext) || ["mp4", "webm", "mov"].includes(m.ext)) {
        if (!audioBuilt) { audioHost.replaceChildren(audioInline(m.mediaId, m.ext)); audioBuilt = true; }
        audioHost.hidden = false;
        const all = el("button", null, t("summarizeAll")); all.type = "button";
        all.onclick = () => runAi([{kind: "transcript", mediaId: m.mediaId}], "summarize");  // 구간별이 아니라 음성 전체를 한 번에 요약한다
        act.append(all);
        const transcript = el("button", null, text.hidden ? t("viewText") : t("hideText")); transcript.type = "button"; transcript.onclick = () => toggleText(transcript); act.append(transcript);
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
