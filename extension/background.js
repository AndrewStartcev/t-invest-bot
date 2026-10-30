/* Extension bridge: the bank session stays in Brave. Only validated
 * operation metadata and the selected author's profile reach the panel. */
"use strict";
const BACKENDS=new Set(["https://invest.argokov.ru","http://127.0.0.1:8765","http://localhost:8765"]);
const BANKS=new Set(["www.tbank.ru","tbank.ru","www.tbank-online.com","tbank-online.com"]);
const captures=new Map();
const safeHeaders=new Map();
const networkState=new Map();
const API_FILTER={urls:[...BANKS].map(host=>"https://"+host+"/mybank/api/*")};
function captureUrl(value) {
  try {
    const u=new URL(value);
    return u.protocol==="https:" && BANKS.has(u.hostname) && !u.username && !u.password &&
      u.pathname.startsWith("/mybank/api/invest/social/v1/profile/") ? u : null;
  } catch {return null;}
}
async function diagnostics() {
  const tabs=await chrome.tabs.query({});
  const results=[];
  for(const tab of tabs.filter(t=>bankTab({tab:t}))) {
    const u=new URL(tab.url);
    const allowed=await chrome.permissions.contains({origins:[u.origin+"/*"]});
    let state;
    if(allowed) {
      try {state=await chrome.tabs.sendMessage(tab.id,{kind:"diagnose"});} catch {}
    }
    results.push({tabId:tab.id, active:!!tab.active, page:u.origin+u.pathname, allowed,
      state:state||null, network:networkState.get(tab.id)||null});
  }
  return {version:chrome.runtime.getManifest().version,tabs:results};
}


function bankTab(sender){
  try{
    const u=new URL(sender.tab?.url);
    return Number.isInteger(sender.tab?.id)&&u.protocol==="https:"&&BANKS.has(u.hostname)&&
      /^\/invest\/(?:social|pulse)\/profile\//.test(u.pathname);
  }catch{return false;}
}
async function send(report){
  const cfg=await chrome.storage.local.get(["endpoint","key","enabled"]);
  const origin=String(cfg.endpoint||"").replace(/\/$/,"");
  if(!BACKENDS.has(origin))throw Object.assign(Error("Допустим только адрес админки T-Invest Bot"),{scope:"dashboard",code:"dashboard_address"});
  if(typeof cfg.key!=="string"||cfg.key.length<32)throw Object.assign(Error("Укажи ключ подключения"),{scope:"dashboard",code:"dashboard_pairing"});
  if(!cfg.enabled&&report.kind!=="config"&&report.status!=="stopped")throw Error("Расширение выключено");
  const response=await fetch(origin+"/api/extension/report",{
    method:"POST",cache:"no-store",signal:AbortSignal.timeout(20000),
    headers:{"Content-Type":"application/json",
             "X-TInvest-Extension-Key":cfg.key},
    body:JSON.stringify(report)
  }).catch(()=>{throw Object.assign(Error("Админка недоступна: сеть или тайм-аут"),
    {scope:"dashboard",code:"dashboard_network"});});
  const data=await response.json().catch(()=>({}));
  if(!response.ok||!data.ok){
    const error=Error(data.error||"Ошибка подключения кабинета · HTTP "+response.status);
    error.scope="dashboard";error.status=response.status;
    throw error;
  }
  if(report.kind==="trades") {
    const update={
      last_status:report.status,
      last_sent_at:new Date().toISOString(),
      last_error:report.status==="ok"?"":String(report.error||"Не удалось получить сделки").slice(0,200)
    };
    if(report.status==="ok"){
      update.last_instrument_count=report.instruments.length;
      update.last_success_at=update.last_sent_at;
    }
    await chrome.storage.local.set(update);
  } else if(report.kind==="auth_status") {
    await chrome.storage.local.set({
      last_auth_status:report.status,
      last_auth_sent_at:new Date().toISOString()
    });
  }
  return data;
}
function checkedProfile(value){
  const u=new URL(value);
  if(u.protocol!=="https:"||!BANKS.has(u.hostname)||
     !/^\/invest\/(?:social|pulse)\/profile\/[^/]+\/?$/.test(u.pathname))
    throw Error("Админка вернула некорректный профиль");
  return u;
}
function pageUrl(profile){
  const u=new URL(profile);
  u.pathname=u.pathname.replace(/\/invest\/(?:social|pulse)\/profile\/([^/]+)\/?$/,
                                "/invest/pulse/profile/$1/operations/");
  return u.href;
}
chrome.webRequest.onBeforeSendHeaders.addListener(async details=>{
  if(details.tabId<0)return;
  const target=captureUrl(details.url);
  if(!target)return;
  const pathname=target.pathname;
  if(!/\/social\/v1\/profile\/[^/]+\/instrument\/?$/.test(pathname))return;
  const headers={};
  for(const entry of details.requestHeaders||[])
    if(["x-app-name","x-app-version","x-platform"].includes(entry.name.toLowerCase()))
      headers[entry.name]=entry.value;
  safeHeaders.set(details.tabId,headers);
  await chrome.storage.session.set({["safe_headers_"+details.tabId]:headers});
  chrome.tabs.sendMessage(details.tabId,{kind:"safe_headers",headers}).catch(()=>{});
},API_FILTER,
["requestHeaders","extraHeaders"]);

