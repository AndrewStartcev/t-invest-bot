"use strict";
const el=id=>document.getElementById(id);
const ALLOWED=new Set(["https://invest.argokov.ru","http://127.0.0.1:8765","http://localhost:8765"]);
function status(msg,err=false){
  el("status").textContent=msg;
  el("status").style.background=err?"#fde9e8":"#e8f0fb";
  el("status").style.color=err?"#982b2b":"#264a78";
}
function request(kind){
  return new Promise((resolve,reject)=>chrome.runtime.sendMessage({kind},response=>{
    if(chrome.runtime.lastError) return reject(Error(chrome.runtime.lastError.message));
    if(!response?.ok) return reject(Error(response?.error||"Нет ответа"));
    resolve(response.data);
  }));
}
async function settings(){
  const endpoint=el("endpoint").value.trim().replace(/\/$/,"");
  if(!ALLOWED.has(endpoint)) throw Error("Допускается только адрес T-Invest Bot");
  if(!el("config").reportValidity()) throw Error("Проверь поля подключения");
  const old=await chrome.storage.local.get("enabled");
  if(old.enabled && !el("enabled").checked) await request("stop");
  await chrome.storage.local.set({endpoint,client:el("client").value.trim(),
    key:el("key").value.trim(),enabled:el("enabled").checked});
  await chrome.storage.local.remove(["profile_url","baselines","interval"]);
  status("Настройки сохранены. Контроль входа раз в 2 минуты.");
}
async function checkConnection(){
  const result=await request("config");
  const target=new URL(result.profile_url);
  el("selected-author").textContent=decodeURIComponent(target.pathname.split("/").filter(Boolean).pop());
  return result;
}
el("config").addEventListener("submit",ev=>{
  ev.preventDefault();settings().then(checkConnection).catch(e=>status(e.message,true));
});
el("test").addEventListener("click",async()=>{
  try{await settings();await checkConnection();status("✅ Сервер подключён. Авторизация проверяется во вкладке Пульса каждые 2 минуты.");}
  catch(e){status(e.message,true);}
});
el("open").addEventListener("click",async()=>{
  try{await settings();await request("open_login");status("Вкладка Пульса открыта. При необходимости войди в Т-Банк.");}
  catch(e){status(e.message,true);}
});
(async()=>{
  const cfg=await chrome.storage.local.get(["endpoint","client","key","enabled","last_status","last_sent_at","last_error"]);
  el("endpoint").value=cfg.endpoint||"https://invest.argokov.ru";
  el("client").value=cfg.client||"admin";
  el("key").value=cfg.key||"";
  el("enabled").checked=Boolean(cfg.enabled);
  if(cfg.last_error)status("Последняя ошибка: "+cfg.last_error,true);
  else if(cfg.last_sent_at)status("Последняя проверка: "+cfg.last_status+" · "+new Date(cfg.last_sent_at).toLocaleString("ru-RU"));
  if(cfg.key?.length>=32) {
    try {
      await checkConnection();
      status("✅ Кабинет подключён. Контроль авторизации каждые 2 минуты.");
    } catch(error) {
      status("Нет связи с админкой: " + error.message,true);
    }
  }
})().catch(e=>status(e.message,true));
