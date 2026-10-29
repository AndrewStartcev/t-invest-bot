/* Extension bridge: the bank session stays in Brave. Only validated
 * operation metadata and the selected author's profile reach the panel. */
"use strict";
const BACKENDS=new Set(["https://invest.argokov.ru","http://127.0.0.1:8765","http://localhost:8765"]);
const BANKS=new Set(["www.tbank.ru","tbank.ru","www.tbank-online.com","tbank-online.com"]);
const captures=new Map();
const safeHeaders=new Map();
const selectedTabs=new Set();

function bankTab(sender){
  try{
    const u=new URL(sender.tab?.url);
    return Number.isInteger(sender.tab?.id)&&u.protocol==="https:"&&BANKS.has(u.hostname)&&
      /^\/invest\/(?:social|pulse)\/profile\//.test(u.pathname);
  }catch{return false;}
}
async function send(report){
  const cfg=await chrome.storage.local.get(["endpoint","client","key","enabled"]);
  const origin=String(cfg.endpoint||"").replace(/\/$/,"");
  if(!BACKENDS.has(origin))throw Error("Допустим только адрес админки T-Invest Bot");
  if(!/^[a-z][a-z0-9_-]{2,31}$/.test(cfg.client||""))throw Error("Неверный логин кабинета");
  if(typeof cfg.key!=="string"||cfg.key.length<32)throw Error("Укажи ключ подключения");
  if(!cfg.enabled&&report.kind!=="config"&&report.status!=="stopped")throw Error("Расширение выключено");
  const response=await fetch(origin+"/api/extension/report",{
    method:"POST",cache:"no-store",signal:AbortSignal.timeout(20000),
    headers:{"Content-Type":"application/json","X-TInvest-Extension-Client":cfg.client,
             "X-TInvest-Extension-Key":cfg.key},
    body:JSON.stringify(report)
  });
  const data=await response.json().catch(()=>({}));
  if(!response.ok||!data.ok)throw Error(data.error||"HTTP "+response.status);
  if(report.kind!=="config")await chrome.storage.local.set({
    last_status:report.kind==="trades"?"ok":report.status,
    last_sent_at:new Date().toISOString(),last_error:""});
  else await chrome.storage.local.set({last_error:""});
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
  const pathname=new URL(details.url).pathname;
  if(!/\/social\/v1\/profile\/[^/]+\/instrument\/?$/.test(pathname))return;
  const headers={};
  for(const entry of details.requestHeaders||[])
    if(["x-app-name","x-app-version","x-platform"].includes(entry.name.toLowerCase()))
      headers[entry.name]=entry.value;
  safeHeaders.set(details.tabId,headers);
  await chrome.storage.session.set({["safe_headers_"+details.tabId]:headers});
  chrome.tabs.sendMessage(details.tabId,{kind:"safe_headers",headers}).catch(()=>{});
},{urls:["https://*.tbank.ru/mybank/api/*","https://*.tbank-online.com/mybank/api/*"]},
["requestHeaders","extraHeaders"]);

chrome.webRequest.onCompleted.addListener(async details=>{
  if(details.tabId<0)return;
  const u=new URL(details.url);
  if(!BANKS.has(u.hostname))return;
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
},{urls:["https://*.tbank.ru/mybank/api/*","https://*.tbank-online.com/mybank/api/*"]});

chrome.tabs.onRemoved.addListener(id=>{
  captures.delete(id);safeHeaders.delete(id);selectedTabs.delete(id);
  chrome.storage.session.remove(["nickname_tab_"+id,"instrument_tab_"+id,"safe_headers_"+id]).catch(()=>{});
});
chrome.tabs.onUpdated.addListener((id,change)=>{
  if(!selectedTabs.has(id)||!change.url)return;
  try{
    const u=new URL(change.url);
    if(/^id\.(?:tbank\.ru|tbank-online\.com)$/.test(u.hostname)||
      BANKS.has(u.hostname)&&/^\/auth(?:\/|$)/.test(u.pathname))
      send({kind:"auth_status",status:"login_required"}).catch(()=>{});
  }catch{}
});

chrome.runtime.onMessage.addListener((message,sender,reply)=>{
  (async()=>{
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
    if(message?.kind==="report"){
      if(!bankTab(sender))throw Error("Данные принимаются только из вкладки Пульса");
      if(!message.report||!["trades","auth_status"].includes(message.report.kind))
        throw Error("Неизвестный формат отчёта");
      return send(message.report);
    }
    if(message?.kind==="open_login"){
      if(sender.tab)throw Error("Открой через значок расширения");
      const profile=checkedProfile((await send({kind:"config"})).profile_url);
      const tab=await chrome.tabs.create({url:pageUrl(profile),active:true});
      selectedTabs.add(tab.id);
      return {opened:true};
    }
    if(message?.kind==="stop"){
      if(sender.tab)throw Error("Остановить можно только в настройках");
      return send({kind:"auth_status",status:"stopped"});
    }
    throw Error("Неизвестное действие расширения");
  })().then(data=>reply({ok:true,data}),error=>{
    chrome.storage.local.set({last_error:String(error?.message||error).slice(0,200)}).catch(()=>{});
    reply({ok:false,error:String(error?.message||error).slice(0,200)});
  });
  return true;
});
