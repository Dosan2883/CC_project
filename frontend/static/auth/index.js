/* ---------- 회원가입 / 로그인 ---------- */
const tokenStore = {  // 탭마다 따로 저장해서, 한 브라우저에서 탭별로 다른 계정을 쓸 수 있다
  get() { try { return sessionStorage.getItem("token"); } catch { return null; } },
  set(v) { try { v ? sessionStorage.setItem("token", v) : sessionStorage.removeItem("token"); } catch {} },
};
let authMode = "login";
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
$("uiLocale").onchange = e => { locale = e.target.value; applyI18n(); if (token) savePreferences({uiLocale: locale}); };

$("authForm").onsubmit = async e => {
  e.preventDefault();
  const nickname = $("user").value.trim(), password = $("authPw").value;
  const fail = key => { $("authErr").textContent = t(key); };
  if (authMode === "signup") {
    if (password.length < 6) return fail("errPwShort");
    if (password !== $("authPw2").value) return fail("errPwMismatch");
  }
  try {
    const r = await api(`/api/auth/${authMode}`, "POST", { nickname, password });
    $("authPw").value = $("authPw2").value = "";
    await startSession(r.token, r.nickname);
  } catch (ex) {
    if (ex.status === 400) fail(ex.code === "INVALID_PASSWORD" ? "errPwShort" : "errNickInvalid");
    else fail({ 409: "errNickTaken", 429: "errTooMany", 401: "errBadLogin" }[ex.status] || "failed");
  }
};
async function startSession(tok, nickname) {
  token = tok; me = nickname; tokenStore.set(tok);
  await languageOptionsReady;
  const saved = await api("/api/users/me/preferences");
  if (token !== tok) return;
  applyPreferences(saved);
  $("login").style.display = "none"; $("app").style.display = "grid";
  $("who").textContent = me;
  clearInterval(roomsTimer); loadRooms(); roomsTimer = setInterval(loadRooms, 3000);
}
function logout(expired) {
  clearFilePolling();
  closeSocket(); currentRoom = null; token = null; me = null; tokenStore.set(null);
  clearInterval(roomsTimer); lastRooms = []; lastPublic = [];
  $("chat").style.display = "none"; $("empty").style.display = "grid"; $("app").classList.remove("in-room");
  $("app").style.display = "none"; $("login").style.display = "grid";
  $("authErr").textContent = expired ? t("sessionExpired") : "";
}
$("logout").onclick = () => logout(false);
