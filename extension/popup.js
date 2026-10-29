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
async function showRuntime(){
  const current=await chrome.storage.local.get([
    "enabled","scan_stage","scan_stage_at","last_success_at","last_instrument_count","last_error","month_history_error"
  ]);
  const time=current.last_success_at
    ? " · последний успешный отчёт: "+new Date(current.last_success_at).toLocaleTimeString("ru-RU")
    : "";
  el("scan-info").textContent=(current.scan_stage||"Первый снимок ещё не получен")+
    (Number.isInteger(current.last_instrument_count)
      ? " · "+current.last_instrument_count+" инструментов":"")+time;
  if(current.month_history_error && current.enabled) {
    el("scan-info").textContent += " · "+current.month_history_error;
  }
  if(!current.enabled) status("⚠️ Передача сделок выключена. Поставь галочку и сохрани настройки.",true);
  else if(current.last_error) status("Ошибка передачи сделок: "+current.last_error,true);
  else if(current.last_success_at) status("✅ Сделки передаются в админку. "+current.last_instrument_count+" инструментов.");
  else status("Кабинет подключён, но первый снимок сделок ещё не получен. Открой «Сделки» в Brave и обнови F5.");
}
async function settings(){
  const endpoint=el("endpoint").value.trim().replace(/\/$/,"");
  if(!ALLOWED.has(endpoint)) throw Error("Допускается только адрес T-Invest Bot");
  if(!el("config").reportValidity()) throw Error("Проверь поля подключения");
  const old=await chrome.storage.local.get("enabled");
  if(old.enabled && !el("enabled").checked) await request("stop");
  await chrome.storage.local.set({endpoint,client:el("client").value.trim(),
    key:el("key").value.trim(),interval:Number(el("interval").value),enabled:el("enabled").checked});
  await chrome.storage.local.remove(["profile_url","baselines"]);
  status("Настройки сохранены. Сделки поступают из Brave в админку.");
}
async function checkConnection(){
  const result=await request("config");
  const target=new URL(result.profile_url);
  el("selected-author").textContent=decodeURIComponent(target.pathname.split("/").filter(Boolean).pop());
  return result;
}
el("config").addEventListener("submit",ev=>{
  ev.preventDefault();
  settings().then(checkConnection).then(showRuntime).catch(e=>status(e.message,true));
});
el("test").addEventListener("click",async()=>{
  try{await settings();await checkConnection();await showRuntime();}
  catch(e){status(e.message,true);}
});
el("open").addEventListener("click",async()=>{
  try{
    el("enabled").checked=true;
    await settings();
    await request("open_login");
    await showRuntime();
    status("Мониторинг включён. Войди в Пульс и обнови вкладку «Сделки» (F5).");
  }catch(e){status(e.message,true);}
});
(async()=>{
  const cfg=await chrome.storage.local.get(["endpoint","client","key","interval","enabled","last_status","last_sent_at","last_error"]);
  el("endpoint").value=cfg.endpoint||"https://invest.argokov.ru";
  el("client").value=cfg.client||"admin";
  el("key").value=cfg.key||"";
  el("enabled").checked=Boolean(cfg.enabled);
  el("interval").value=String(cfg.interval===3?3:5);
  if(cfg.last_error)status("Последняя ошибка: "+cfg.last_error,true);
  else if(cfg.last_sent_at)status("Последняя проверка: "+cfg.last_status+" · "+new Date(cfg.last_sent_at).toLocaleString("ru-RU"));
  if(cfg.key?.length>=32) {
    try {
      await checkConnection();
      await showRuntime();
    } catch(error) {
      status("Нет связи с админкой: " + error.message,true);
    }
  }else await showRuntime();
})().catch(e=>status(e.message,true));
