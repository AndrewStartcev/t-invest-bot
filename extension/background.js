/* Pulse authorization watchdog. No trades, instrument lists, banking cookies,
 * Authorization headers or brokerage APIs are accessed or sent to the server. */
"use strict";
const BACKENDS = new Set(["https://invest.argokov.ru", "http://127.0.0.1:8765", "http://localhost:8765"]);
const BANKS = new Set(["www.tbank.ru","tbank.ru","www.tbank-online.com","tbank-online.com"]);
const monitored = new Set();

function bankTab(sender) {
  try {
    const u = new URL(sender.tab?.url);
    return sender.tab?.id >= 0 && u.protocol === "https:" && BANKS.has(u.hostname) &&
      /^\/invest\/(?:social|pulse)\/profile\//.test(u.pathname);
  } catch { return false; }
}

async function send(report) {
  const cfg = await chrome.storage.local.get(["endpoint", "client", "key", "enabled"]);
  const origin = String(cfg.endpoint || "").replace(/\/$/, "");
  if (!BACKENDS.has(origin)) throw Error("Разрешён только сервер T-Invest Bot или локальная админка");
  if (!/^[a-z][a-z0-9_-]{2,31}$/.test(cfg.client || "")) throw Error("Неверный логин кабинета");
  if (typeof cfg.key !== "string" || cfg.key.length < 32) throw Error("Укажи ключ из админки");
  if (!cfg.enabled && report.kind !== "config" && report.status !== "stopped") throw Error("Контроль отключён");
  const response = await fetch(origin + "/api/extension/report", {
    method:"POST", cache:"no-store", signal:AbortSignal.timeout(15000),
    headers:{"Content-Type":"application/json",
             "X-TInvest-Extension-Client":cfg.client,"X-TInvest-Extension-Key":cfg.key},
    body:JSON.stringify(report)
  });
  const data = await response.json().catch(()=>({}));
  if (!response.ok || !data.ok) throw Error(data.error || "HTTP " + response.status);
  if (report.kind === "auth_status") {
    await chrome.storage.local.set({last_status:report.status,
      last_sent_at:new Date().toISOString(),last_error:""});
  } else if (report.kind === "config") {
    await chrome.storage.local.set({last_error:""});
  }
  return data;
}
async function selectedProfile() {
  const result = await send({kind:"config"});
  const u = new URL(result.profile_url);
  if (u.protocol !== "https:" || !BANKS.has(u.hostname) ||
      !/^\/invest\/(?:social|pulse)\/profile\/[^/]+\/?$/.test(u.pathname))
    throw Error("Админка вернула некорректный адрес Пульса");
  return u;
}
function loginUrl(profile) {
  const url = new URL(profile);
  url.pathname = url.pathname.replace(
    /\/invest\/(?:social|pulse)\/profile\/([^/]+)\/?$/,
    "/invest/pulse/profile/$1/operations/");
  return url.href;
}
chrome.runtime.onMessage.addListener((message,sender,reply)=>{
  (async()=>{
    if(message?.kind === "config") {
      if (sender.tab && !bankTab(sender)) throw Error("Недопустимая вкладка");
      return {profile_url:(await selectedProfile()).href};
    }
    if(message?.kind === "open_login") {
      if (sender.tab) throw Error("Открывается только из окна расширения");
      const profile=await selectedProfile();
      const tab=await chrome.tabs.create({url:loginUrl(profile),active:true});
      monitored.add(tab.id);
      return {opened:true};
    }
    if(message?.kind === "report") {
      if (!bankTab(sender)) throw Error("Отчёт принимается только со страницы Пульса");
      const profile=await selectedProfile();
      const opened = new URL(sender.tab.url);
      const selectedName=decodeURIComponent(profile.pathname.split("/").filter(Boolean).pop()).toLowerCase();
      const openedName=decodeURIComponent(opened.pathname.split("/").filter(Boolean).at(-2) || opened.pathname.split("/").filter(Boolean).pop()).toLowerCase();
      if (selectedName !== openedName) return {ignored:true};
      monitored.add(sender.tab.id);
      if (!["authenticated","login_required","unknown"].includes(message.status))
        throw Error("Недопустимый статус авторизации");
      return await send({kind:"auth_status",status:message.status});
    }
    if(message?.kind === "stop") {
      if (sender.tab) throw Error("Остановить можно только из настроек");
      return await send({kind:"auth_status",status:"stopped"});
    }
    throw Error("Неизвестное действие");
  })().then(data=>reply({ok:true,data}),error=>{
    chrome.storage.local.set({last_error:String(error.message||error).slice(0,200)}).catch(()=>{});
    reply({ok:false,error:String(error.message||error).slice(0,200)});
  });
  return true;
});
// Explicit bank sign-in redirect: this is more reliable than guessing from
// a profile's available trades. No bank page data is read.
chrome.tabs.onUpdated.addListener((tabId,change,tab)=>{
  if (!monitored.has(tabId) || !change.url) return;
  try {
    const url=new URL(change.url);
    if ((/^id\.(?:tbank\.ru|tbank-online\.com)$/.test(url.hostname) ||
         BANKS.has(url.hostname) && /^\/auth(?:\/|$)/.test(url.pathname))) {
      send({kind:"auth_status",status:"login_required"}).catch(()=>{});
    }
  } catch { /* Not an URL. */ }
});
chrome.tabs.onRemoved.addListener(id=>monitored.delete(id));
