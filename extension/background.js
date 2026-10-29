/* MV3 bridge: requests go from the bank tab to the isolated backend.
 * Bank cookies, page request headers and broker tokens never cross this bridge.
 * Chrome may suspend the service worker; the tab's content script owns polling.
 */
"use strict";
const BACKEND_ORIGINS = new Set(["https://invest.argokov.ru", "http://127.0.0.1:8765", "http://localhost:8765"]);
const PULSE_HOSTS = new Set(["www.tbank.ru", "tbank.ru", "www.tbank-online.com", "tbank-online.com"]);
const observed = new Map();
const nicknames = new Map();
const safeHeaders = new Map();
let profileCache = null;
let profileDetails = null;
let profileCachedUntil = 0;

async function adminProfile(force = false) {
  // One paired handshake supplies the currently selected author for all tabs.
  // Keep a short cache to avoid hitting the backend on every 3-5s cycle.
  if (!force && profileCache && Date.now() < profileCachedUntil) return profileCache;
  const result = await sendReport({status:"test"});
  const value = result.profile_url;
  if (typeof value !== "string" ||
      !/^https:\/\/(?:www\.)?tbank(?:-online)?\.(?:ru|com)\/invest\/(?:social|pulse)\/profile\/[^/]+\/$/.test(value))
    throw Error("Сервер вернул некорректную ссылку автора");
  profileCache = value;
  profileDetails = {
    profile_url: value,
    reset_baseline: Boolean(result.reset_baseline),
    counts: result.counts && typeof result.counts === "object" ? result.counts : {}
  };
  profileCachedUntil = Date.now() + 10000;
  return value;
}

function bankTab(sender) {
  if (!sender.tab?.id || !sender.tab.url) return false;
  try {
    const url = new URL(sender.tab.url);
    return url.protocol === "https:" && PULSE_HOSTS.has(url.hostname) &&
      /^\/invest\/(?:social|pulse)\/profile\//.test(url.pathname);
  } catch { return false; }
}

async function sendReport(report) {
  const config = await chrome.storage.local.get(["endpoint", "client", "key", "enabled"]);
  const origin = String(config.endpoint || "").replace(/\/$/, "");
  if (!BACKEND_ORIGINS.has(origin)) throw Error("Разрешён только HTTPS invest.argokov.ru или локальная админка");
  if (!/^[a-z][a-z0-9_-]{2,31}$/.test(config.client || "")) throw Error("Неверный логин кабинета");
  if (typeof config.key !== "string" || config.key.length < 32) throw Error("Сначала укажи ключ подключения");
  if (!config.enabled && report.status !== "test" && report.status !== "stopped") {
    throw Error("Мониторинг выключен");
  }
  const response = await fetch(origin + "/api/extension/report", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-TInvest-Extension-Client": config.client,
      "X-TInvest-Extension-Key": config.key
    },
    body: JSON.stringify(report),
    cache: "no-store",
    signal: AbortSignal.timeout(15000)
  });
  const result = await response.json().catch(() => ({}));
  if (!response.ok || !result.ok) throw Error(result.error || "Сервер вернул HTTP " + response.status);
  if (report.status === "ok" && profileDetails && report.profile_url === profileDetails.profile_url) {
    profileDetails = {...profileDetails, reset_baseline: false,
      counts: Object.fromEntries((report.instruments || []).map(item =>
        [item.ticker + ":" + item.classCode, item.totalOperationsCount]))};
  }
  if (report.status !== "test") {
    await chrome.storage.local.set({
      last_status: report.status,
      last_sent_at: new Date().toISOString(),
      last_error: ""
    });
  }
  return result;
}

// Copy only non-secret application metadata used by the banking site.
const HEADER_ALLOWLIST = new Set(["x-app-name","x-app-version","x-platform"]);
chrome.webRequest.onBeforeSendHeaders.addListener(async details => {
  if (details.tabId < 0) return;
  const path = new URL(details.url).pathname;
  if (!/\/social\/v1\/profile\/[^/]+\/instrument\/?$/.test(path)) return;
  const headers = {};
  for (const entry of details.requestHeaders || []) {
    if (HEADER_ALLOWLIST.has(entry.name.toLowerCase()) && typeof entry.value === "string")
      headers[entry.name] = entry.value;
  }
  safeHeaders.set(details.tabId, headers);
  await chrome.storage.session.set({["headers_tab_" + details.tabId]:headers});
  chrome.tabs.sendMessage(details.tabId,{kind:"safe_headers",headers}).catch(()=>{});
}, {urls:["https://*.tbank.ru/mybank/api/*","https://*.tbank-online.com/mybank/api/*"]},
   ["requestHeaders","extraHeaders"]);

