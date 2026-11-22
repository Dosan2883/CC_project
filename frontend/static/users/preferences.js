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

async function ensureAiConsent() {
  const session = token;
  if (!session) return false;
  try {
    let consent = await api("/api/users/me/ai-consent");
    if (token !== session) return false;
    if (!consent.allowed) {
      if (!confirm(t("translateConsent"))) return false;
      consent = await api("/api/users/me/ai-consent", "PATCH", {allowed:true});
    }
    if (token !== session) return false;
    $("prefAiConsent").checked = consent.allowed;
    return consent.allowed === true;
  } catch {
    if (token === session) toast(t("failed"));
    return false;
  }
}

$("preferencesPanel").ontoggle = async () => {
  if (!$("preferencesPanel").open || !token) return;
  const control = $("prefAiConsent"), session = token;
  control.disabled = true;
  try {
    const consent = await api("/api/users/me/ai-consent");
    if (token === session) control.checked = consent.allowed;
  } catch {
    if (token === session) toast(t("failed"));
  } finally {
    control.disabled = false;
  }
};

$("prefAiConsent").onchange = async event => {
  const control = event.target, allowed = control.checked, session = token;
  if (!session || (allowed && !confirm(t("translateConsent")))) {
    control.checked = !allowed;
    return;
  }
  control.disabled = true;
  try {
    const consent = await api("/api/users/me/ai-consent", "PATCH", {allowed});
    if (token === session) control.checked = consent.allowed;
  } catch {
    if (token === session) {
      control.checked = !allowed;
      toast(t("failed"));
    }
  } finally {
    control.disabled = false;
  }
};
