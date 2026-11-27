function setLanguageOptions(list) {
  const options = codes => codes.map(code => new Option(LANG_NAMES[code], code));
  for (const id of ["tgtLang", "prefTarget"]) $(id).replaceChildren(...options(list));
  for (const id of ["srcLang", "prefSource"]) $(id).replaceChildren(new Option("자동 판별", "auto"), ...options(list));
  $("prefUiLocale").replaceChildren(...options(["ko", "en", "ja"]));
  const def = ["ko","en","ja"].includes(locale) ? locale : "ko";
  $("srcLang").value = def; $("tgtLang").value = targetLang = def;
}
setLanguageOptions(Object.keys(LANG_NAMES));
let languageOptionsReady;
function loadLanguageOptions() {
  return languageOptionsReady ??= api("/api/languages", "GET", undefined, {timeout:2000, authToken:null})
    .then(list => {
      const supported = Array.isArray(list) ? list.filter(code => Object.hasOwn(LANG_NAMES, code)) : [];
      if (supported.length) setLanguageOptions(supported);
    })
    .catch(() => { languageOptionsReady = undefined; });
}
applyI18n();
// 왼쪽 아이콘 레일: 기존 버튼을 대신 눌러 준다.
$("railChats").onclick = toggleSideList;
$("railCal").onclick = toggleCalendar;
$("sideCal").onclick = toggleCalendar;
// 데스크톱은 레일, 모바일은 닉네임 줄에서 설정을 연다.
const settingsPop = $("settingsPop");
const themeSelect = $("prefTheme"), pageTheme = document.documentElement.dataset;
themeSelect.value = pageTheme.theme ?? "system";
themeSelect.onchange = () => {
  const theme = themeSelect.value;
  if (theme === "system") delete pageTheme.theme;
  else pageTheme.theme = theme;
  try { localStorage.setItem("theme", theme); } catch { /* 현재 페이지에는 계속 적용한다. */ }
};
// 설정과 마감 알림은 같은 방식의 선택 상자다. 하나를 열면 다른 하나는 닫고, 바깥을 누르거나 Esc를 누르면 닫는다.
const popovers = [
  {box: settingsPop, buttons: "#railPrefs, #sideSettings"},
  {box: $("alertPop"), buttons: "#railBell, #sideBell"},
];
function togglePopover(box, anchor) {
  for (const other of popovers) if (other.box !== box) other.box.hidden = true;
  box.hidden = !box.hidden;
  if (box.hidden) return;
  // 레일의 아이콘 바로 옆에 띄운다(모바일은 CSS대로 화면 아래). 화면 아래로 넘치지 않게 위로 올린다.
  if (anchor.closest("#rail")) {
    const rect = anchor.getBoundingClientRect();
    box.style.setProperty("--pop-left", `${Math.round(rect.right + 8)}px`);
    box.style.setProperty("--pop-top", `${Math.max(8, Math.min(Math.round(rect.top), innerHeight - box.offsetHeight - 8))}px`);
  }
}
for (const id of ["railPrefs", "sideSettings"]) $(id).onclick = () => togglePopover(settingsPop, $(id));
for (const id of ["railBell", "sideBell"]) $(id).onclick = () => {
  renderAlerts();
  togglePopover($("alertPop"), $(id));
  if (!$("alertPop").hidden) markAlertsRead();
};
document.addEventListener("click", event => { for (const {box, buttons} of popovers) if (!box.hidden && !box.contains(event.target) && !event.target.closest(buttons)) box.hidden = true; });
document.addEventListener("keydown", event => { if (event.key === "Escape") for (const {box} of popovers) box.hidden = true; });
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") refreshAlerts(); });  // 다른 탭에 있다가 돌아오면 바로 다시 확인
$("railLogout").onclick = () => { settingsPop.hidden = true; $("logout").click(); };
// 같은 탭에서 새로고침해도 로그인이 유지되도록, 저장된 토큰이 아직 유효한지 확인한다.
purgeLegacyOutboxes();
const savedToken = tokenStore.get(), resumeGeneration = authGeneration;
if (savedToken) {
  const resumeStillCurrent = () => authGeneration === resumeGeneration && !token && tokenStore.get() === savedToken;
  api("/api/auth/me", "GET", undefined, {authToken:savedToken})
    .then(r => { if (resumeStillCurrent()) return startSession(savedToken, r.nickname, resumeGeneration); })
    .catch(error => {
      if (resumeStillCurrent() && error.status === 401) { clearOutbox(); tokenStore.set(null); }
    });
}
