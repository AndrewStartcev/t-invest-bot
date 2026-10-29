/* MV3 bridge: requests go from the bank tab to the isolated backend.
 * Bank cookies, page request headers and broker tokens never cross this bridge.
 * Chrome may suspend the service worker; the tab's content script owns polling.
 */
"use strict";
const BACKEND_ORIGINS = new Set(["https://invest.argokov.ru", "http://127.0.0.1:8765", "http://localhost:8765"]);
const PULSE_HOSTS = new Set(["www.tbank.ru", "tbank.ru", "www.tbank-online.com", "tbank-online.com"]);
const observed = new Map();

function bankTab(sender) {
  if (!sender.tab?.id || !sender.tab.url) return false;
  try {
    const url = new URL(sender.tab.url);
    return url.protocol === "https:" && PULSE_HOSTS.has(url.hostname) &&
      /^\/invest\/(?:social|pulse)\/profile\//.test(url.pathname);
  } catch { return false; }
}

async function sendReport(report) {
  const config = await chrome.storage.local.get(["endpoint", "client", "key", "profile_url", "enabled"]);
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
  await chrome.storage.local.set({
    last_status: report.status === "test" ? "test_ok" : report.status,
    last_sent_at: new Date().toISOString(),
    last_error: ""
  });
  return result;
}

chrome.webRequest.onCompleted.addListener(async details => {
  if (details.tabId < 0) return;
  let url;
  try { url = new URL(details.url); } catch { return; }
  if (!/\/social\/v1\/profile\/[^/]+\/instrument\/?$/.test(url.pathname)) return;
  if (!PULSE_HOSTS.has(url.hostname)) return;
  observed.set(details.tabId, details.url);
  await chrome.storage.session.set({["instrument_tab_" + details.tabId]: details.url});
  chrome.tabs.sendMessage(details.tabId, {kind: "instrument_url", url: details.url}).catch(() => {});
}, {urls: ["https://*.tbank.ru/mybank/api/*", "https://*.tbank-online.com/mybank/api/*"]});

chrome.tabs.onRemoved.addListener(tabId => {
  observed.delete(tabId);
  chrome.storage.session.remove("instrument_tab_" + tabId).catch(() => {});
});

chrome.runtime.onMessage.addListener((message, sender, reply) => {
  (async () => {
    if (message?.kind === "get_instrument_url") {
      if (!bankTab(sender)) throw Error("Нужна вкладка с опубликованными сделками Пульса");
      const key = "instrument_tab_" + sender.tab.id;
      const saved = await chrome.storage.session.get(key);
      return {url: observed.get(sender.tab.id) || saved[key] || null};
    }
    if (message?.kind === "report") {
      if (!bankTab(sender)) throw Error("Запрос разрешён только со страницы Пульса");
      return await sendReport(message.report);
    }
    if (message?.kind === "test") {
      if (sender.tab) throw Error("Тест запускается из настроек расширения");
      return await sendReport({status:"test"});
    }
    throw Error("Неизвестная команда расширения");
  })().then(data => reply({ok:true,data}), error => {
    chrome.storage.local.set({last_error: String(error?.message || error).slice(0,250)}).catch(() => {});
    reply({ok:false,error: String(error?.message || error).slice(0,250)});
  });
  return true;
});
