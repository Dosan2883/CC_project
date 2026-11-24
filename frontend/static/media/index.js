/* ---------- 파일 카드 / 업로드 ---------- */
const cards = {};  // mediaId -> 카드를 갱신하는 함수 (WebSocket 이벤트가 호출)
const filePollers = new Map();
function clearFilePolling() { for (const timer of filePollers.values()) clearTimeout(timer); filePollers.clear(); }
const IMAGE_EXTENSIONS = ["jpg", "jpeg", "png"];
const isImageExt = ext => IMAGE_EXTENSIONS.includes((ext || "").toLowerCase());
const imagePreviews = new Map();
function clearImagePreviews(mediaId) {
  for (const [dispose, id] of imagePreviews) {
    if (mediaId === undefined || mediaId === id) dispose();
  }
}
const fmtSize = n => n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(1)} MB`;
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };

function imagePreview(m, isCurrent, authToken) {
  const wrap = el("div", "file-img-wrap"), image = el("img", "file-img");
  wrap.hidden = true;
  image.alt = m.filename;
  image.decoding = "async";
  wrap.append(image);
  let attempted = false, disposed = false, url;
  const controller = new AbortController();

  function clearImage() {
    image.onload = image.onerror = null;
    image.removeAttribute("src");
    wrap.replaceChildren();
    if (url) URL.revokeObjectURL(url);
    url = null;
  }

  function dispose() {
    disposed = true;
    controller.abort();
    clearImage();
    wrap.hidden = true;
    imagePreviews.delete(dispose);
  }

  function fail() {
    if (disposed || !isCurrent()) return dispose();
    clearImage();
    wrap.append(el("div", "file-msg err", t("imageLoadFail")));
    wrap.hidden = false;
  }

  async function load() {
    if (attempted || disposed) return;
    attempted = true;
    imagePreviews.set(dispose, m.mediaId);
    try {
      const response = await fetch(`/api/media/${m.mediaId}/original`, {
        headers:{Authorization:"Bearer " + authToken}, cache:"no-store", signal:controller.signal,
      });
      if (!response.ok) throw new Error();
      const blob = await response.blob();
      if (disposed || !isCurrent()) return dispose();
      url = URL.createObjectURL(blob);
      image.onload = () => {
        if (disposed || !isCurrent()) return dispose();
        const log = $("log");
        const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 60;
        wrap.hidden = false;
        if (atBottom) log.scrollTop = log.scrollHeight;
      };
      image.onerror = fail;
      image.src = url;
    } catch {
      fail();
    }
  }

  return {wrap, load};
}

/* 페이지 번역은 서버에 저장된 추출 글자를 사용한다. */
function pageTranslateButton(mediaId, page, pre, active = () => true) {
  const button = el("button", "page-translate", t("translate")); button.type = "button";
  button.onclick = async () => {
    if (!active() || button.disabled) return;
    const shown = pre.parentElement?.querySelector(`.translation[data-page="${page}"]`);
    if (shown) { shown.remove(); button.textContent = t("translate"); return; }  // 다시 누르면 번역을 숨긴다
    const roomId = currentRoom?.roomId, target = targetLang, epoch = itemEpoch(`file:${mediaId}`), generation = viewGeneration, authToken = token;
    const current = () => active() && currentRoom?.roomId === roomId && viewGeneration === generation &&
      token === authToken && epoch === itemEpoch(`file:${mediaId}`);
    button.disabled = true;
    try {
      if (!await ensureAiConsent() || !current()) return;
      button.textContent = t("translating"); setBusy(button, true);
      const result = await api("/api/ai/translate-page", "POST", { mediaId, page, target });
      if (!current()) return;
      const box = el("div", "translation"); box.dataset.page = String(page);
      box.append(el("small", null, `${t("translated")} · ${LANG_NAMES[target]}`), el("div", null, result.translated));  // textContent로 XSS 방지
      button.after(box);
      button.textContent = t("translateHide");
    } catch (error) {
      if (current()) toast(aiErrorText(error.code) || t(error.code === "PAGE_TOO_LONG" ? "translatePageLong" : translateErrors[error.status] || "translateFail"));
    } finally {
      button.disabled = false; setBusy(button, false);
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
async function fillDocument(text, mediaId, options) {
  const r = await api(`/api/media/${mediaId}/transcript`);
  if (options.active()) renderDocument(text, mediaId, r, options);
}

function renderDocument(text, mediaId, r, {image, active}) {
  text.replaceChildren();
  (r.pages || []).forEach((page, i) => {
    if (r.pages.length > 1) text.append(el("h4", null, `${t("pageN")} ${i + 1}`));
    const detail = r.pageDetails?.[i];
    const uncorrectedImage = image && !r.corrections?.[String(i + 1)];
    const ocrError = uncorrectedImage && detail?.error;
    const hasText = page?.trim() && !(uncorrectedImage && (ocrError || r.needsOcr));
    const message = image ? t("fe_" + ocrError) || t("ocrNoText") : t("emptyText");
    const pre = el("pre", null, hasText ? page : message);
    // 번역 버튼은 본문 위에 둔다. 긴 페이지에서도 스크롤 없이 바로 보이고, 번역은 버튼 아래(원문 위)에 붙는다.
    if (hasText) text.append(pageTranslateButton(mediaId, i + 1, pre, () => active() && !text.hidden));
    if (image) {
      const advanced = el("details", "file-ocr-options");
      const onCorrect = updated => renderDocument(text, mediaId, updated, {image, active});
      advanced.append(el("summary", null, t("ocrOptions")), pageReview(mediaId, i + 1, pre, r, {active, onCorrect}));
      text.append(pre, advanced);
    } else {
      text.append(pageReview(mediaId, i + 1, pre, r, {active}), pre);
    }
  });
  (r.segments || []).forEach(segment => {  // 대사 한 줄마다 시간·글·번역 버튼만 둔다
    const source = {kind:"segment", mediaId, start:segment.start, end:segment.end};
    const row = el("div", "seg-row");
    const translate = el("button", null, t("translate")); translate.type = "button"; translate.onclick = () => runAi([source], "translate");
    row.append(el("small", "seg-time", `${segment.start}–${segment.end}초`), el("span", "seg-text", segment.text), translate);
    text.append(row);
  });
  if (!(r.pages || []).length && !(r.segments || []).length) text.append(el("pre", null, t(image ? "ocrNoText" : "emptyText")));
}

/* 음성: 파일 카드 바로 아래에 재생바와 지금 재생 중인 대사(원문과 번역)만 보여 준다. 번역은 처음 재생할 때 한 번 받는다. */
function audioInline(mediaId, ext) {
  const box = el("div", "audio-inline"), video = ["mp4", "webm", "mov"].includes(ext);
  const player = document.createElement(video ? "video" : "audio");
  player.controls = true; player.preload = "metadata"; player.className = "audio-player";
  const language = document.createElement("select"); language.title = "자막 언어";
  language.add(new Option("원어 자막", "original")); language.add(new Option("자막 끄기", "off"));
  for (const code of ["ko", "en", "ja", "zh-CN", "es", "fr"]) language.add(new Option(LANG_NAMES[code] || code, code));
  language.value = targetLang || "original";  // 자막은 번역 언어를 따라간다
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
    status.textContent = "자막 준비 중…"; setBusy(status, true);
    try {
      const transcript = await api(`/api/media/${mediaId}/transcript`);
      if (!active() || generation !== request) return;
      segments = transcript.segments || [];
      const target = language.value;
      if (target === "off") { loaded = true; status.textContent = ""; return; }
      if (target !== "original") {
        const allowed = await ensureAiConsent();
        if (!active() || generation !== request) return;
        if (!allowed) { language.value = "original"; return load(); }
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
    } catch (error) { if (active() && generation === request) status.textContent = aiErrorText(error.message) || error.message || t("failed"); }
    finally { if (generation === request) setBusy(status, false); }
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
  const cardRoomId = currentRoom?.roomId, cardGeneration = viewGeneration, cardToken = token;
  const image = isImageExt(m.ext);
  const isCurrent = () => cards[m.mediaId] === update && currentRoom?.roomId === cardRoomId &&
    viewGeneration === cardGeneration && token === cardToken;
  const preview = image ? imagePreview(m, isCurrent, cardToken) : null;
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
  box.append(top);
  if (preview) box.append(preview.wrap);
  box.append(bar, msg, act, audioHost, text);

  let currentStatus, statusRevision = 0, loaded = false, loadingText = false, textRevision = 0;
  let wantOpenText = false, startingOcr = false, audioBuilt = false, pollFailures = 0;
  const textButton = el("button", null, t("viewText"));
  textButton.type = "button";
  textButton.onclick = toggleText;
  const busyStatus = status => status === "PENDING" || status === "PROCESSING";

  function schedulePoll(delay) {
    clearTimeout(filePollers.get(m.mediaId));
    filePollers.set(m.mediaId, setTimeout(refreshStatus, delay));
  }
  async function refreshStatus() {
    if (!isCurrent() || currentStatus === "DELETED") return;
    const revision = statusRevision;
    const current = () => isCurrent() && revision === statusRevision;
    try {
      const state = await api(`/api/media/${m.mediaId}`);
      if (!current()) return;
      pollFailures = 0;
      update(state);
    } catch (error) {
      if (!current()) return;
      if (error.status === 404 || error.status === 403) return update({status:"DELETED"});
      pollFailures++;
      schedulePoll(Math.min(2000 * 2 ** Math.min(pollFailures, 4), 30000));
    }
  }
  function syncTextButton() {
    textButton.textContent = t(text.hidden ? "viewText" : "hideText");
    textButton.disabled = startingOcr || loadingText;
  }
  function invalidateText() {
    textRevision++;
    text.hidden = true;
    loaded = loadingText = false;
    text.replaceChildren();
  }
  async function showText() {
    if (loadingText) return;
    if (!loaded) {
      const revision = ++textRevision;
      const active = () => isCurrent() && currentStatus === "COMPLETED" && revision === textRevision;
      loadingText = true;
      syncTextButton();
      try {
        await fillDocument(text, m.mediaId, {image, active});
        if (!active()) return;
        loaded = true;
      } catch (error) {
        if (active() && (error.status === 404 || error.status === 403)) update({status:"DELETED"});
        else if (active()) toast(t("failed"));
        return;
      } finally {
        if (active()) { loadingText = false; syncTextButton(); }
      }
    }
    text.hidden = false;
    syncTextButton();
  }
  async function toggleText() {
    if (!isCurrent() || currentStatus === "DELETED" || textButton.disabled) return;
    if (!text.hidden) {
      text.hidden = true;
      syncTextButton();
    } else if (currentStatus === "COMPLETED") {
      await showText();
    } else if (image && ["UPLOADED", "FAILED"].includes(currentStatus)) {
      wantOpenText = startingOcr = true;
      syncTextButton();
      msg.textContent = t("imageExtracting");
      const revision = statusRevision;
      try {
        const state = await api(`/api/media/${m.mediaId}/process`, "POST");
        if (isCurrent() && revision === statusRevision) update(state);
      } catch (error) {
        if (isCurrent() && revision === statusRevision) {
          wantOpenText = false;
          if (error.status === 404 || error.status === 403) update({status:"DELETED"});
          else msg.textContent = t("fe_" + error.code) || t("failed");
        }
      } finally {
        startingOcr = false;
        if (isCurrent()) syncTextButton();
      }
    }
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
    if (!isCurrent() || currentStatus === "DELETED") return;
    statusRevision++;
    const wasBusy = busyStatus(currentStatus);
    currentStatus = s.status;
    clearTimeout(filePollers.get(m.mediaId)); filePollers.delete(m.mediaId);
    const busy = busyStatus(s.status);
    if (busy || (!image && ["UPLOADING", "UPLOADED"].includes(s.status))) {
      schedulePoll(2000);
    }
    if (preview && !["UPLOADING", "DELETED"].includes(s.status)) preview.load();
    st.textContent = t("st" + s.status); st.dataset.s = s.status;
    setBusy(st, busy);
    if (busy && !wasBusy) {
      if (image && (!text.hidden || loadingText)) wantOpenText = true;
      invalidateText();
    }
    if (s.status === "FAILED" || s.status === "DELETED") wantOpenText = false;
    bar.style.display = busy ? "" : "none"; fill.style.width = (s.progress ?? 0) + "%";
    msg.className = "file-msg"; msg.textContent = "";
    act.replaceChildren();
    syncTextButton();
    if (image && busy) {
      msg.textContent = t("imageExtracting");
    } else if (s.status === "FAILED") {
      msg.classList.add("err"); msg.textContent = t("fe_" + s.errorCode) || t("failed");
    }
    if (s.status === "FAILED" && !image) {
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
      clearImagePreviews(m.mediaId);
      msg.textContent = t("fileDeleted");
      invalidateText();
      if (!image) stopAllAudio();
      audioHost.hidden = true; audioHost.replaceChildren();
    } else if (image && ["UPLOADED", "FAILED", "COMPLETED"].includes(s.status)) {
      act.append(textButton);
      if (s.status === "COMPLETED" && wantOpenText) {
        wantOpenText = false;
        showText();
      }
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
        act.append(textButton);
      } else {
        act.append(textButton);
      }
    }
    const canDelete = s.status === "COMPLETED" || (image && !["UPLOADING", "DELETED"].includes(s.status));
    if (canDelete && sender && sender === me) {
      const remove = el("button", "danger", t("fileDelete")); remove.type = "button"; remove.onclick = () => removeFile(remove); act.append(remove);
    }
  }
  cards[m.mediaId] = update;
  if (!image) update({status:"PENDING", progress:0});
  else bar.style.display = "none";
  // 기록에서 다시 불러온 카드도 현재 처리 상태를 보여 준다.
  refreshStatus();
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
  const ext = file.name.split(".").pop();
  try {
    const meta = await api("/api/files/upload-url", "POST", { roomId: currentRoom.roomId, filename: file.name, size: file.size });
    const res = await fetch(meta.uploadUrl, { method: meta.method, headers: meta.headers, body: file });
    if (!res.ok) throw Object.assign(new Error(), { code: (await res.json().catch(() => ({}))).detail });
    await api(meta.completeUrl, "POST");
    if (!isImageExt(ext)) await api(`/api/media/${meta.mediaId}/process`, "POST");
  } catch (err) { toast(t(uploadErrors[err.code] || "eUpFail")); }
};
