/* ---------- 파일 카드 / 업로드 ---------- */
const cards = {};  // mediaId -> 카드를 갱신하는 함수 (WebSocket 이벤트가 호출)
const filePollers = new Map();
function clearFilePolling() { for (const timer of filePollers.values()) clearTimeout(timer); filePollers.clear(); }
const fmtSize = n => n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(1)} MB`;
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };

function fileCard(m) {
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

  let loaded = false;
  async function toggleText(btn) {
    if (!text.hidden) { text.hidden = true; btn.textContent = t("viewText"); return; }
    if (!loaded) {
      try {
        const r = await api(`/api/media/${m.mediaId}/transcript`);
        text.replaceChildren();
        r.pages.forEach((page, i) => {
          if (r.pages.length > 1) text.append(el("h4", null, `${t("pageN")} ${i + 1}`));
          text.append(el("pre", null, page || t("emptyText")));
        });
        loaded = true;
      } catch (e) { toast(t("failed")); return; }
    }
    text.hidden = false; btn.textContent = t("hideText");
  }
  function update(s) {
    clearTimeout(filePollers.get(m.mediaId)); filePollers.delete(m.mediaId);
    if (["PENDING", "PROCESSING", "UPLOADING", "UPLOADED"].includes(s.status)) {
      filePollers.set(m.mediaId, setTimeout(() => {
        if (cards[m.mediaId] === update && currentRoom) api(`/api/media/${m.mediaId}`).then(update).catch(() => {});
      }, 2000));
    }
    st.textContent = t("st" + s.status); st.dataset.s = s.status;
    const busy = s.status === "PENDING" || s.status === "PROCESSING";
    bar.style.display = busy ? "" : "none"; fill.style.width = s.progress + "%";
    msg.className = "file-msg"; msg.textContent = "";
    act.replaceChildren();
    if (s.status === "FAILED") {
      msg.classList.add("err"); msg.textContent = t("fe_" + s.errorCode) || t("failed");
      const b = el("button", null, t("retry")); b.type = "button";
      b.onclick = () => api(`/api/media/${m.mediaId}/process`, "POST").catch(() => toast(t("failed")));
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
  api(`/api/media/${m.mediaId}`).then(update).catch(() => {});
  return box;
}

const uploadErrors = { UNSUPPORTED_TYPE: "eUpType", FILE_TOO_LARGE: "eUpSize", FILE_CONTENT_MISMATCH: "eUpMismatch" };
$("attDocBtn").onclick = () => $("fileInput").click();
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
