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
  setAuthMode(authMode);
  renderRooms();
  renderReadReceipts();
}
