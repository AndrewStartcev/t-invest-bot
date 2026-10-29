/* Runs ONLY in a real authenticated Pulse tab. No script is injected into
 * the banking page and no authentication data is read or transferred.
 * Best-effort 3-5s cycle requires an open tab; hidden tabs may be throttled.
 */
"use strict";
(() => {
  const INSTRUMENT_PATH = /\/social\/v1\/profile\/[^/]+\/instrument\/?$/;
  const VALID_HOSTS = new Set(["www.tbank.ru", "tbank.ru", "www.tbank-online.com", "tbank-online.com"]);
  const MAX_ITEMS = 500;
  const MAX_DELTA = 100;
  let instrumentUrl = null, nicknameUrl = null, busy = false, lastProfileCheck = 0, lastIdentityCheck = 0, previousStart = 0;
  let consecutiveFailures = 0;
  let safeHeaders = {};
  let activeProfileUrl = null;
  const mountedAt = Date.now();

  function selectedNickname(urlString) {
    try {
      const u = new URL(urlString);
      const m = /^\/invest\/(?:social|pulse)\/profile\/([^/]+)\/?/.exec(u.pathname);
      return m ? decodeURIComponent(m[1]).toLowerCase() : null;
    } catch { return null; }
  }

  function operationsTab() {
    return /^\/invest\/(?:social|pulse)\/profile\/[^/]+\/operations\/?$/.test(location.pathname);
  }

  function wait(ms) { return new Promise(resolve => setTimeout(resolve, ms)); }
  function request(message) {
    return new Promise((resolve, reject) => {
      chrome.runtime.sendMessage(message, response => {
        if (chrome.runtime.lastError) return reject(Error(chrome.runtime.lastError.message));
        if (!response?.ok) return reject(Error(response?.error || "Нет ответа расширения"));
        resolve(response.data);
      });
    });
  }

  function errorStatus(error) {
    if (error.status === 401 || /авторизац|вход|login/i.test(error.message)) return "login_required";
    if (error.status === 403 || error.status === 404) return "restricted";
    if (error.status === 429) return "rate_limited";
    return "unavailable";
  }

  async function api(url, timeoutMs = 9000) {
    const target = new URL(url);
    if (!VALID_HOSTS.has(target.hostname) || target.origin !== location.origin)
      throw Error("Внутренний запрос не совпадает с доменом открытой вкладки");
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(target.href, {
        credentials: "include", cache: "no-store", signal: controller.signal,
        headers: safeHeaders
      });
      if (!response.ok) {
        const error = Error("API Пульса: HTTP " + response.status);
        error.status = response.status;
        throw error;
      }
      if (new URL(response.url).hostname.startsWith("id.")) throw Error("Требуется вход в банк");
      const json = await response.json();
      const payload = json?.payload;
      if (!payload || !Array.isArray(payload.items)) throw Error("Формат API Пульса изменился");
      return payload;
    } finally { clearTimeout(timer); }
  }

  function cursorUrl(base, cursor, param) {
    const url = new URL(base);
    if (cursor !== null) url.searchParams.set(param, String(cursor));
    return url.href;
  }

  async function instruments() {
    let cursor = null;
    const seen = new Set();
    const result = [];
    for (let pageNum=0; pageNum<25; pageNum++) {
      const page = await api(cursorUrl(instrumentUrl,cursor,"nextCursor"));
      for (const item of page.items) {
        const ticker = item?.ticker, code = item?.classCode;
        const count = item?.statistics?.totalOperationsCount;
        if (typeof ticker !== "string" || typeof code !== "string" || !Number.isSafeInteger(count) || count < 0)
          throw Error("Неожиданный формат инструмента/счётчика");
        result.push({ticker, classCode:code, totalOperationsCount:count});
      }
      if (result.length > MAX_ITEMS) throw Error("Превышен лимит инструментов; проверка неполная");
      if (!page.hasNext) return result;
      cursor = page.nextCursor;
      if ((typeof cursor !== "number" && typeof cursor !== "string") || seen.has(String(cursor)))
        throw Error("Повторяющийся курсор списка");
      seen.add(String(cursor));
    }
    throw Error("Слишком много страниц списка; проверка неполная");
  }

  async function history(ticker, code, required) {
    const base = new URL(instrumentUrl);
    base.pathname = base.pathname.replace(/\/instrument\/?$/, "/operation/instrument/" +
      encodeURIComponent(ticker) + "/" + encodeURIComponent(code));
    base.search = "";
    let cursor = null, result = [];
    const seen = new Set();
    for (let pageNum=0; pageNum<25 && result.length<required; pageNum++) {
      const page = await api(cursorUrl(base.href,cursor,"cursor"));
      result = result.concat(page.items);
      if (result.length >= required) return result.slice(0,required).map(item => ({
        ticker, classCode:code, action:item.action, tradeDateTime:item.tradeDateTime,
        averagePrice:item.averagePrice
      }));
      if (!page.hasNext) break;
      cursor = page.nextCursor;
      if ((typeof cursor !== "number" && typeof cursor !== "string") || seen.has(String(cursor)))
        throw Error("Повторяющийся курсор истории");
      seen.add(String(cursor));
    }
    throw Error("История " + ticker + " неполная; счётчики сохранены без изменения");
  }

  async function verifyAuthor() {
    if (Date.now()-lastIdentityCheck < 90000) return;
    if (!nicknameUrl || !instrumentUrl) throw Error("Не обнаружен запрос подтверждения автора; обнови Сделки F5");
    const nickname = new URL(nicknameUrl);
    if (nickname.origin !== location.origin ||
        decodeURIComponent(nickname.pathname.split("/").pop()).toLowerCase() !== selectedNickname(location.href)) {
      throw Error("Нельзя подтвердить соответствие страницы выбранному автору");
    }
    const uuid = new URL(instrumentUrl).pathname.match(/\/profile\/([^/]+)\/instrument/);
    if (!uuid) throw Error("Неподтверждённый адрес списка инструментов");
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(),9000);
    try {
      const response = await fetch(nickname.href, {
        credentials:"include",cache:"no-store",signal:controller.signal,headers:safeHeaders
      });
      if (!response.ok) {
        const error = Error("Автор Пульса: HTTP " + response.status);
        error.status = response.status;
        throw error;
      }
      const data = await response.json();
      if (typeof data?.payload?.id !== "string" || data.payload.id !== uuid[1])
        throw Error("UUID инструментов не принадлежит автору на странице; обнови её F5");
      lastIdentityCheck = Date.now();
    } finally { clearTimeout(timer); }
  }

  async function profileCheck() {
    if (Date.now()-lastProfileCheck < 90000) return;
    lastProfileCheck = Date.now();
    // Verify the page/auth session independently of fast instrument polling.
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 9000);
    try {
      const response = await fetch(location.href, {credentials:"include", cache:"no-store", signal:controller.signal});
      if (!response.ok) {
        const error = Error("Профиль: HTTP " + response.status);
        error.status = response.status;
        throw error;
      }
      if (new URL(response.url).hostname.startsWith("id.")) throw Error("Требуется вход в банк");
    } finally {clearTimeout(timer);}
  }

  async function poll(elapsed) {
    if (busy) return;
    busy = true;
    try {
      const cfg = await chrome.storage.local.get(["enabled", "baselines", "interval"]);
      const interval = cfg.interval === 3 ? 3000 : 5000;
      if (!cfg.enabled || !operationsTab()) return interval;
      const selected = await request({kind:"config"});
      activeProfileUrl = selected.profile_url;
      // The cabinet determines the author; never scan a different open bank tab.
      if (selectedNickname(activeProfileUrl) !== selectedNickname(location.href)) return interval;
      if (!instrumentUrl || !nicknameUrl) {
        const result = await request({kind:"get_instrument_url"});
        instrumentUrl = result.url;
        nicknameUrl = result.nickname_url;
        safeHeaders = result.headers || {};
      }
      if (!instrumentUrl || !nicknameUrl) {
        if (Date.now()-mountedAt > 20000)
          await request({kind:"report", report:{status:"unavailable",profile_url:activeProfileUrl,
            error:"Не найден запрос списка инструментов. Открой Сделки и обнови вкладку F5."}});
        return interval;
      }
      if (document.hidden && elapsed > 15000) {
        await request({kind:"report",report:{status:"background_throttled",profile_url:activeProfileUrl,
          error:"Вкладка находится в фоне; браузер замедляет интервалы."}});
      }
      await verifyAuthor();
      await profileCheck();
      const list = await instruments();
      const baselines = cfg.baselines || {};
      const profileId = new URL(instrumentUrl).pathname.match(/\/profile\/([^/]+)\/instrument/)[1];
      const saved = selected.reset_baseline ? {} : (baselines[profileId] || {});
      const next = {...saved};
      let totalDelta = 0;
      const changed = [];
      for (const item of list) {
        const id = item.ticker + ":" + item.classCode;
        const old = saved[id], value = item.totalOperationsCount;
        if (Number.isInteger(old) && value < old) throw Error("Счётчик " + id + " уменьшился; нужна сверка");
        if (Number.isInteger(old) && value > old) {
          totalDelta += value-old;
          if (totalDelta > MAX_DELTA) throw Error("Слишком много новых операций; не меняем точку отсчёта");
          // Keep oldest-to-newest order for stable presentation.
          changed.push(...(await history(item.ticker,item.classCode,value-old)).reverse());
        }
        next[id]=value;
      }
      const result = await request({kind:"report",report:{
        status:"ok",profile_url:activeProfileUrl,instruments:list,events:changed}});
      if (!result?.ok) throw Error("Сервер не подтвердил сохранение");
      await chrome.storage.local.set({baselines:{...baselines,[profileId]:next}});
      consecutiveFailures = 0;
      return interval;
    } catch (error) {
      consecutiveFailures++;
      const cfg = await chrome.storage.local.get("enabled");
      if (cfg.enabled && activeProfileUrl && selectedNickname(activeProfileUrl) === selectedNickname(location.href)) {
        try { await request({kind:"report",report:{
          status:errorStatus(error),profile_url:activeProfileUrl,error:String(error.message).slice(0,180)}}); }
        catch { /* The server will alert on the missing heartbeat. */ }
      }
      return Math.min(60000, consecutiveFailures >= 3 ? 30000 : 5000);
    } finally {busy = false;}
  }

  async function run() {
    const elapsed = Date.now() - previousStart;
    previousStart = Date.now();
    const delay = await poll(elapsed);
    setTimeout(run,delay || 5000);
  }

  chrome.runtime.onMessage.addListener(message => {
    if (message?.kind === "safe_headers") {
      safeHeaders = Object.fromEntries(Object.entries(message.headers || {}).filter(
        ([key]) => ["x-app-name","x-app-version","x-platform"].includes(key.toLowerCase())));
    }
    if (message?.kind === "instrument_url") {
      try {
        const parsed = new URL(message.url);
        if (parsed.origin === location.origin && INSTRUMENT_PATH.test(parsed.pathname)) {
          instrumentUrl=parsed.href;
          lastIdentityCheck=0;
        }
      } catch { /* Not a Pulse URL. */ }
    }
    if (message?.kind === "nickname_url") {
      try {
        const parsed = new URL(message.url);
        const match=/\/social\/v1\/profile\/nickname\/([^/]+)\/?$/.exec(parsed.pathname);
        if (parsed.origin === location.origin && match &&
            decodeURIComponent(match[1]).toLowerCase() === selectedNickname(location.href)) {
          nicknameUrl=parsed.href;
          lastIdentityCheck=0;
        }
      } catch { /* Not a Pulse URL. */ }
    }
  });
  run();
})();
