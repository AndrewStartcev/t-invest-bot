"use strict";
const el = id => document.getElementById(id);
const ALLOWED = new Set(["https://invest.argokov.ru", "http://127.0.0.1:8765", "http://localhost:8765"]);
const FIELDS = ["endpoint", "client", "key", "interval", "enabled"];

function status(message, failed = false) {
  el("status").textContent = message;
  el("status").style.background = failed ? "#fde9e8" : "#e8f0fb";
  el("status").style.color = failed ? "#982b2b" : "#264a78";
}

function showAuthor(url) {
  if (typeof url !== "string") throw Error("Не удалось получить автора из админки");
  const selected = new URL(url);
  const match = /^\/invest\/(?:social|pulse)\/profile\/([^/]+)\/?$/.exec(selected.pathname);
  if (selected.protocol !== "https:" ||
      !["www.tbank.ru", "tbank.ru", "www.tbank-online.com", "tbank-online.com"].includes(selected.hostname) ||
      !match) throw Error("Админка вернула некорректную ссылку Пульса");
  el("selected-author").textContent = decodeURIComponent(match[1]);
}

async function send(kind) {
  const reply = await chrome.runtime.sendMessage({kind});
  if (!reply?.ok) throw Error(reply?.error || "Нет подтверждения от расширения");
  return reply.data || {};
}

async function save() {
  const origin = el("endpoint").value.trim().replace(/\/$/, "");
  if (!ALLOWED.has(origin)) throw Error("Разрешён только invest.argokov.ru или локальная админка");
  if (!el("config").reportValidity()) throw Error("Проверь поля");
  const old = await chrome.storage.local.get("enabled");
  if (old.enabled && !el("enabled").checked) await send("stop");
  await chrome.storage.local.set({
    endpoint: origin,
    client: el("client").value.trim(),
    key: el("key").value.trim(),
    interval: Number(el("interval").value),
    enabled: el("enabled").checked
  });
  // An author is never configured in the browser: remove legacy settings
  // from a previously installed version.
  await chrome.storage.local.remove("profile_url");
  status("Настройки сохранены. Автор автоматически берётся из админки.");
}

el("config").addEventListener("submit", event => {
  event.preventDefault();
  save().catch(error => status(error.message, true));
});
el("test").addEventListener("click", async () => {
  try {
    await save();
    const result = await send("test");
    showAuthor(result.profile_url);
    status("✅ Ключ принят. Отслеживаемый автор: " + el("selected-author").textContent + ". Только чтение.");
  } catch (error) { status(error.message, true); }
});
el("open").addEventListener("click", async () => {
  try {
    await save();
    await send("open_profile");
    status("Открыл сделки выбранного автора. Войди в Т-Банк и обнови вкладку (F5).");
  } catch (error) { status(error.message, true); }
});
(async () => {
  const data = await chrome.storage.local.get(FIELDS.concat(["last_status", "last_sent_at", "last_error"]));
  el("endpoint").value = data.endpoint || "https://invest.argokov.ru";
  el("client").value = data.client || "admin";
  el("key").value = data.key || "";
  el("interval").value = String(data.interval === 3 ? 3 : 5);
  el("enabled").checked = Boolean(data.enabled);
  if (data.last_error) status("Последняя ошибка: " + data.last_error, true);
  else if (data.last_sent_at) status("Последний ответ: " + (data.last_status || "—") + " · " +
    new Date(data.last_sent_at).toLocaleString("ru-RU"));
  if (el("key").value.length >= 32) {
    try { showAuthor((await send("test")).profile_url); }
    catch (error) { status("Автор не загружен: " + error.message, true); }
  }
})().catch(error => status(error.message, true));
