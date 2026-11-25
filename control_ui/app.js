"use strict";
const tg = window.Telegram?.WebApp;
tg?.ready();
tg?.expand();
const $ = id => document.getElementById(id);
const panel = $("panel");
const levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"];
const selected = new Set(levels.slice(1));
let current = "control", owner = false, epoch = 0, logView;

function el(tag, text, cls) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (cls) node.className = cls;
  return node;
}
async function api(path, data) {
  const headers = {"X-Telegram-Init-Data": tg?.initData || ""};
  if (data !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(`./api/${path}`, {headers, method: data === undefined ? "GET" : "POST", body: data === undefined ? undefined : JSON.stringify(data)});
  const value = await res.json();
  if (!res.ok) throw Error(value.error || "Запрос не выполнен.");
  return value;
}
const command = async text => (await api("edit", {command:text})).result;
function notice(text) { $("notice").textContent = text; $("notice").hidden = !text; }
function button(text, action, cls) {
  const b = el("button", text, cls);
  b.type = "button";
  b.onclick = async () => {
    b.disabled = true;
    try { await action(); } catch (err) { notice(err.message); }
    finally { b.disabled = false; }
  };
  return b;
}
function field(label, type = "text", value = "") {
  const wrap = el("label", label);
  const input = el(type === "textarea" ? "textarea" : "input");
  if (type !== "textarea") input.type = type;
  input.value = value;
  wrap.append(input);
  return [wrap, input];
}
function heading(title) { panel.append(el("h2", title)); }
function actions(...items) { const row = el("div", undefined, "actions"); row.append(...items); return row; }
function block(title, controls, buttons) {
  const node = el("section", undefined, "band stack");
  node.append(el("h3", title), ...controls, actions(...buttons));
  return node;
}
async function status() {
  const data = await api("status");
  $("status").textContent = data.status;
  $("dot").classList.toggle("online", data.status.startsWith("Ада в сети"));
  if (owner !== data.owner) { owner = data.owner; tabs(); }
}
function control() {
  heading("Управление");
  const labels = [["start","Запустить","primary"],["stop","Остановить","danger"],["restart","Перезапустить"],["maintenance","Техработы"]];
  panel.append(actions(...labels.map(([action, label, cls]) => button(label, async () => {
    if (action !== "start" && !confirm(`${label} Аду?`)) return;
    notice((await api("action", {action})).result);
    await status();
  }, cls))));
  const details = el("pre", ""); details.hidden = true;
  panel.append(actions(...[["Диагностика","состояние"],["Модули","модули"]].map(([label, cmd]) => button(label, async () => {
    details.textContent = await command(cmd); details.hidden = false;
  }))), details);
}
async function mood() {
  const run = epoch;
  heading("Настроение");
  const state = JSON.parse(await command("эмоции"));
  if (run !== epoch) return;
  const grid = el("div", undefined, "grid");
  for (const [key, value] of Object.entries(state)) {
    if (typeof value !== "number") continue;
    const [label, input] = field(key, "range");
    input.min = "0"; input.max = "100"; input.value = value;
    const out = el("output", value);
    input.oninput = () => { out.textContent = input.value; };
    grid.append(block(key, [label, out], [button("Сохранить", async () => notice(await command(`эмоции ${key} ${input.value}`)))]));
  }
  panel.append(grid);
}
async function prompt() {
  const run = epoch;
  heading("Промпт");
  const [label, input] = field("Базовый промпт", "textarea", await command("промпт базовый"));
  if (run !== epoch) return;
  input.className = "prompt"; input.maxLength = 12000;
  panel.append(label, actions(button("Сохранить промпт", async () => {
    if (!input.value.trim()) throw Error("Промпт пуст.");
    notice(await command(`промпт установить ${input.value}`));
  }, "primary")));
}
function memory() {
  heading("Память");
  const output = el("pre", ""); output.hidden = true;
  const show = async cmd => { output.textContent = await command(cmd); output.hidden = false; };
  const [factLabel, fact] = field("Факт", "textarea");
  const [idLabel, id] = field("ID записи");
  const [groupLabel, group] = field("ID группы"); groupLabel.hidden = true;
  const scopeLabel = el("label", "Область"); const scope = el("select");
  for (const [value, label] of [["личное","Личное"],["группа","Группа"],["global","Общее"]]) {
    const option = el("option", label); option.value = value; scope.append(option);
  }
  scopeLabel.append(scope); scope.onchange = () => { groupLabel.hidden = scope.value !== "группа"; };
  panel.append(actions(button("Записи", () => show("память")), button("История изменений", () => show("память история"))), output,
    block("Добавить факт", [scopeLabel, groupLabel, factLabel], [button("Добавить", async () => {
      if (!fact.value.trim() || (scope.value === "группа" && !/^-\d+$/.test(group.value.trim()))) throw Error("Проверь факт и ID группы.");
      notice(await command(`память добавить ${scope.value} ${scope.value === "группа" ? group.value.trim() + " " : ""}${fact.value.trim()}`));
      await show("память");
    }, "primary")]),
    block("Удаление", [idLabel], [button("Удалить запись", async () => {
      if (!/^\d+$/.test(id.value.trim())) throw Error("Укажи ID записи.");
      if (confirm("Удалить запись?")) { notice(await command(`память удалить ${id.value.trim()}`)); await show("память"); }
    }, "danger"), button("Очистить память и историю", async () => {
      if (confirm("Безвозвратно очистить память, историю сообщений и журналы?")) { notice(await command("память очистить")); await show("память"); }
    }, "danger")]));
}
async function loadLogs() {
  const view = logView;
  const {rows} = await api(`logs?levels=${encodeURIComponent([...selected].join(","))}`);
  if (view !== logView || !view?.isConnected) return;
  const follow = view.scrollHeight - view.scrollTop - view.clientHeight < 30;
  const entries = rows.map(([level, text]) => el("div", text, `log-${level}`));
  view.replaceChildren(...(entries.length ? entries : [el("span", "Нет записей выбранных уровней.", "muted")]));
  if (follow) view.scrollTop = view.scrollHeight;
}
async function logs() {
  heading("Журнал");
  const choices = el("div", undefined, "row");
  for (const level of levels) {
    const label = el("label"); const box = el("input"); box.type = "checkbox"; box.checked = selected.has(level);
    box.onchange = async () => { box.checked ? selected.add(level) : selected.delete(level); try { await loadLogs(); } catch (err) { notice(err.message); } };
    label.append(box, level); choices.append(label);
  }
  logView = el("pre"); panel.append(choices, logView); await loadLogs();
}
async function settings() {
  const run = epoch;
  heading("Переменные .env");
  const {fields} = await api("env");
  if (run !== epoch) return;
  const [filterLabel, filter] = field("Фильтр переменных", "search");
  const grid = el("div", undefined, "grid");
  const changes = Object.create(null);
  const inputs = fields.map(({key, value, secret, configured, editable}) => {
    const [label, input] = field(key, secret ? "password" : "text", value ?? "");
    input.autocomplete = "off"; input.spellcheck = false; input.disabled = editable === false;
    if (secret) input.placeholder = configured ? "Задано · новое значение" : "Не задано";
    const original = value ?? "";
    input.oninput = () => { if (input.value === original) delete changes[key]; else changes[key] = input.value; };
    if (secret && configured && editable !== false) label.append(button("Очистить", async () => {
      if (confirm(`Очистить ${key} при сохранении?`)) { changes[key] = ""; input.value = ""; input.placeholder = "Будет очищено"; }
    }, "danger"));
    grid.append(label); return [key, label];
  });
  filter.oninput = () => { for (const [key, label] of inputs) label.hidden = !key.includes(filter.value.toUpperCase()); };
  panel.append(filterLabel, actions(button("Сохранить .env", async () => {
    await api("env", {changes}); await open("env"); notice("Сохранено. Для настроек Ады нажми «Перезапустить».");
  }, "primary")), grid);
}
function editor() {
  heading("Редактирование");
  const output = el("pre", ""); output.hidden = true;
  const show = async cmd => { output.textContent = await command(cmd); output.hidden = false; };
  const shortcuts = [["Команды","меню"],["Календарь","календарь"],["Задачи","задачи"],["Запросы встреч","запросы"],["УКРТБ сегодня","расписание сегодня"],["УКРТБ завтра","расписание завтра"],["Импортированное расписание","расписание показать"],["Обновить расписание","расписание обновить"]];
  const [cmdLabel, cmd] = field("Команда редактирования", "textarea");
  panel.append(actions(...shortcuts.map(([label, text]) => button(label, () => show(text)))),
    cmdLabel, actions(button("Выполнить", async () => {
      if (!cmd.value.trim()) throw Error("Команда пуста.");
      if (cmd.value.trim().toLowerCase() === "память очистить" && !confirm("Очистить память и историю?")) return;
      await show(cmd.value);
    }, "primary")), output);
}
const views = [["control","Управление",control],["mood","Настроение",mood],["prompt","Промпт",prompt],["memory","Память",memory],["logs","Логи",logs],["edit","Ещё",editor],["env",".env",settings]];
function tabs() {
  $("tabs").replaceChildren(...views.filter(([key]) => key !== "env" || owner).map(([key, title]) => {
    const b = button(title, () => open(key)); b.setAttribute("aria-current", String(current === key)); return b;
  }));
}
async function open(key) {
  current = key; const run = ++epoch; logView = null; panel.replaceChildren(); panel.className = "stack"; notice(""); tabs();
  try { await views.find(item => item[0] === key)[2](); }
  catch (err) { if (run === epoch) notice(err instanceof SyntaxError ? "Ада не готова. Проверь состояние." : err.message); }
}
async function init() {
  if (!tg?.initData) { $("status").textContent = "Нет сеанса Telegram"; notice("Открой панель кнопкой в личке с ботом."); return; }
  try { await status(); await open("control"); }
  catch (err) { $("status").textContent = "Нет доступа"; notice(err.message); return; }
  setInterval(async () => {
    if (document.hidden) return;
    try { await status(); if (logView) await loadLogs(); }
    catch (err) { notice(err.message); }
  }, 3500);
}
void init();
