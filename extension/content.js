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
  let instrumentUrl = null, nicknameUrl = null, busy = false, lastIdentityCheck = 0, previousStart = 0;
  let consecutiveFailures = 0;
  let safeHeaders = {};
  const failedUrls = new Set();
  let lastAuthFailure=0;
  let contextKey = "", generation = 0;
  let diagnostic = {code:"starting", stage:"Запуск", step:"Подожди несколько секунд"};
  function failure(code, message, step) {
    return Object.assign(Error(message), {code, step});
  }
  function checkedApi(value, pattern) {
    const u = new URL(value);
    if(u.origin !== location.origin || u.protocol !== "https:" ||
       !VALID_HOSTS.has(u.hostname) || u.username || u.password ||
       !u.pathname.startsWith("/mybank/api/invest/") || !pattern.test(u.pathname))
      throw failure("api_address", "Неподтверждённый адрес API", "Открой сделки выбранного автора через расширение");
    return u;
  }

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
  async function request(message) {
    if(message.kind === "report" && contextKey) {
      const pairing = await chrome.storage.local.get(["enabled","endpoint","key"]);
      const expected=JSON.parse(contextKey);
      if(!pairing.enabled || location.href!==expected[0] || (pairing.endpoint??null)!==expected[2] || (pairing.key??null)!==expected[3])
        throw failure("context_changed", "Вкладка или подключение изменились", "Дождись следующей проверки");
    }
    return new Promise((resolve, reject) => {
      chrome.runtime.sendMessage(message, response => {
        if (chrome.runtime.lastError) return reject(Error(chrome.runtime.lastError.message));
        if (!response?.ok) return reject(Object.assign(Error(response?.error || "Нет ответа расширения"),
          {scope:response?.scope, status:response?.status, code:response?.code}));
        resolve(response.data);
      });
    });
  }

  function errorStatus(error) {
    // A wrong client pairing key is NOT a lost T-Bank session.
    if (error.status === 401 && error.scope === "pulse") return "login_required";
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
        credentials: "include", cache: "no-store", redirect:"error", signal: controller.signal,
        headers: safeHeaders
      });
      if (!response.ok) {
        const error = Error("API Пульса: HTTP " + response.status);
        error.status = response.status;
        error.scope = "pulse";
        throw error;
      }
      if (new URL(response.url).hostname.startsWith("id.")) throw Error("Требуется вход в банк");
      const json = await response.json().catch(() => {throw failure("api_format", "API Пульса вернул не JSON", "Проверь доступность Сделок в самой вкладке банка");});
      const payload = json?.payload;
      if (!payload || !Array.isArray(payload.items)) throw failure("api_format", "Формат API Пульса изменился", "Передай код диагностики разработчику");
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
    // Keep the bank's original sessionId and other required API query
    // parameters inside Brave; only the paging cursors are reset.
    base.searchParams.delete("nextCursor");
    base.searchParams.delete("cursor");
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

  const historyRetryAt = new Map();
  const HISTORY_HORIZON_MS = 24 * 60 * 60 * 1000;
  const HISTORY_LIMIT = 500;

  async function recentHistory(ticker, code) {
    const base = new URL(instrumentUrl);
    base.pathname = base.pathname.replace(/\/instrument\/?$/, "/operation/instrument/" +
      encodeURIComponent(ticker) + "/" + encodeURIComponent(code));
    // Keep the bank's original sessionId and other required API query
    // parameters inside Brave; only the paging cursors are reset.
    base.searchParams.delete("nextCursor");
    base.searchParams.delete("cursor");
    const found = [];
    const seen = new Set();
    const cutoff = Date.now() - HISTORY_HORIZON_MS;
    let cursor = null;
    let previousTradeTime = Infinity;
    for(let pageNum = 0; pageNum < 50; pageNum++) {
      const page = await api(cursorUrl(base.href, cursor, "cursor"));
      let reachedCutoff = false;
      for(const item of page.items){
        const timestamp = Date.parse(item?.tradeDateTime);
        if(!Number.isFinite(timestamp))
          throw Error("Некорректная дата истории: " + ticker);
        if(timestamp > previousTradeTime)
          throw Error("Пульс вернул историю не по убыванию даты: " + ticker);
        previousTradeTime = timestamp;
        if(timestamp < cutoff) {
          reachedCutoff = true;
          continue;
        }
        if(timestamp > Date.now() + 5 * 60 * 1000)
          throw Error("Дата сделки в будущем: " + ticker);
        found.push({
          ticker, classCode:code, action:item.action,
          tradeDateTime:item.tradeDateTime, averagePrice:item.averagePrice
        });
        if(found.length > HISTORY_LIMIT)
          throw Error("Более 500 сделок за 24 часа по " + ticker +
            "; история требует постраничной синхронизации");
      }
      if(reachedCutoff || !page.hasNext) return found;
      cursor = page.nextCursor;
      if ((typeof cursor !== "number" && typeof cursor !== "string") || seen.has(String(cursor)))
        throw Error("Повторный курсор истории " + ticker);
      seen.add(String(cursor));
    }
    throw Error("История за 24 часа не загрузилась полностью по " + ticker);
  }

  async function backfillHistory(profileUrl, instruments, config) {
    // Do one instrument per poll. The main counter report is already
    // acknowledged and future live trades are never blocked by backfill.
    const done = new Set(config.history_done || []);
    const cutoff = Date.now() - HISTORY_HORIZON_MS;
    const pending = instruments.filter(item => {
      if (item.totalOperationsCount <= 0) return false;
      const rawDate = String(item.maxTradeDateTime || "");
      // The server considers timestamps without a timezone unknown. Keep
      // candidate selection identical to avoid a permanently incomplete scan.
      const hasTimezone = /(?:Z|[+-]\d{2}:\d{2})$/i.test(rawDate);
      const last = hasTimezone ? Date.parse(rawDate) : NaN;
      if(Number.isFinite(last) && last < cutoff) return false;
      const key = item.ticker + ":" + item.classCode;
      return !done.has(key) && Date.now() >= (historyRetryAt.get(key) || 0);
    }).sort((a,b) => {
      const first = Date.parse(a.maxTradeDateTime || "") || 0;
      const second = Date.parse(b.maxTradeDateTime || "") || 0;
      return second - first;
    });
    if(!pending.length)return;
    const item = pending[0];
    const key = item.ticker + ":" + item.classCode;
    try {
      await setStage("История за 24 часа: "+item.ticker);
      const found = await recentHistory(item.ticker,item.classCode);
      await request({kind:"report",report:{
        kind:"history",profile_url:profileUrl,
        ticker:item.ticker,classCode:item.classCode,events:found
      }});
      historyRetryAt.delete(key);
      await setStage("История загружена: "+item.ticker+" · "+found.length+" операций");
    } catch(error) {
      // A corrupt/incomplete instrument must remain unacknowledged, without
      // preventing monitoring of other instruments. Retry it later.
      historyRetryAt.set(key,Date.now() + 60000);
      await chrome.storage.local.set({
        history_error:("История "+item.ticker+": "+String(error.message||error)).slice(0,200)
      });
    }
  }

  // Monthly backfill is independent of both fast counters and 24-hour history.
  // Browser-side cursors and sessionId never leave the user's browser.
  const MONTH_WINDOW_MS = 30 * 24 * 60 * 60 * 1000;
  const MONTH_BATCH_SIZE = 200;
  const monthRetryAt = new Map();
  let monthCurrentKey = null, monthCursor = null, monthBatch = 0;
  let monthLastTrade = Infinity;
  const monthSeenCursors = new Set();

  async function backfillMonth(profileUrl, instruments, config) {
    const complete = new Set(config.month_done || []);
    const offsets = config.month_offsets || {};
    const cutoff = Date.now() - MONTH_WINDOW_MS;
    const targets = instruments.filter(item => {
      if(item.totalOperationsCount <= 0) return false;
      const key = item.ticker + ":" + item.classCode;
      if(complete.has(key) || Date.now() < (monthRetryAt.get(key) || 0)) return false;
      const rawDate = String(item.maxTradeDateTime || "");
      const zoned = /(?:Z|[+-]\d{2}:\d{2})$/i.test(rawDate);
      const last = zoned ? Date.parse(rawDate) : NaN;
      return !Number.isFinite(last) || last >= cutoff;
    }).sort((a,b) => {
      const da = Date.parse(a.maxTradeDateTime || "") || 0;
      const db = Date.parse(b.maxTradeDateTime || "") || 0;
      return db - da;
    });
    if(!targets.length) return;
    const item = targets[0], key = item.ticker + ":" + item.classCode;
    if(monthCurrentKey !== key){
      monthCurrentKey = key;
      monthCursor = null;
      monthBatch = 0;
      monthLastTrade = Infinity;
      monthSeenCursors.clear();
    }
    try{
      const base = new URL(instrumentUrl);
      base.pathname = base.pathname.replace(/\/instrument\/?$/,
        "/operation/instrument/"+encodeURIComponent(item.ticker)+"/"+encodeURIComponent(item.classCode));
      base.searchParams.delete("nextCursor");
      base.searchParams.delete("cursor");
      await setStage("История за 30 дней: "+item.ticker+" · пакет "+(monthBatch+1));
      const page = await api(cursorUrl(base.href, monthCursor, "cursor"));
      const found = [];
      let reachedCutoff = false;
      for(const trade of page.items){
        const when = Date.parse(trade?.tradeDateTime);
        if(!Number.isFinite(when) || when > monthLastTrade)
          throw Error("Некорректная последовательность месячной истории: "+item.ticker);
        if(when > Date.now()+5*60*1000)
          throw Error("Время сделки в будущем: "+item.ticker);
        monthLastTrade = when;
        if(when < cutoff){
          reachedCutoff = true;
          continue;
        }
        found.push({ticker:item.ticker,classCode:item.classCode,
          action:trade.action,tradeDateTime:trade.tradeDateTime,
          averagePrice:trade.averagePrice});
      }
      const finished = reachedCutoff || !page.hasNext;
      if(!finished){
        if((typeof page.nextCursor !== "number" && typeof page.nextCursor !== "string") ||
            monthSeenCursors.has(String(page.nextCursor)))
          throw Error("Неверный курсор месячной истории "+item.ticker);
      }
      const batches = [];
      for(let offset=0;offset<found.length;offset+=MONTH_BATCH_SIZE)
        batches.push(found.slice(offset,offset+MONTH_BATCH_SIZE));
      if(!batches.length) batches.push([]);
      for(let i=0;i<batches.length;i++){
        const last = i === batches.length-1;
        await request({kind:"report",report:{
          kind:"month_history",profile_url:profileUrl,
          ticker:item.ticker,classCode:item.classCode,batch_index:monthBatch,
          complete:finished && last,events:batches[i]
        }});
        monthBatch++;
      }
      if(finished){
        monthCurrentKey = null;
        monthCursor = null;
        monthBatch = 0;
        monthLastTrade = Infinity;
        monthSeenCursors.clear();
      }else{
        monthSeenCursors.add(String(page.nextCursor));
        monthCursor = page.nextCursor;
      }
      monthRetryAt.delete(key);
      await chrome.storage.local.set({month_history_error:""});
    }catch(error){
      // Never mark partial month history complete, or advance live counters.
      monthRetryAt.set(key,Date.now()+60000);
      monthCurrentKey=null;
      await chrome.storage.local.set({
        month_history_error:("История 30 дней "+item.ticker+": "+String(error.message||error)).slice(0,200)
      });
    }
  }

  async function verifyAuthor() {
    if (Date.now()-lastIdentityCheck < 90000) return;
    // Bootstrap from the existing same-origin API contract. No session storage,
    // cookie inspection or guessing of third-party API hosts is needed.
    let nickname = nicknameUrl
      ? checkedApi(nicknameUrl, /\/profile\/nickname\/[^/]+\/?$/)
      : new URL("/mybank/api/invest/social/v1/profile/nickname/" +
          encodeURIComponent(selectedNickname(location.href)), location.origin);
    if(!nicknameUrl && instrumentUrl) {
      // A missed nickname request can reuse the observed API query locally.
      nickname.search = checkedApi(instrumentUrl, INSTRUMENT_PATH).search;
      nickname.searchParams.delete("nextCursor");
    }
    if(decodeURIComponent(nickname.pathname.replace(/\/$/, "").split("/").pop()).toLowerCase()
        !== selectedNickname(location.href)) {
      nicknameUrl = null; instrumentUrl = null;
      nickname = new URL("/mybank/api/invest/social/v1/profile/nickname/" +
        encodeURIComponent(selectedNickname(location.href)), location.origin);
    }
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(),9000);
    const page = location.href, epoch = generation;
    try {
      const response = await fetch(nickname.href, {
        credentials:"include",cache:"no-store",redirect:"error",signal:controller.signal,headers:safeHeaders
      });
      if (!response.ok) {
        throw Object.assign(Error("Автор Пульса: HTTP " + response.status),
          {status:response.status, scope:"pulse"});
      }
      const data = await response.json().catch(() => {throw failure("api_format", "Ответ автора не JSON", "Проверь, открывается ли профиль в банке");});
      const id = data?.payload?.id;
      if(typeof id !== "string" || !/^[a-zA-Z0-9-]{1,128}$/.test(id))
        throw failure("author_format", "API не подтвердил идентификатор автора", "Проверь доступность профиля; передай код разработчику");
      if(page !== location.href || epoch !== generation)
        throw failure("page_changed", "Страница изменилась во время проверки", "Дождись следующего цикла");
      if(instrumentUrl) {
        const captured = checkedApi(instrumentUrl, INSTRUMENT_PATH);
        const capturedId = captured.pathname.match(/\/profile\/([^/]+)\/instrument/)[1];
        if(capturedId !== id) instrumentUrl = null;
      }
      if(!instrumentUrl) {
        const target = new URL(nickname.href);
        target.pathname = target.pathname.replace(/\/nickname\/[^/]+\/?$/, "/"+encodeURIComponent(id)+"/instrument");
        target.searchParams.delete("nextCursor");
        instrumentUrl = target.href;
      }
      nicknameUrl = nickname.href;
      lastIdentityCheck = Date.now();
    } finally { clearTimeout(timer); }
  }

  async function setStage(stage, extra={}) {
    diagnostic = {...diagnostic, stage, at:new Date().toISOString()};
    await chrome.storage.local.set({scan_stage:stage,scan_stage_at:new Date().toISOString(),...extra});
  }

  async function poll(elapsed) {
    if (busy) return 5000;
    busy = true;
    const cfg = await chrome.storage.local.get(["enabled", "interval"]);
    const interval = cfg.interval === 3 ? 3000 : 5000;
    let profileUrl = null, pollPage = location.href, pollContext = "";
    function stillCurrent() {
      if(location.href !== pollPage || contextKey !== pollContext)
        throw failure("page_changed", "Страница или подключение изменились", "Дождись следующей проверки");
    }
    try {
      if (!cfg.enabled) {
        diagnostic.code="disabled"; diagnostic.step="Включи передачу сделок и сохрани настройки";
        await setStage("Мониторинг выключен");
        return interval;
      }
      if (!operationsTab()) {
        diagnostic.code="wrong_page"; diagnostic.step="Нажми «Открыть сделки автора»";
        await setStage("Ожидаем вкладку «Сделки» выбранного автора");
        return interval;
      }
      await setStage("Проверяем выбранного автора в админке");
      const config=await request({kind:"config"});
      profileUrl=config.profile_url;
      const pairing = await chrome.storage.local.get(["endpoint","key"]);
      pollContext = JSON.stringify([location.href,profileUrl,pairing.endpoint,pairing.key]);
      if(contextKey !== pollContext) {
        contextKey=pollContext; generation++; lastIdentityCheck=0;
        instrumentUrl=null; nicknameUrl=null; safeHeaders={};
        historyRetryAt.clear(); monthRetryAt.clear(); monthCurrentKey=null;
      }
      stillCurrent();
      if(selectedNickname(profileUrl)!==selectedNickname(location.href)) {
        diagnostic.code="wrong_author"; diagnostic.step="Открой автора, выбранного в твоём кабинете, кнопкой расширения";
        await setStage("Открыта вкладка другого автора");
        return interval;
      }
      if(!instrumentUrl||!nicknameUrl){
        const observed=await request({kind:"get_instrument_url"});
        instrumentUrl=failedUrls.has(observed.instrument_url)?null:observed.instrument_url;
        nicknameUrl=failedUrls.has(observed.nickname_url)?null:observed.nickname_url;
        safeHeaders=observed.headers||{};
      }
      await setStage("Проверяем, что API относится к выбранному автору");
      await verifyAuthor();
      stillCurrent();
      await setStage("Загружаем инструменты из Пульса");
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
      stillCurrent();
      const latest = await chrome.storage.local.get(["enabled","endpoint","key"]);
      if(!latest.enabled || JSON.stringify([location.href,profileUrl,latest.endpoint,latest.key]) !== pollContext)
        throw failure("pairing_changed", "Подключение изменилось во время запроса", "Дождись следующего цикла");
      // Recheck server-selected author before acknowledging a snapshot.
      if((await request({kind:"config"})).profile_url !== profileUrl)
        throw failure("author_changed", "В кабинете выбран другой автор", "Открой его сделки через расширение");
      stillCurrent();
      await setStage("Отправляем "+list.length+" инструментов в админку");
      const ack=await request({kind:"report",report:{
        kind:"trades",status:"ok",profile_url:profileUrl,
        instruments:list,events:changed
      }});
      if(!ack?.ok)throw Error("Сервер не подтвердил сохранение");
      await setStage("Данные получены · "+list.length+" инструментов",
                     {last_success_at:new Date().toISOString(),last_instrument_count:list.length,last_error:""});
      diagnostic.code="ok"; diagnostic.message=""; diagnostic.http=null; diagnostic.step="Держи вкладку сделок открытой";
      consecutiveFailures=0;
      await backfillHistory(profileUrl,list,config);
      await backfillMonth(profileUrl,list,config);
      return interval;
    }catch(error){
      consecutiveFailures++;
      const dashboard = error.scope === "dashboard";
      diagnostic.code = error.code || (dashboard ? "dashboard_" : "pulse_") +
        (error.status || (error.name === "AbortError" ? "timeout" : "network"));
      diagnostic.message = String(error.message||error).slice(0,200);
      diagnostic.http = error.status || null;
      diagnostic.step = error.step || (dashboard
        ? (error.status===401 || error.status===403 ? "Скопируй ключ из своего кабинета клиента и сохрани его" : "Проверь доступность админки и адрес подключения")
        : error.status===401 ? "Войди в свой аккаунт банка в этой вкладке; аккаунт автора не нужен"
        : error.status===403 || error.status===404 ? "Проверь, видны ли сделки автора в банке: доступ ограничен или API изменился"
        : error.status===429 ? "Банк ограничил частоту: подожди, повтор выполняется автоматически"
        : "Проверь открытие сделок в банке, сеть и сертификат. Сетевой сбой сам по себе не доказывает проблему сертификата");
      // An expired captured URL must not trap polling forever.
      if(!dashboard && [400,401,403,404].includes(error.status)) {
        if(instrumentUrl)failedUrls.add(instrumentUrl);
        if(nicknameUrl)failedUrls.add(nicknameUrl);
        if(failedUrls.size>32)failedUrls.delete(failedUrls.values().next().value);
        instrumentUrl=null; nicknameUrl=null; lastIdentityCheck=0;
      }
      await setStage("Ошибка загрузки данных",{last_error:String(error.message||error).slice(0,200)});
      if(!dashboard && location.href===pollPage && profileUrl&&selectedNickname(profileUrl)===selectedNickname(location.href)){
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

  chrome.runtime.onMessage.addListener((message, sender, reply) => {
    if(message?.kind === "diagnose") {
      reply({code:diagnostic.code, stage:diagnostic.stage, step:diagnostic.step,
        at:diagnostic.at, message:diagnostic.message||"", http:diagnostic.http || null,
        page:location.origin+location.pathname, author:selectedNickname(location.href),
        operations:operationsTab(), instrument:!!instrumentUrl, nickname:!!nicknameUrl,
        verified:!!lastIdentityCheck, busy});
      return false;
    }
    if (message?.kind === "safe_headers") {
      safeHeaders = Object.fromEntries(Object.entries(message.headers || {}).filter(
        ([key]) => ["x-app-name","x-app-version","x-platform"].includes(key.toLowerCase())));
    }
    if (message?.kind === "instrument_url") {
      if(busy)return false; // Keep the verified API identity fixed during a scan.
      try {
        const parsed = new URL(message.url);
        if (parsed.origin === location.origin && INSTRUMENT_PATH.test(parsed.pathname)) {
          instrumentUrl=parsed.href;
          lastIdentityCheck=0;
        }
      } catch { /* Not a Pulse URL. */ }
    }
    if (message?.kind === "nickname_url") {
      if(busy)return false;
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
