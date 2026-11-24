const languageOptionsReady = fetch("/api/languages").then(r => { if (!r.ok) throw new Error("languages unavailable"); return r.json(); }).then(list => {
  const opts = list.map(c => `<option value="${c}">${LANG_NAMES[c]}</option>`).join("");
  for (const id of ["tgtLang", "prefTarget"]) $(id).innerHTML = opts;
  $("srcLang").innerHTML = $("prefSource").innerHTML = '<option value="auto">자동 판별</option>' + opts;
  $("prefUiLocale").innerHTML = ["ko", "en", "ja"].map(c => `<option value="${c}">${LANG_NAMES[c]}</option>`).join("");
  const def = ["ko","en","ja"].includes(locale) ? locale : "ko";
  $("srcLang").value = def; $("tgtLang").value = targetLang = def;
});
applyI18n();
// 왼쪽 아이콘 레일: 기존 버튼을 대신 눌러 준다.
const appBox = $("app"), sideHidden = () => appBox.classList.contains("side-collapsed");
$("railChats").onclick = () => (sideHidden() ? $("sideOpen") : $("sideFold")).click();
$("railCal").onclick = toggleCalendar;
$("sideCal").onclick = toggleCalendar;
$("railPrefs").onclick = () => { if (sideHidden()) $("sideOpen").click(); const box = $("preferencesPanel"); box.open = !box.open; };
$("railLogout").onclick = () => $("logout").click();
// 같은 탭에서 새로고침해도 로그인이 유지되도록, 저장된 토큰이 아직 유효한지 확인한다.
token = tokenStore.get();
if (token) api("/api/auth/me").then(r => startSession(token, r.nickname)).catch(() => { token = null; tokenStore.set(null); });
