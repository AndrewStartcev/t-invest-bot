"use strict";
const el = id => document.getElementById(id);
const ALLOWED = new Set(["https://invest.argokov.ru", "http://127.0.0.1:8765", "http://localhost:8765"]);
const FIELDS = ["endpoint", "client", "profile", "key", "interval", "enabled"];

function status(message, failed = false) {
  el("status").textContent = message;
  el("status").style.background = failed ? "#fde9e8" : "#e8f0fb";
  el("status").style.color = failed ? "#982b2b" : "#264a78";
}

async function save() {
  const origin = el("endpoint").value.trim().replace(/\/$/,"");
  if (!ALLOWED.has(origin)) throw Error("Разрешён только invest.argokov.ru или локальная админка");
  const profile = new URL(el("profile").value);
  if (profile.protocol !== "https:" ||
      !["www.tbank.ru","tbank.ru","www.tbank-online.com","tbank-online.com"].includes(profile.hostname) ||
      !/^\/invest\/(?:social|pulse)\/profile\/[^/]+\/?$/.test(profile.pathname))
    throw Error("Введи ссылку на профиль автора, не на страницу Сделки");
  if (!el("config").reportValidity()) throw Error("Проверь поля");
  await chrome.storage.local.set({
    endpoint:origin, client:el("client").value.trim(), profile_url:profile.href,
    key:el("key").value.trim(), interval:Number(el("interval").value),
    enabled:el("enabled").checked
  });
  status("Настройки сохранены. Банковские cookie расширение не считывает.");
}

el("config").addEventListener("submit", event => {
  event.preventDefault();
  save().catch(error => status(error.message,true));
});
el("test").addEventListener("click", async () => {
  try {
    await save();
    const result=await chrome.runtime.sendMessage({kind:"test"});
    if (!result?.ok) throw Error(result?.error || "Сервер не подтвердил соединение");
    status("✅ Ключ принят сервером. Канал только для чтения.");
  } catch(error) {status(error.message,true);}
});
el("open").addEventListener("click", async () => {
  try {
    await save();
    const link = new URL(el("profile").value);
    link.pathname = link.pathname.replace(/\/invest\/(?:social|pulse)\/profile\/([^/]+)\/?$/,
      "/invest/pulse/profile/$1/operations/");
    await chrome.tabs.create({url:link.href,active:true});
    status("Войди в банк, открой Сделки и обнови вкладку (F5) для захвата API.");
  } catch(error) {status(error.message,true);}
});
(async () => {
  const data=await chrome.storage.local.get(FIELDS.concat(["profile_url","last_status","last_sent_at","last_error"]));
  el("endpoint").value=data.endpoint||"https://invest.argokov.ru";
  el("client").value=data.client||"admin";
  el("profile").value=data.profile_url||"https://www.tbank.ru/invest/social/profile/LinMath/";
  el("key").value=data.key||"";
  el("interval").value=String(data.interval===3?3:5);
  el("enabled").checked=Boolean(data.enabled);
  if(data.last_error)status("Последняя ошибка: "+data.last_error,true);
  else if(data.last_sent_at)status("Последний ответ: "+(data.last_status||"—")+" · "+new Date(data.last_sent_at).toLocaleString("ru-RU"));
})().catch(error=>status(error.message,true));
