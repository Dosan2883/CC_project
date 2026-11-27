/* ---------- 회원가입 / 로그인 ---------- */
const tokenStore = {  // 탭마다 따로 저장해서, 한 브라우저에서 탭별로 다른 계정을 쓸 수 있다
  get() { try { return sessionStorage.getItem("token"); } catch { return null; } },
  set(v) { try { v ? sessionStorage.setItem("token", v) : sessionStorage.removeItem("token"); } catch {} },
};
let authMode = "login";
let authGeneration = 0;
let pickedLocale = null;  // 로그인 화면에서 직접 고른 표시 언어(고르지 않았으면 계정에 저장된 언어를 쓴다)
function setAuthMode(mode) {
  authMode = mode;
  $("tabLogin").classList.toggle("on", mode === "login"); $("tabSignup").classList.toggle("on", mode === "signup");
  $("pw2Wrap").style.display = $("nickHint").style.display = mode === "signup" ? "" : "none";
  $("authPw").autocomplete = mode === "signup" ? "new-password" : "current-password";
  $("authSubmit").textContent = t(mode === "signup" ? "doSignup" : "doLogin");
  $("authErr").textContent = "";
}
$("tabLogin").onclick = () => setAuthMode("login");
$("tabSignup").onclick = () => setAuthMode("signup");
$("uiLocale").value = locale;
$("uiLocale").onchange = e => { locale = e.target.value; applyI18n(); if (token) savePreferences({uiLocale: locale}); else pickedLocale = locale; };

$("authForm").onsubmit = async e => {
  e.preventDefault();
  const submit = $("authSubmit");
  if (submit.disabled) return;
  const nickname = $("user").value.trim(), password = $("authPw").value;
  const fail = key => { $("authErr").textContent = t(key); };
  if (authMode === "signup") {
    if (password.length < 6) return fail("errPwShort");
    if (password !== $("authPw2").value) return fail("errPwMismatch");
  }
  const generation = ++authGeneration;
  submit.disabled = true;
  try {
    const r = await api(`/api/auth/${authMode}`, "POST", { nickname, password, ...(authMode === "signup" ? {uiLocale:locale} : {}) });
    $("authPw").value = $("authPw2").value = "";
    if (generation === authGeneration) await startSession(r.token, r.nickname, generation);
  } catch (ex) {
    if (generation !== authGeneration) return;
    if (ex.status === 400) fail(ex.code === "INVALID_PASSWORD" ? "errPwShort" : "errNickInvalid");
    else fail({ 409: "errNickTaken", 429: "errTooMany", 401: "errBadLogin" }[ex.status] || "failed");
  } finally { submit.disabled = false; }
};
async function startSession(tok, nickname, generation = authGeneration) {
  if (generation !== authGeneration) return;
  cancelRoomLoads(); cancelApiRequests();
  let [saved] = await Promise.all([
    api("/api/users/me/preferences", "GET", undefined, {authToken:tok}),
    loadLanguageOptions(),
  ]);
  if (generation !== authGeneration) return;
  if (pickedLocale && pickedLocale !== saved.uiLocale) {  // 로그인 화면에서 고른 언어가 우선이고, 계정에도 저장한다(저장에 실패해도 이번 화면에는 적용)
    const picked = pickedLocale;
    saved = await api("/api/users/me/preferences", "PATCH", {uiLocale:picked}, {authToken:tok}).catch(() => ({...saved, uiLocale:picked}));
    if (generation !== authGeneration) return;
  }
  clearImagePreviews();
  lastRooms = []; lastPublic = [];
  token = tok; me = nickname;
  try {
    applyPreferences(saved);
  } catch (error) {
    logout(false);
    throw error;
  }
  tokenStore.set(tok);
  pickedLocale = null;
  $("login").style.display = "none"; $("app").style.display = "grid";
  $("who").textContent = me;
  $("railMe").textContent = initial(me); $("railMe").style.background = color(me);
  clearInterval(roomsTimer); loadRooms(); roomsTimer = setInterval(loadRooms, 3000);
  startAlerts();
}
function logout(expired) {
  authGeneration++;
  clearOutbox(me);
  resetAiPanel();
  cancelRoomLoads(); cancelApiRequests(); viewGeneration++;
  clearFilePolling();
  clearImagePreviews();
  closeSocket(); currentRoom = null; token = null; closeDocPanel(); me = null; tokenStore.set(null);
  $("settingsPop").hidden = true;
  stopAlerts();
  $("attachMenu").classList.remove("open");
  $("roomMenu").open = false;
  for (const dialog of document.querySelectorAll("dialog[open]")) dialog.close();
  for (const id of ["authForm", "createForm", "privateForm", "form"]) $(id).reset();
  $("fileInput").value = "";
  $("log").replaceChildren(); messageViews.clear();
  for (const key in cards) delete cards[key];
  resetParticipants();
  clearInterval(roomsTimer); lastRooms = []; lastPublic = [];
  renderRooms(); $("roomsRetry").hidden = true;
  $("chat").style.display = "none"; $("empty").style.display = "grid"; $("app").classList.remove("in-room");
  $("app").style.display = "none"; $("login").style.display = "grid";
  $("authErr").textContent = expired ? t("sessionExpired") : "";
}
$("logout").onclick = () => logout(false);
