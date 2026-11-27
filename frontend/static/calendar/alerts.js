/* 마감 알림: 내 캘린더의 일정이 7일·3일·1일 남으면 종 아이콘으로 알린다.
   화면을 열어 둔 동안에만 동작하고, 어디까지 알렸고 읽었는지는 이 브라우저에 계정별로 저장한다.
   그동안 접속하지 않았으면 지나간 단계를 몰아서 보내지 않고 지금 해당하는 단계 하나만 보여 준다. */
const ALERT_STAGES = [1, 3, 7];  // 남은 날 기준, 가까운 단계부터
const ALERT_REFRESH_MS = 5 * 60 * 1000;
let alertList = [], alertTimer = null;
let alertVersion = 0;  // 목록이 새로 정해질 때마다 올라간다. 늦게 도착한 예전 응답을 가려내는 데 쓴다

function daysLeft(date, today) {  // 시각과 상관없이 달력 날짜로 센다
  const [year, month, day] = date.split("-").map(Number);
  return Math.round((Date.UTC(year, month - 1, day) - Date.UTC(today.getFullYear(), today.getMonth(), today.getDate())) / 86400000);
}

/* 일정마다 지금 해당하는 단계의 알림 하나. key에 날짜와 단계가 들어 있어서 단계가 바뀌거나 날짜를 옮기면 새 알림이 된다. */
function deadlineAlerts(events, today = new Date()) {
  return events.flatMap(event => {
    const left = daysLeft(event.date, today), stage = ALERT_STAGES.find(days => left <= days);
    return left >= 0 && stage ? [{event, left, key: `${event.id}:${event.date}:${stage}`}] : [];
  }).sort((a, b) => a.left - b.left || (a.event.time || "").localeCompare(b.event.time || ""));
}

function loadAlertState() {
  try {
    const saved = JSON.parse(localStorage.getItem(`alerts:${me}`));
    return {read: new Set(saved?.read), told: new Set(saved?.told)};
  } catch { return {read: new Set(), told: new Set()}; }
}

function saveAlertState(state) {  // 지금 목록에 없는(지나갔거나 지운) 알림의 기록은 버린다
  const keys = alertList.map(alert => alert.key);
  try {
    localStorage.setItem(`alerts:${me}`, JSON.stringify({read: keys.filter(key => state.read.has(key)), told: keys.filter(key => state.told.has(key))}));
  } catch { /* 저장소를 못 쓰면 새로고침 때 다시 알린다 */ }
}

const alertWhen = alert => alert.left === 0 ? t("alertToday") : t("alertLeft").replace("{n}", alert.left);

function updateAlerts(events) {  // 일정 목록을 새로 읽을 때마다 부른다
  if (!token || !Array.isArray(events)) return;
  alertVersion++;
  alertList = deadlineAlerts(events);
  const state = loadAlertState(), fresh = alertList.filter(alert => !state.told.has(alert.key) && !state.read.has(alert.key));
  for (const alert of fresh) state.told.add(alert.key);
  saveAlertState(state);
  if (fresh.length) toast(fresh.length === 1 ? `${t("alerts")} · ${fresh[0].event.title} · ${alertWhen(fresh[0])}` : t("alertToastMany").replace("{n}", fresh.length));
  renderAlerts();
  if (!$("alertPop").hidden) markAlertsRead();  // 목록을 보고 있으면 새로 온 것도 읽은 것으로 한다(굵은 표시는 남는다)
}

async function refreshAlerts() {
  const session = token;
  if (!session) return;
  const version = ++alertVersion;
  try {
    const events = await api("/api/calendar/events");
    // 기다리는 동안 더 새로운 요청을 보냈거나 캘린더가 목록을 갱신했으면, 이 응답은 낡은 것이라 버린다
    if (token === session && version === alertVersion) updateAlerts(events);
  } catch { /* 알림 갱신 실패는 다음 주기에 다시 시도한다 */ }
}

function showAlertCount(count) {
  const badge = $("bellCount");
  badge.textContent = count > 9 ? "9+" : String(count);
  badge.hidden = !count;
  $("sideBell").textContent = count ? `${t("alerts")} ${count}` : t("alerts");
  $("railBell").title = $("railBell").ariaLabel = count ? `${t("alerts")} ${count}` : t("alerts");
}

function renderAlerts() {
  const state = loadAlertState(), list = $("alertItems");
  showAlertCount(alertList.filter(alert => !state.read.has(alert.key)).length);
  if (!alertList.length) { list.replaceChildren(calNode("div", "cal-empty", t("alertsEmpty"))); return; }
  list.replaceChildren(...alertList.map(alert => {
    const item = calButton("", state.read.has(alert.key) ? "alert-item" : "alert-item unread", () => openAlertDate(alert.event.date));
    const date = new Intl.DateTimeFormat(locale, {month: "numeric", day: "numeric", weekday: "short"}).format(calParse(alert.event.date));
    item.style.borderLeft = `4px solid ${calRoomColor(alert.event.roomId)}`;
    item.append(calNode("strong", null, alert.event.title),
      calNode("span", "alert-when", `${alertWhen(alert)} · ${date}${alert.event.time ? " " + alert.event.time : ""}`),
      calNode("small", null, alert.event.roomName || t("calNoRoom")));
    return item;
  }));
}

function markAlertsRead() {
  const state = loadAlertState();
  for (const alert of alertList) state.read.add(alert.key);
  saveAlertState(state);
  showAlertCount(0);
}

function openAlertDate(date) {  // 알림을 누르면 캘린더에서 그 날짜를 보여 준다
  $("alertPop").hidden = true;
  if (!calendarWin?.open) openCalendar();
  const day = calParse(date);
  Object.assign(calendarState, {selected: date, year: day.getFullYear(), month: day.getMonth()});
  syncCalendarFormDate(calendarState);
  renderCalendar(calendarState);
}

function startAlerts() {
  stopAlerts();
  refreshAlerts();
  alertTimer = setInterval(refreshAlerts, ALERT_REFRESH_MS);
}

function stopAlerts() {
  clearInterval(alertTimer);
  alertVersion++;  // 로그아웃 전에 보낸 요청의 응답은 쓰지 않는다
  alertList = [];
  $("alertPop").hidden = true;
  $("alertItems").replaceChildren();
  showAlertCount(0);
}
