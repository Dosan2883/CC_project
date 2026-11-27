/* 내 캘린더: 사용자마다 따로 있는 개인 달력. 떠 있는 창에 월간 달력과 선택한 날의 일정을 보여 주고, 내가 직접 일정을 추가·수정·삭제한다.
   다른 사람은 내 일정을 볼 수 없다. 창 안의 글은 모두 textContent로만 넣는다. */
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

const CAL_API = "/api/calendar/events";
const CAL_COLORS = ["blue", "red", "green", "orange", "purple", "cyan", "pink", "lime"].map(color => `var(--cal-room-${color})`);

/* 일정에는 '관련 방'을 붙일 수 있고, 방마다 다른 색으로 보인다. 방 id 순서로 색을 정해서 방이 늘어도 기존 방의 색은 바뀌지 않는다.
   방을 붙이지 않았거나 이미 나간 방의 일정은 회색이다. */
let calColorCache = {rooms: null, colors: new Map()};
function calRoomColor(roomId) {
  const rooms = typeof lastRooms === "undefined" ? [] : lastRooms;
  if (calColorCache.rooms !== rooms) {  // 방 목록이 바뀔 때만 색을 다시 정한다(점마다 정렬하지 않는다)
    const sorted = [...rooms].sort((a, b) => a.roomId.localeCompare(b.roomId));
    calColorCache = {rooms, colors: new Map(sorted.map((room, index) => [room.roomId, CAL_COLORS[index % CAL_COLORS.length]]))};
  }
  return calColorCache.colors.get(roomId) || "var(--muted)";
}

function toggleCalendar() {
  if (calendarWin?.open) calendarWin.close();
  else openCalendar();
}

function openCalendar() {
  const today = new Date();
  const state = calendarState = {year: today.getFullYear(), month: today.getMonth(), selected: calIso(today), events: [], editing: null, busy: false};
  const win = calendarWin = openFloatWindow(t("calMine"), {width: 420, height: 600});
  win.onClose = () => { if (calendarWin === win) { calendarWin = null; calendarState = null; } };
  const nav = calNode("div", "cal-nav"), grid = calNode("div", "cal-grid"), day = calNode("div", "cal-day"), form = calNode("form", "cal-form");
  const legend = calNode("div", "cal-legend");
  win.body.append(nav, grid, legend, day, form, calNode("div", "cal-empty", t("calPrivateHint")));
  state.parts = {nav, grid, legend, day, form};
  buildCalendarForm(state);
  renderCalendar(state);
  loadCalendar(state);
}

async function loadCalendar(state) {
  if (!state || state !== calendarState || !calendarWin?.open) return;
  try {
    const events = await api(CAL_API);
    if (state !== calendarState || !calendarWin?.open) return;
    state.events = events;
    renderCalendar(state);
    updateAlerts(events);  // 추가·수정·삭제한 결과가 종 알림에도 바로 반영된다
  } catch (error) {
    if (state === calendarState && calendarWin?.open) toast(calErrorText(error));
  }
}

