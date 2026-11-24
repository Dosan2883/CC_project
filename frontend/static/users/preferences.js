/* 서버 저장값을 복원하고, 언어 변경을 HTTP API로 저장한다. */
let preferenceQueue = Promise.resolve();

function applyPreferences(saved) {
  locale = saved.uiLocale;
  targetLang = saved.defaultTargetLanguage;
  $("uiLocale").value = $("prefUiLocale").value = locale;
  $("srcLang").value = $("prefSource").value = saved.sourceLanguage;
  $("tgtLang").value = $("prefTarget").value = targetLang;
  applyI18n();
}

function savePreferences(changes) {
  const session = token;
  preferenceQueue = preferenceQueue.catch(() => {}).then(async () => {
    if (!session || session !== token) return;
    const saved = await api("/api/users/me/preferences", "PATCH", changes);
    if (session === token) applyPreferences(saved);
  }).catch(() => toast(t("failed")));
  return preferenceQueue;
}

$("prefUiLocale").onchange = e => savePreferences({uiLocale: e.target.value});
$("prefSource").onchange = e => savePreferences({sourceLanguage: e.target.value});
$("srcLang").onchange = e => savePreferences({sourceLanguage: e.target.value});
$("prefTarget").onchange = e => savePreferences({defaultTargetLanguage: e.target.value});
$("tgtLang").onchange = e => savePreferences({defaultTargetLanguage: e.target.value});

/* 이 데모는 실제 배포용이 아니라서 별도 동의 화면 없이 자동으로 허용한다. 서버는 계정마다 동의 값을 요구하므로, 처음 AI 기능을 쓸 때 한 번 저장해 둔다. */
async function ensureAiConsent() {
  const session = token;
  if (!session) return false;
  try {
    let consent = await api("/api/users/me/ai-consent");
    if (token !== session) return false;
    if (!consent.allowed) consent = await api("/api/users/me/ai-consent", "PATCH", {allowed:true});
    return token === session && consent.allowed === true;
  } catch {
    if (token === session) toast(t("failed"));
    return false;
  }
}