chrome.webRequest.onCompleted.addListener(async details => {
  if (details.tabId < 0) return;
  let url;
  try { url = new URL(details.url); } catch { return; }
  if (!PULSE_HOSTS.has(url.hostname)) return;
  if (/\/social\/v1\/profile\/nickname\/[^/]+\/?$/.test(url.pathname)) {
    nicknames.set(details.tabId, details.url);
    await chrome.storage.session.set({["nickname_tab_" + details.tabId]: details.url});
    chrome.tabs.sendMessage(details.tabId, {kind: "nickname_url", url: details.url}).catch(() => {});
    return;
  }
  if (!/\/social\/v1\/profile\/[^/]+\/instrument\/?$/.test(url.pathname)) return;
  // A paginated request may complete last; never start the next cycle mid-list.
  url.searchParams.delete("nextCursor");
  observed.set(details.tabId, url.href);
  await chrome.storage.session.set({["instrument_tab_" + details.tabId]: url.href});
  chrome.tabs.sendMessage(details.tabId, {kind: "instrument_url", url: url.href}).catch(() => {});
}, {urls: ["https://*.tbank.ru/mybank/api/*", "https://*.tbank-online.com/mybank/api/*"]});

chrome.tabs.onRemoved.addListener(tabId => {
  observed.delete(tabId);
  nicknames.delete(tabId);
  safeHeaders.delete(tabId);
  chrome.storage.session.remove(["instrument_tab_" + tabId, "nickname_tab_" + tabId,
                                 "headers_tab_" + tabId]).catch(() => {});
});

chrome.runtime.onMessage.addListener((message, sender, reply) => {
  (async () => {
    if (message?.kind === "get_instrument_url") {
      if (!bankTab(sender)) throw Error("Нужна вкладка с опубликованными сделками Пульса");
      const key = "instrument_tab_" + sender.tab.id;
      const nick = "nickname_tab_" + sender.tab.id;
      const headers = "headers_tab_" + sender.tab.id;
      const saved = await chrome.storage.session.get([key, nick, headers]);
      return {url: observed.get(sender.tab.id) || saved[key] || null,
              nickname_url: nicknames.get(sender.tab.id) || saved[nick] || null,
              headers: safeHeaders.get(sender.tab.id) || saved[headers] || {}};
    }
    if (message?.kind === "report") {
      if (!bankTab(sender)) throw Error("Запрос разрешён только со страницы Пульса");
      return await sendReport(message.report);
    }
    if (message?.kind === "config") {
      if (!bankTab(sender)) throw Error("Настройки автора доступны только со вкладки Пульса");
      await adminProfile();
      return {...profileDetails};
    }
    if (message?.kind === "test") {
      if (sender.tab) throw Error("Тест запускается из настроек расширения");
      const profile_url = await adminProfile(true);
      await chrome.storage.local.set({last_error:""});
      return {profile_url, read_only:true};
    }
    if (message?.kind === "open_profile") {
      if (sender.tab) throw Error("Открыть профиль можно только из настроек расширения");
      const url = new URL(await adminProfile(true));
      url.pathname = url.pathname.replace(
        /\/invest\/(?:social|pulse)\/profile\/([^/]+)\/?$/,
        "/invest/pulse/profile/$1/operations/");
      if (!PULSE_HOSTS.has(url.hostname) || url.protocol !== "https:")
        throw Error("Некорректная ссылка на профиль банка");
      await chrome.tabs.create({url:url.href,active:true});
      return {opened:true};
    }
    if (message?.kind === "stop") {
      if (sender.tab) throw Error("Остановить можно только из настроек расширения");
      return await sendReport({status:"stopped",profile_url:await adminProfile(true)});
    }
    throw Error("Неизвестная команда расширения");
  })().then(data => reply({ok:true,data}), error => {
    chrome.storage.local.set({last_error: String(error?.message || error).slice(0,250)}).catch(() => {});
    reply({ok:false,error: String(error?.message || error).slice(0,250)});
  });
  return true;
});
