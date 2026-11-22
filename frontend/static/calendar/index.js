/* 방 캘린더: 떠 있는 창에 월간 달력과 선택한 날의 일정을 보여 주고, 일정을 추가·수정·삭제한다.
   일정은 방 구성원 모두가 함께 보고, 누가 바꾸면 서버가 calendar:changed를 보내 열려 있는 창이 목록을 다시 읽는다.
   창 안의 글은 모두 textContent로만 넣는다. */
let calendarWin = null, calendarState = null;

const calPad = number => String(number).padStart(2, "0");
const calIso = date => `${date.getFullYear()}-${calPad(date.getMonth() + 1)}-${calPad(date.getDate())}`;
const calParse = iso => { const [year, month, day] = iso.split("-").map(Number); return new Date(year, month - 1, day); };
function calNode(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}
function calButton(label, className, handler) {
  const button = calNode("button", className, label);
  button.type = "button"; button.onclick = handler;
  return button;
}
const calErrorText = error => t("calErr_" + error.code) || aiErrorText(error.code) || t("failed");

function toggleCalendar() {
  if (calendarWin?.open) { calendarWin.close(); return; }
  if (!currentRoom) return;
  openCalendar();
}

function openCalendar() {
  const today = new Date();
  const state = calendarState = {roomId: currentRoom.roomId, year: today.getFullYear(), month: today.getMonth(), selected: calIso(today), events: [], editing: null, busy: false};
  const win = calendarWin = openFloatWindow(t("calendar"), {width: 420, height: 600});
  win.onClose = () => { if (calendarWin === win) { calendarWin = null; calendarState = null; } };
  const nav = calNode("div", "cal-nav"), grid = calNode("div", "cal-grid"), day = calNode("div", "cal-day"), form = calNode("form", "cal-form");
  win.body.append(nav, grid, day, form);
  state.parts = {nav, grid, day, form};
  buildCalendarForm(state);
  renderCalendar(state);
  loadCalendar(state);
}

async function loadCalendar(state) {
  if (!state || state !== calendarState || !calendarWin?.open) return;
  try {
    const events = await api(`/api/rooms/${state.roomId}/events`);
    if (state !== calendarState || !calendarWin?.open) return;
    state.events = events;
    renderCalendar(state);
  } catch (error) {
    if (state === calendarState && calendarWin?.open) toast(calErrorText(error));
  }
}

function refreshCalendar() {  // 다른 사람이 일정을 바꿨을 때 서버 알림으로 부른다
  if (calendarState && calendarWin?.open && currentRoom?.roomId === calendarState.roomId) loadCalendar(calendarState);
}

function renderCalendar(state) {
  const {nav, grid, day} = state.parts;
  const byDate = new Map();
  for (const event of state.events) {
    if (!byDate.has(event.date)) byDate.set(event.date, []);
    byDate.get(event.date).push(event);
  }
  // 위쪽: 이전 달 / 제목 / 다음 달 / 오늘
  const shown = new Date(state.year, state.month, 1);
  const move = delta => { const next = new Date(state.year, state.month + delta, 1); state.year = next.getFullYear(); state.month = next.getMonth(); renderCalendar(state); };
  const title = calNode("strong", null, new Intl.DateTimeFormat(locale, {year: "numeric", month: "long"}).format(shown));
  const prev = calButton("‹", "ghost", () => move(-1)), next = calButton("›", "ghost", () => move(1));
  prev.title = t("calPrev"); next.title = t("calNext");
  const todayButton = calButton(t("calToday"), "ghost", () => { const now = new Date(); state.year = now.getFullYear(); state.month = now.getMonth(); state.selected = calIso(now); syncCalendarFormDate(state); renderCalendar(state); });
  nav.replaceChildren(prev, title, next, todayButton);
  // 달력: 요일 줄 + 6주
  grid.replaceChildren();
  for (let index = 0; index < 7; index++) {
    grid.append(calNode("div", "cal-dow", new Intl.DateTimeFormat(locale, {weekday: "short"}).format(new Date(2023, 0, 1 + index))));  // 2023-01-01은 일요일
  }
  const first = new Date(state.year, state.month, 1), todayIso = calIso(new Date());
  for (let index = 0; index < 42; index++) {
    const date = new Date(state.year, state.month, 1 - first.getDay() + index), iso = calIso(date), events = byDate.get(iso) || [];
    const cell = calButton("", "cal-cell", () => { state.selected = iso; if (!state.editing) syncCalendarFormDate(state); if (date.getMonth() !== state.month) { state.year = date.getFullYear(); state.month = date.getMonth(); } renderCalendar(state); });
    cell.classList.toggle("other", date.getMonth() !== state.month);
    cell.classList.toggle("today", iso === todayIso);
    cell.classList.toggle("selected", iso === state.selected);
    cell.append(calNode("span", "cal-num", String(date.getDate())));
    const dots = calNode("span", "cal-dots");
    for (let dot = 0; dot < Math.min(events.length, 3); dot++) dots.append(calNode("i"));
    cell.append(dots);
    if (events.length) cell.title = events.map(event => `${event.time || t("calAllDay")} ${event.title}`).join("\n");
    grid.append(cell);
  }
  renderCalendarDay(state, byDate.get(state.selected) || []);
}