function renderCalendar(state) {
  const {nav, grid, legend} = state.parts;
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
    for (const dotColor of [...new Set(events.map(event => calRoomColor(event.roomId)))].slice(0, 3)) {  // 같은 방 일정은 점 하나로 묶는다
      const dot = calNode("i"); dot.style.background = dotColor; dots.append(dot);
    }
    cell.append(dots);
    if (events.length) cell.title = events.map(event => `${event.time || t("calAllDay")} ${event.title}`).join("\n");
    grid.append(cell);
  }
  // 범례: 일정에 붙은 방 이름과 색
  const tags = new Map(state.events.map(event => [event.roomId || "", event.roomId ? event.roomName : t("calNoRoom")]));  // 방 표시가 없는 일정은 회색 "관련 방 없음"
  legend.replaceChildren(...[...tags].map(([roomId, name]) => {
    const chip = calNode("span", "cal-legend-item", name), dot = calNode("i");
    dot.style.background = calRoomColor(roomId); chip.prepend(dot);
    return chip;
  }));
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
    item.style.borderLeft = `4px solid ${calRoomColor(event.roomId)}`;
    item.append(calNode("small", "cal-room", event.roomName || t("calNoRoom")));  // 어느 방과 관련된 일정인지
    if (event.note) item.append(calNode("div", "cal-note", event.note));
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
  const room = calNode("select"), row = calNode("div", "cal-row");
  room.title = t("calRoomTitle");
  row.append(date, time);
  const submit = calNode("button", "primary", t("calAdd")), cancel = calButton(t("calCancel"), "ghost", () => resetCalendarForm(state));
  submit.type = "submit"; cancel.hidden = true;
  const actions = calNode("div", "cal-row");
  actions.append(submit, cancel);
  form.append(title, row, room, note, actions);
  state.form = {title, date, time, note, room, submit, cancel};
  fillCalendarRooms(state, currentRoom?.roomId || "");
  form.onsubmit = async event => {
    event.preventDefault();
    if (state.busy || state !== calendarState) return;
    const body = {title: title.value.trim(), date: date.value, time: time.value, note: note.value.trim(), roomId: room.value, roomName: room.selectedOptions[0]?.dataset.name || ""};
    if (!body.title) { toast(t("calErr_TITLE_REQUIRED")); title.focus(); return; }
    if (!body.date) { toast(t("calErr_INVALID_DATE")); return; }
    state.busy = true; submit.disabled = true; setBusy(submit, true);
    try {
      const saved = state.editing ? await api(`${CAL_API}/${state.editing}`, "PATCH", body) : await api(CAL_API, "POST", body);
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

function fillCalendarRooms(state, selectedId, selectedName = "") {  // 관련 방 선택칸: 방 없음 + 내가 들어간 방들(이미 나간 방의 일정은 그 방 이름도 보여 준다)
  const select = state.form.room, rooms = typeof lastRooms === "undefined" ? [] : lastRooms;
  const options = [["", t("calNoRoom"), ""], ...rooms.map(room => [room.roomId, room.name, room.name])];
  if (selectedId && !rooms.some(room => room.roomId === selectedId)) options.push([selectedId, selectedName, selectedName]);
  select.replaceChildren(...options.map(([value, label, name]) => { const option = new Option(label, value); option.dataset.name = name; return option; }));
  select.value = selectedId;
}

function syncCalendarFormDate(state) { if (state.form && !state.editing) state.form.date.value = state.selected; }

function resetCalendarForm(state) {
  const f = state.form;
  state.editing = null;
  f.title.value = ""; f.time.value = ""; f.note.value = ""; f.date.value = state.selected;
  fillCalendarRooms(state, currentRoom?.roomId || "");
  f.submit.textContent = t("calAdd"); f.cancel.hidden = true;
}

function startCalendarEdit(state, event) {
  const f = state.form;
  state.editing = event.id;
  f.title.value = event.title; f.date.value = event.date; f.time.value = event.time; f.note.value = event.note;
  fillCalendarRooms(state, event.roomId || "", event.roomName);
  f.submit.textContent = t("calSave"); f.cancel.hidden = false;
  f.title.focus();
}

async function deleteCalendarEvent(state, event) {
  if (!confirm(t("calDeleteConfirm"))) return;
  try {
    await api(`${CAL_API}/${event.id}`, "DELETE");
    if (state.editing === event.id) resetCalendarForm(state);
    await loadCalendar(state);
  } catch (error) {
    toast(calErrorText(error));
    loadCalendar(state);
  }
}


/* ---------- 일정 찾기: 요약에 쓴 자료에서 마감·일정 후보를 찾아 카드로 보여 주고, 확인한 것만 캘린더에 넣는다 ---------- */
function addDeadlineFinder(container, sources, active) {
  if (typeof currentRoom === "undefined" || !currentRoom) return;
  const room = {roomId: currentRoom.roomId, roomName: currentRoom.name};  // 찾은 일정은 이 방과 관련된 일정으로 표시된다
  const roomId = room.roomId;
  const box = calNode("div", "cal-find"), results = calNode("div", "cal-found");
  let running = false;
  const button = calButton(t("calFind"), "ghost", async () => {
    if (running) return;
    if (!await ensureAiConsent() || !active() || running) return;
    running = true; button.disabled = true; button.textContent = t("calFinding"); setBusy(button, true);
    results.replaceChildren();
    try {
      const found = await api(`/api/rooms/${roomId}/events/suggest`, "POST", {
        sources, consent: true, tzOffsetMinutes: -new Date().getTimezoneOffset()
      }, {timeout: 300000});
      if (active()) renderSuggestions(results, found.suggestions, room);
    } catch (error) {
      if (active()) results.replaceChildren(calNode("div", "cal-empty", calErrorText(error)));
    } finally {
      running = false; button.disabled = false; button.textContent = t("calFind"); setBusy(button, false);
    }
  });
  box.append(button, results);
  container.append(box);
}

function renderSuggestions(container, suggestions, room) {
  if (!suggestions.length) { container.replaceChildren(calNode("div", "cal-empty", t("calNoneFound"))); return; }
  const cards = suggestions.map(suggestion => suggestionCard(suggestion, room));
  const head = calNode("div", "cal-found-head");
  head.append(calNode("strong", null, t("calFoundN").replace("{n}", suggestions.length)));
  if (cards.length > 1) {
    const all = calButton(t("calAddAll"), "ghost", async () => { all.disabled = true; for (const card of cards) await card.add(); });
    head.append(all);
  }
  container.replaceChildren(head, ...cards.map(card => card.node));
}

function suggestionCard(suggestion, room) {
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
      await api(CAL_API, "POST", {title: title.value.trim(), date: date.value, time: time.value, note: suggestion.evidence, roomId: room.roomId, roomName: room.roomName});
      done = true; addButton.textContent = t("calAdded"); node.classList.add("done");
      refreshAlerts();
      for (const input of [title, date, time]) input.disabled = true;
    } catch (error) {
      toast(calErrorText(error)); addButton.disabled = false;
    } finally {
      setBusy(addButton, false);
    }
  }};
  const chip = calNode("span", "cal-room-chip", room.roomName), chipDot = calNode("i");  // 이 기한이 나온 방의 이름과 색
  chipDot.style.background = calRoomColor(room.roomId); chip.prepend(chipDot);
  node.append(chip, title, row, calNode("small", "cal-source", suggestion.evidence), addButton);
  return card;
}
