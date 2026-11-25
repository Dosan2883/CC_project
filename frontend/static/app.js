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
const settingsPop = $("settingsPop"), settingsButtons = "#railPrefs, #sideSettings";
const themeSelect = $("prefTheme"), pageTheme = document.documentElement.dataset;
themeSelect.value = pageTheme.theme ?? "system";
themeSelect.onchange = () => {
  const theme = themeSelect.value;
  if (theme === "system") delete pageTheme.theme;
  else pageTheme.theme = theme;
  try { localStorage.setItem("theme", theme); } catch { /* 현재 페이지에는 계속 적용한다. */ }
};
function toggleSettings(anchor) {
  settingsPop.hidden = !settingsPop.hidden;
  if (settingsPop.hidden) return;
  // 레일의 설정 아이콘 바로 옆에 띄운다(모바일은 CSS대로 화면 아래). 화면 아래로 넘치지 않게 위로 올린다.
  if (anchor.closest("#rail")) {
    const box = anchor.getBoundingClientRect();
    settingsPop.style.setProperty("--settings-left", `${Math.round(box.right + 8)}px`);
    settingsPop.style.setProperty("--settings-top", `${Math.max(8, Math.min(Math.round(box.top), innerHeight - settingsPop.offsetHeight - 8))}px`);
  }
}
$("railPrefs").onclick = () => toggleSettings($("railPrefs"));
$("sideSettings").onclick = () => toggleSettings($("sideSettings"));
document.addEventListener("click", event => { if (!settingsPop.hidden && !settingsPop.contains(event.target) && !event.target.closest(settingsButtons)) settingsPop.hidden = true; });
document.addEventListener("keydown", event => { if (event.key === "Escape") settingsPop.hidden = true; });
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