function renderCalendarDay(state, events) {
  const {day} = state.parts;
  day.replaceChildren(calNode("div", "cal-day-title", new Intl.DateTimeFormat(locale, {month: "long", day: "numeric", weekday: "short"}).format(calParse(state.selected))));
  if (!events.length) { day.append(calNode("div", "cal-empty", t("calNoEvents"))); return; }
  for (const event of events) {
    const item = calNode("div", "cal-event"), head = calNode("div", "cal-event-head");
    head.append(calNode("span", "cal-time", event.time || t("calAllDay")), calNode("strong", null, event.title));
    item.append(head);
    if (event.note) item.append(calNode("div", "cal-note", event.note));
    item.append(calNode("small", null, event.createdBy));
    if (event.canEdit) {
      const actions = calNode("div", "cal-act");
      actions.append(
        calButton(t("calEdit"), "ghost", () => startCalendarEdit(state, event)),
        calButton(t("calDelete"), "ghost danger", () => deleteCalendarEvent(state, event)));
      item.append(actions);
    }
    day.append(item);
  }
}

function buildCalendarForm(state) {
  const {form} = state.parts;
  const title = calNode("input"), date = calNode("input"), time = calNode("input"), note = calNode("textarea");
  title.maxLength = 100; title.placeholder = t("calTitlePlaceholder");
  date.type = "date"; date.required = true; date.value = state.selected; date.min = "2000-01-01"; date.max = "2100-12-31";
  time.type = "time"; time.title = t("calTimeLabel");
  note.maxLength = 500; note.rows = 2; note.placeholder = t("calNotePlaceholder");
  const row = calNode("div", "cal-row");
  row.append(date, time);
  const submit = calNode("button", "primary", t("calAdd")), cancel = calButton(t("calCancel"), "ghost", () => resetCalendarForm(state));
  submit.type = "submit"; cancel.hidden = true;
  const actions = calNode("div", "cal-row");
  actions.append(submit, cancel);
  form.append(title, row, note, actions);
  state.form = {title, date, time, note, submit, cancel};
  form.onsubmit = async event => {
    event.preventDefault();
    if (state.busy || state !== calendarState) return;
    const body = {title: title.value.trim(), date: date.value, time: time.value, note: note.value.trim()};
    if (!body.title) { toast(t("calErr_TITLE_REQUIRED")); title.focus(); return; }
    if (!body.date) { toast(t("calErr_INVALID_DATE")); return; }
    state.busy = true; submit.disabled = true; setBusy(submit, true);
    try {
      const path = `/api/rooms/${state.roomId}/events`;
      const saved = state.editing ? await api(`${path}/${state.editing}`, "PATCH", body) : await api(path, "POST", body);
      if (state !== calendarState) return;
      state.selected = saved.date;
      const shown = calParse(saved.date); state.year = shown.getFullYear(); state.month = shown.getMonth();  // 저장한 날짜로 달력이 이동한다
      resetCalendarForm(state);
      await loadCalendar(state);
    } catch (error) {
      toast(calErrorText(error));
    } finally {
      state.busy = false; submit.disabled = false; setBusy(submit, false);
    }
  };
}

function syncCalendarFormDate(state) { if (state.form && !state.editing) state.form.date.value = state.selected; }

