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
  $("srcLang").title = t("srcLang");
  $("user").placeholder = t("nickname"); $("authPw").placeholder = t("password"); $("authPw2").placeholder = t("passwordConfirm");
  $("docFold").title = t("panelClose");
  $("railChats").title = t("roomList"); $("railCal").title = t("calendar"); $("railPrefs").title = t("settings"); $("railLogout").title = t("logout");
  $("roomMenuButton").title = $("roomMenuButton").ariaLabel = t("roomMenu");
  if (typeof refreshSelectionBar === "function") refreshSelectionBar();
  setAuthMode(authMode);
  renderRooms();
  renderReadReceipts();
}
