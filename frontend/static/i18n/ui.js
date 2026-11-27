const t = k => I18N[locale][k];
function applyI18n() {
  document.documentElement.lang = locale;
  document.title = $("title").textContent = t("title");
  $("loginTitle").textContent = t("loginTitle");
  document.querySelectorAll("[data-i]").forEach(el => el.textContent = t(el.dataset.i));
  $("body").placeholder = t("placeholder");
  $("roomName").placeholder = t("roomName");
  $("roomPw").placeholder = t("pwPlaceholder");
  $("privName").placeholder = t("roomName");
  $("privPw").placeholder = t("pwEnter");
  $("attach").title = t("attach");
  $("send").title = $("send").ariaLabel = t("send");
  $("srcLang").title = t("srcLang");
  $("user").placeholder = t("nickname"); $("authPw").placeholder = t("password"); $("authPw2").placeholder = t("passwordConfirm");
  $("docFold").title = t("panelClose");
  $("docResize").title = $("docResize").ariaLabel = t("panelResize");
  $("railChats").title = t("roomList"); $("railCal").title = t("calendar"); $("railPrefs").title = t("settings"); $("railLogout").title = t("logout");
  $("roomMenuButton").title = $("roomMenuButton").ariaLabel = t("roomMenu");
  if (typeof refreshSelectionBar === "function") refreshSelectionBar();
  if (typeof renderAlerts === "function") renderAlerts();  // 종 아이콘 설명과 알림 목록도 새 언어로
  setAuthMode(authMode);
  renderRooms();
  renderReadReceipts();
}