function resetCalendarForm(state) {
  const f = state.form;
  state.editing = null;
  f.title.value = ""; f.time.value = ""; f.note.value = ""; f.date.value = state.selected;
  f.submit.textContent = t("calAdd"); f.cancel.hidden = true;
}

function startCalendarEdit(state, event) {
  const f = state.form;
  state.editing = event.id;
  f.title.value = event.title; f.date.value = event.date; f.time.value = event.time; f.note.value = event.note;
  f.submit.textContent = t("calSave"); f.cancel.hidden = false;
  f.title.focus();
}

async function deleteCalendarEvent(state, event) {
  if (!confirm(t("calDeleteConfirm"))) return;
  try {
    await api(`/api/rooms/${state.roomId}/events/${event.id}`, "DELETE");
    if (state.editing === event.id) resetCalendarForm(state);
    await loadCalendar(state);
  } catch (error) {
    toast(calErrorText(error));
    loadCalendar(state);
  }
}

$("calendarBtn").onclick = toggleCalendar;

/* ---------- 일정 찾기: 요약에 쓴 자료에서 마감·일정 후보를 찾아 카드로 보여 주고, 확인한 것만 캘린더에 넣는다 ---------- */
function addDeadlineFinder(container, sources, active) {
  if (typeof currentRoom === "undefined" || !currentRoom) return;
  const roomId = currentRoom.roomId;
  const box = calNode("div", "cal-find"), results = calNode("div", "cal-found");
  let running = false;
  const button = calButton(t("calFind"), "ghost", async () => {
    if (running) return;
    if (!await ensureAiConsent() || !active() || running) return;
    running = true; button.disabled = true; button.textContent = t("calFinding"); setBusy(button, true);
    results.replaceChildren();
    try {
      const found = await api(`/api/rooms/${roomId}/events/suggest`, "POST", {sources, consent: true, tzOffsetMinutes: -new Date().getTimezoneOffset()});
      if (active()) renderSuggestions(results, roomId, found.suggestions);
    } catch (error) {
      if (active()) results.replaceChildren(calNode("div", "cal-empty", calErrorText(error)));
    } finally {
      running = false; button.disabled = false; button.textContent = t("calFind"); setBusy(button, false);
    }
  });
  box.append(button, results);
  container.append(box);
}

function renderSuggestions(container, roomId, suggestions) {
  if (!suggestions.length) { container.replaceChildren(calNode("div", "cal-empty", t("calNoneFound"))); return; }
  const cards = suggestions.map(suggestion => suggestionCard(roomId, suggestion));
  const head = calNode("div", "cal-found-head");
  head.append(calNode("strong", null, t("calFoundN").replace("{n}", suggestions.length)));
  if (cards.length > 1) {
    const all = calButton(t("calAddAll"), "ghost", async () => { all.disabled = true; for (const card of cards) await card.add(); });
    head.append(all);
  }
  container.replaceChildren(head, ...cards.map(card => card.node));
}

function suggestionCard(roomId, suggestion) {
  const node = calNode("div", "cal-card"), title = calNode("input"), date = calNode("input"), time = calNode("input");
  title.maxLength = 100; title.value = suggestion.title;
  date.type = "date"; date.value = suggestion.date; date.min = "2000-01-01"; date.max = "2100-12-31";
  time.type = "time"; time.value = suggestion.time; time.title = t("calTimeLabel");
  const row = calNode("div", "cal-row");
  row.append(date, time);
  let done = false;
  const addButton = calButton(t("calAddToCal"), "primary", () => card.add());
  const card = {node, async add() {
    if (done) return;
    if (!title.value.trim() || !date.value) { toast(t(title.value.trim() ? "calErr_INVALID_DATE" : "calErr_TITLE_REQUIRED")); return; }
    addButton.disabled = true; setBusy(addButton, true);
    try {
      await api(`/api/rooms/${roomId}/events`, "POST", {title: title.value.trim(), date: date.value, time: time.value, note: suggestion.evidence});
      done = true; addButton.textContent = t("calAdded"); node.classList.add("done");
      for (const input of [title, date, time]) input.disabled = true;
    } catch (error) {
      toast(calErrorText(error)); addButton.disabled = false;
    } finally {
      setBusy(addButton, false);
    }
  }};
  node.append(title, row, calNode("small", "cal-source", suggestion.evidence), addButton);
  return card;
}
