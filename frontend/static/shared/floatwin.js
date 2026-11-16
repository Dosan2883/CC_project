/* 위치를 옮기고 크기를 바꿀 수 있는 떠 있는 창. 제목 줄을 끌어 옮기고, 오른쪽 아래 모서리를 끌어 크기를 바꾼다.
   방을 바꾸거나 나가면 모두 닫힌다(closeAllFloatWindows). 창 안의 글은 textContent로만 넣는다. */
const floatWindows = new Set();
let floatZ = 60;

function openFloatWindow(title, {width = 460, height = 420} = {}) {
  const win = document.createElement("div");
  win.className = "float-win";
  const head = document.createElement("div"), titleEl = document.createElement("strong");
  head.className = "float-head"; titleEl.textContent = title;
  const close = document.createElement("button");
  close.type = "button"; close.className = "ghost float-close"; close.textContent = "×"; close.title = t("floatClose");
  head.append(titleEl, close);
  const body = document.createElement("div");
  body.className = "float-body";
  win.append(head, body);

  const viewWidth = window.innerWidth || 800, viewHeight = window.innerHeight || 600;
  const w = Math.min(width, viewWidth - 16), h = Math.min(height, viewHeight - 16), step = floatWindows.size % 8;
  win.style.width = w + "px"; win.style.height = h + "px";
  win.style.left = Math.max(8, Math.min(viewWidth - w - 8, 90 + step * 28)) + "px";  // 새 창은 조금씩 비껴서 열린다
  win.style.top = Math.max(8, Math.min(viewHeight - h - 8, 70 + step * 28)) + "px";
  win.style.zIndex = String(++floatZ);

  const state = {win, body, title: titleEl, open: true, onClose: null, close() {
    if (!state.open) return;
    state.open = false;
    floatWindows.delete(state);
    win.remove();
    if (state.onClose) state.onClose();
  }};
  close.onclick = () => state.close();
  win.addEventListener("pointerdown", () => { win.style.zIndex = String(++floatZ); });  // 누른 창이 맨 앞으로 온다

  head.addEventListener("pointerdown", event => {
    if (event.target === close) return;
    const rect = win.getBoundingClientRect(), startX = event.clientX, startY = event.clientY;
    head.setPointerCapture?.(event.pointerId);
    const move = moved => {
      const maxLeft = (window.innerWidth || 800) - 80, maxTop = (window.innerHeight || 600) - 40;  // 제목 줄이 화면 밖으로 사라지지 않게 한다
      win.style.left = Math.max(-rect.width + 80, Math.min(maxLeft, rect.left + moved.clientX - startX)) + "px";
      win.style.top = Math.max(0, Math.min(maxTop, rect.top + moved.clientY - startY)) + "px";
    };
    const up = () => { head.removeEventListener("pointermove", move); head.removeEventListener("pointerup", up); head.removeEventListener("pointercancel", up); };
    head.addEventListener("pointermove", move);
    head.addEventListener("pointerup", up);
    head.addEventListener("pointercancel", up);
    event.preventDefault();
  });

  document.body.append(win);
  floatWindows.add(state);
  return state;
}

function closeAllFloatWindows() {
  for (const state of [...floatWindows]) state.close();
}
