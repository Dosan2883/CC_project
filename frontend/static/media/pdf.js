/* PDF 파일 카드의 기능 3가지: 원본 보기, 전체 번역, 전체 요약.
   눌러서 열리는 창은 모두 떠 있는 창(floatwin.js)이라 위치와 크기를 마음대로 바꿀 수 있다. */
const PDF_SUMMARY_PAGES = 20;     // 서버가 한 번에 받는 자료 수의 상한
const PDF_SUMMARY_CHARS = 3500;   // 서버가 한 번에 요약하는 글자 수의 상한

function pdfButtons(m) {
  const make = (label, handler) => { const button = el("button", null, label); button.type = "button"; button.onclick = handler; return button; };
  return [
    make(t("pdfOriginal"), () => openPdfOriginal(m)),
    make(t("pdfTranslateAll"), () => openPdfTranslateAll(m)),
    make(t("pdfSummarizeAll"), () => pdfSummarizeAll(m)),
  ];
}

async function pdfPages(mediaId) {
  const result = await api(`/api/media/${mediaId}/transcript`);
  return result.pages || [];
}

/* 쪽 이동 막대: ← [번호] / 전체 → */
function pageNav(total, onChange) {
  let page = 1;
  const node = el("div", "pdf-nav"), input = document.createElement("input");
  const button = label => { const b = el("button", "ghost", label); b.type = "button"; return b; };
  const prev = button("←"), next = button("→");
  input.type = "number"; input.min = "1"; input.max = String(total); input.value = "1";
  const go = value => {
    page = Math.min(total, Math.max(1, Math.floor(Number(value) || 1)));
    input.value = String(page);
    onChange(page);
  };
  prev.onclick = () => go(page - 1);
  next.onclick = () => go(page + 1);
  input.onchange = () => go(input.value);
  node.append(prev, input, el("span", null, `/ ${total}${t("pdfPageUnit")}`), next);
  return {node, go, page: () => page};
}

function ensureConsent() {
  if (hasTranslateConsent()) return true;
  if (!confirm(t("translateConsent"))) return false;
  rememberTranslateConsent();
  return true;
}

/* 원본 보기: 서버가 그린 페이지 이미지를 쪽마다 보여 준다. 이미지에는 로그인 토큰을 실을 수 없어 받아서 표시한다. */
async function openPdfOriginal(m) {
  let total;
  try { total = (await pdfPages(m.mediaId)).length; } catch { return toast(t("failed")); }
  if (!total) return toast(t("emptyText"));
  const win = openFloatWindow(`${m.filename} · ${t("pdfOriginal")}`, {width: 520, height: 680});
  const image = document.createElement("img"), status = el("small", "pdf-status");
  image.className = "pdf-image"; image.alt = m.filename;
  let url = null, sequence = 0;
  const authToken = token;
  const show = async page => {
    const mine = ++sequence;
    status.textContent = "…"; setBusy(status, true);
    try {
      const response = await fetch(`/api/media/${m.mediaId}/pages/${page}/image`, {headers: {Authorization: "Bearer " + authToken}});
      if (!response.ok) throw new Error();
      const blob = await response.blob();
      if (mine !== sequence || !win.open) return;
      if (url) URL.revokeObjectURL(url);
      image.src = url = URL.createObjectURL(blob);
      status.textContent = ""; setBusy(status, false);
    } catch { if (mine === sequence) { status.textContent = t("pdfImageFail"); setBusy(status, false); } }
  };
  const nav = pageNav(total, show);
  win.body.append(nav.node, status, image);
  win.onClose = () => { if (url) URL.revokeObjectURL(url); };
  show(1);
}

/* 전체 번역: 쪽을 차례로 번역해 오는 대로 보여 준다. 중간에 닫거나 한도를 넘으면 멈춘다. */
async function openPdfTranslateAll(m) {
  if (!ensureConsent()) return;
  let pages;
  try { pages = await pdfPages(m.mediaId); } catch { return toast(t("failed")); }
  if (!pages.length) return toast(t("emptyText"));
  const target = targetLang, epoch = itemEpoch(`file:${m.mediaId}`), generation = viewGeneration, authToken = token;
  const win = openFloatWindow(`${m.filename} · ${t("pdfTranslateAll")} (${LANG_NAMES[target]})`, {width: 520, height: 620});
  const status = el("small", "pdf-status"), list = el("div", "pdf-results");
  win.body.append(status, list);
  const alive = () => win.open && viewGeneration === generation && token === authToken && epoch === itemEpoch(`file:${m.mediaId}`);
  setBusy(status, true);
  try {
  for (let index = 0; index < pages.length && alive(); index++) {
    status.textContent = `${t("translating")} ${index + 1} / ${pages.length}`;
    const block = el("div", "pdf-block"); block.append(el("h4", null, `${t("pageN")} ${index + 1}`));
    list.append(block);
    if (!pages[index].trim()) { block.append(el("div", "pdf-note", t("emptyText"))); continue; }
    try {
      const result = await api("/api/ai/translate-page", "POST", {mediaId: m.mediaId, page: index + 1, target});
      if (!alive()) return;
      block.append(el("div", "pdf-translated", result.translated));
    } catch (error) {
      if (!alive()) return;
      block.append(el("div", "pdf-note err", t(error.code === "PAGE_TOO_LONG" ? "translatePageLong" : translateErrors[error.status] || "translateFail")));
      if (error.status !== 422) { status.textContent = t("pdfStopped"); return; }  // 그 쪽만의 문제(너무 긴 쪽 등, 422)가 아니면 서버·한도 문제라 더 시도하지 않는다
    }
  }
  if (alive()) status.textContent = `${t("translated")} · ${LANG_NAMES[target]} · ${pages.length}${t("pdfPageUnit")}`;
  } finally { setBusy(status, false); }
}

/* 전체 요약: 모든 쪽을 하나의 요약으로 만든다. 서버가 한 번에 받는 양(20쪽, 3500자)을 넘으면 미리 알려 준다. */
async function pdfSummarizeAll(m) {
  let pages;
  try { pages = await pdfPages(m.mediaId); } catch { return toast(t("failed")); }
  const filled = pages.map((text, index) => ({text: text.trim(), page: index + 1})).filter(item => item.text);
  if (!filled.length) return toast(t("pdfNoText"));
  if (filled.length > PDF_SUMMARY_PAGES) return toast(t("pdfTooManyPages"));
  if (filled.reduce((sum, item) => sum + item.text.length, 0) > PDF_SUMMARY_CHARS) return toast(t("pdfTooLong"));
  runAi(filled.map(item => ({kind: "page", mediaId: m.mediaId, page: item.page})), "summarize");
}
