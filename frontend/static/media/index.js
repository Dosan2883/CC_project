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
  const text = el("div", "file-text"); text.hidden = true;
  box.append(top, bar, msg, act, text);

  let loaded = false, pollFailures = 0;
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
      try {
        const r = await api(`/api/media/${m.mediaId}/transcript`);
        text.replaceChildren();
        (r.pages || []).forEach((page, i) => {
          if (r.pages.length > 1) text.append(el("h4", null, `${t("pageN")} ${i + 1}`));
          const pre = el("pre", null, page || t("emptyText"));
          // 번역 버튼은 본문 위에 둔다. 긴 페이지에서도 스크롤 없이 바로 보이고, 번역은 버튼 아래(원문 위)에 붙는다.
          if (page && page.trim()) text.append(pageTranslateButton(m.mediaId, i + 1, pre));
          text.append(pre);
          pageReview(m.mediaId, i + 1, pre, r);
        });
        (r.segments || []).forEach(segment => {
          const label = el("label", null, `${segment.start}–${segment.end}초 · ${segment.text}`);
          const source = {kind:"segment", mediaId:m.mediaId, start:segment.start, end:segment.end};
          const select = document.createElement("input"); select.type = "checkbox";
          select.onchange = () => select.checked ? aiSelections.set(`${m.mediaId}:${segment.start}`, source) : aiSelections.delete(`${m.mediaId}:${segment.start}`);
          const translate = el("button", null, t("translate")); translate.type = "button"; translate.onclick = () => runAi([source], "translate");
          const summary = el("button", null, t("summarize")); summary.type = "button"; summary.onclick = () => runAi([source]);
          label.prepend(select); text.append(label, translate, summary);
        });
        loaded = true;
      } catch (e) { toast(t("failed")); return; }
    }
    text.hidden = false; btn.textContent = t("hideText");
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
    } else if (s.status === "COMPLETED") {
      if (s.needsOcr) msg.textContent = t("needsOcrMsg");
      const b = el("button", null, text.hidden ? t("viewText") : t("hideText")); b.type = "button"; b.onclick = () => toggleText(b); act.append(b);
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