chrome.webRequest.onCompleted.addListener(async details=>{
  if(details.tabId<0)return;
  const u=captureUrl(details.url);
  if(!u)return;
  networkState.set(details.tabId,{status:details.statusCode,at:new Date().toISOString()});
  if(details.statusCode<200||details.statusCode>=300)return;
  const id=details.tabId;
  const old=captures.get(id)||{};
  if(/\/social\/v1\/profile\/nickname\/[^/]+\/?$/.test(u.pathname)){
    captures.set(id,{...old,nickname_url:u.href});
    await chrome.storage.session.set({["nickname_tab_"+id]:u.href});
    chrome.tabs.sendMessage(id,{kind:"nickname_url",url:u.href}).catch(()=>{});
  }else if(/\/social\/v1\/profile\/[^/]+\/instrument\/?$/.test(u.pathname)){
    u.searchParams.delete("nextCursor");
    captures.set(id,{...old,instrument_url:u.href});
    await chrome.storage.session.set({["instrument_tab_"+id]:u.href});
    chrome.tabs.sendMessage(id,{kind:"instrument_url",url:u.href}).catch(()=>{});
  }
},API_FILTER);

chrome.webRequest.onErrorOccurred.addListener(details=>{
  if(details.tabId<0||!captureUrl(details.url))return;
  networkState.set(details.tabId,{error:String(details.error).slice(0,100),at:new Date().toISOString()});
},API_FILTER);
chrome.tabs.onUpdated.addListener((id,change)=>{
  if(!change.url)return;
  captures.delete(id);safeHeaders.delete(id);networkState.delete(id);
  chrome.storage.session.remove(["nickname_tab_"+id,"instrument_tab_"+id,"safe_headers_"+id]).catch(()=>{});
});
chrome.tabs.onRemoved.addListener(id=>{
  networkState.delete(id);
  captures.delete(id);safeHeaders.delete(id);
  chrome.storage.session.remove(["nickname_tab_"+id,"instrument_tab_"+id,"safe_headers_"+id]).catch(()=>{});
});

chrome.runtime.onMessage.addListener((message,sender,reply)=>{
  (async()=>{
    if(message?.kind==="diagnostics"){
      if(sender.tab)throw Error("Диагностика открывается через значок расширения");
      return diagnostics();
    }
    if(message?.kind==="config"){
      if(sender.tab&&!bankTab(sender))throw Error("Недопустимая страница");
      const data=await send({kind:"config"});
      checkedProfile(data.profile_url);
      return data;
    }
    if(message?.kind==="get_instrument_url"){
      if(!bankTab(sender))throw Error("Открой выбранного автора в Пульсе");
      const id=sender.tab.id;
      const saved=await chrome.storage.session.get([
        "nickname_tab_"+id,"instrument_tab_"+id,"safe_headers_"+id]);
      const cached=captures.get(id)||{};
      return {instrument_url:cached.instrument_url||saved["instrument_tab_"+id]||null,
        nickname_url:cached.nickname_url||saved["nickname_tab_"+id]||null,
        headers:safeHeaders.get(id)||saved["safe_headers_"+id]||{}};
    }
    if(message?.kind==="portfolio") {
      if(!bankTab(sender))throw Error("Портфель читается только из выбранного профиля Пульса");
      if(message.report?.kind!=="portfolio")throw Error("Неверный формат портфеля");
      const profile=checkedProfile((await send({kind:"config"})).profile_url);
      const opened=new URL(sender.tab.url).pathname.match(/\/profile\/([^/]+)/)?.[1];
      const selected=profile.pathname.split("/").filter(Boolean).pop();
      if(!opened||decodeURIComponent(opened).toLowerCase()!==decodeURIComponent(selected).toLowerCase())
        throw Error("Портфель другой учётной записи");
      return await send(message.report);
    }
    if(message?.kind==="open_portfolio"){
      if(sender.tab)throw Error("Открывается только из расширения");
      const profile=checkedProfile((await send({kind:"config"})).profile_url);
      await chrome.tabs.create({url:profile.href,active:true});
      return {opened:true};
    }
    if(message?.kind==="report"){
      if(!bankTab(sender))throw Error("Данные принимаются только из вкладки Пульса");
      if(!message.report||!["trades","history","month_history","auth_status"].includes(message.report.kind))
        throw Error("Неизвестный формат отчёта");
      return send(message.report);
    }
    if(message?.kind==="open_profile"||message?.kind==="open_login"){
      if(sender.tab)throw Error("Открой через значок расширения");
      const profile=checkedProfile((await send({kind:"config"})).profile_url);
      // Open the watched public author page, not a banking login.
      await chrome.tabs.create({url:pageUrl(profile),active:true});
      return {opened:true};
    }
    if(message?.kind==="stop"){
      if(sender.tab)throw Error("Остановить можно только в настройках");
      return send({kind:"auth_status",status:"stopped"});
    }
    throw Error("Неизвестное действие расширения");
  })().then(data=>reply({ok:true,data}),error=>{
    chrome.storage.local.set({last_error:String(error?.message||error).slice(0,200)}).catch(()=>{});
    reply({ok:false,error:String(error?.message||error).slice(0,200),
      scope:error.scope,status:error.status,code:error.code});
  });
  return true;
});
