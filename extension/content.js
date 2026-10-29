/* Runs ONLY in a real authenticated Pulse tab. No script is injected into
 * the banking page and no authentication data is read or transferred.
 * Best-effort 3-5s cycle requires an open tab; hidden tabs may be throttled.
 */
"use strict";
(() => {
  const INSTRUMENT_PATH = /\/social\/v1\/profile\/[^/]+\/instrument\/?$/;
  const VALID_HOSTS = new Set(["www.tbank.ru", "tbank.ru", "www.tbank-online.com", "tbank-online.com"]);
  const MAX_ITEMS = 6000;
  const MAX_DELTA = 500;
  let instrumentUrl = null, nicknameUrl = null, busy = false, lastProfileCheck = 0, lastIdentityCheck = 0, previousStart = 0;
  let consecutiveFailures = 0;
  let safeHeaders = {};
  let lastAuthFailure=0;
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
    for (let pageNum=0; pageNum<240; pageNum++) {
      const page = await api(cursorUrl(instrumentUrl,cursor,"nextCursor"));
      for (const item of page.items) {
        const ticker = item?.ticker, code = item?.classCode;
        const count = item?.statistics?.totalOperationsCount;
        if (typeof ticker !== "string" || typeof code !== "string" || !Number.isSafeInteger(count) || count < 0)
          throw Error("Неожиданный формат инструмента/счётчика");
        result.push({ticker, classCode:code, totalOperationsCount:count,
          showName:String(item.showName||ticker).slice(0,160),
          type:String(item.type||"").slice(0,32),
          maxTradeDateTime:String(item.statistics?.maxTradeDateTime||"").slice(0,40)});
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
    for (let pageNum=0; pageNum<100 && result.length<required; pageNum++) {
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
    if (busy) return 5000;
    busy = true;
    const cfg = await chrome.storage.local.get(["enabled", "interval"]);
    const interval = cfg.interval === 3 ? 3000 : 5000;
    let profileUrl = null;
    try {
      if (!cfg.enabled || !operationsTab()) return interval;
      const config=await request({kind:"config"});
      profileUrl=config.profile_url;
      if(selectedNickname(profileUrl)!==selectedNickname(location.href)) return interval;
      if(!instrumentUrl||!nicknameUrl){
        const observed=await request({kind:"get_instrument_url"});
        instrumentUrl=observed.instrument_url;
        nicknameUrl=observed.nickname_url;
        safeHeaders=observed.headers||{};
      }
      if(!instrumentUrl||!nicknameUrl){
        if(Date.now()-mountedAt>20000)
          throw Error("Пульс не отправил API запросы. Открой Сделки автора и нажми F5.");
        return interval;
      }
      await verifyAuthor();
      await profileCheck();
      const list=await instruments();
      const saved=config.counts||{};
      const changed=[];
      let total=0;
      for(const item of list){
        const key=item.ticker+":"+item.classCode;
        const old=saved[key];
        if(Number.isSafeInteger(old)&&item.totalOperationsCount<old)
          throw Error("Счётчик "+key+" уменьшился; история требует сверки");
        if(Number.isSafeInteger(old)&&item.totalOperationsCount>old){
          const delta=item.totalOperationsCount-old;
          total+=delta;
          if(total>MAX_DELTA)throw Error("Более 500 новых операций; не обновляем точку отсчёта");
          changed.push(...(await history(item.ticker,item.classCode,delta)).reverse());
        }
      }
      const ack=await request({kind:"report",report:{
        kind:"trades",status:"ok",profile_url:profileUrl,
        instruments:list,events:changed
      }});
      if(!ack?.ok)throw Error("Сервер не подтвердил сохранение");
      consecutiveFailures=0;
      return interval;
    }catch(error){
      consecutiveFailures++;
      await chrome.storage.local.set({last_error:String(error.message||error).slice(0,200)});
      if(profileUrl&&selectedNickname(profileUrl)===selectedNickname(location.href)){
        const status=errorStatus(error);
        try{
          await request({kind:"report",report:{kind:"trades",status:status==="login_required"?"login_required":"unavailable",
            profile_url:profileUrl,error:String(error.message||error).slice(0,180)}});
          if(status==="login_required"&&Date.now()-lastAuthFailure>30000){
            lastAuthFailure=Date.now();
            await request({kind:"report",report:{kind:"auth_status",status:"login_required"}});
          }
        }catch{}
      }
      return consecutiveFailures>=3?15000:interval;
    }finally{busy=false;}
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
