/* ---------- 아직 구현되지 않은 기능의 버튼 ---------- */
let toastTimer;
function toast(msg) {
  const el = $("toast"); el.textContent = msg; el.classList.add("show");
  clearTimeout(toastTimer); toastTimer = setTimeout(() => el.classList.remove("show"), 1800);
}
$("attach").onclick = e => { e.stopPropagation(); $("attachMenu").classList.toggle("open"); };
document.addEventListener("click", e => {
  $("attachMenu").classList.remove("open");
  const b = e.target.closest("button.soon");
  if (b) toast(`${b.textContent} — ${t("soon")}`);
});
