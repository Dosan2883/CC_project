/* 서버 저장값을 복원하고, 언어 변경을 HTTP API로 저장한다. */
let preferenceQueue = Promise.resolve();

function applyPreferences(saved) {
  locale = saved.uiLocale;
  targetLang = saved.defaultTargetLanguage;
  $("uiLocale").value = $("prefUiLocale").value = locale;
  $("srcLang").value = $("prefSource").value = saved.sourceLanguage;
  $("tgtLang").value = $("prefTarget").value = targetLang;
  $("prefSubtitle").value = saved.preferredSubtitleLanguage;
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
$("prefSubtitle").onchange = e => savePreferences({preferredSubtitleLanguage: e.target.value});
