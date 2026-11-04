/* Read published author allocations from the user's visible Pulse portfolio.
 * Nothing from the bank is posted until the author, section, and rows match.
 * No cookies, tokens, raw page HTML, or arbitrary network request headers.
 */
"use strict";
(() => {
  const visible = node => Boolean(node?.getClientRects().length) &&
    getComputedStyle(node).visibility !== "hidden";
  // A successful browser read stays fresh server-side for at most 180s.
  // Send another verified snapshot every 60s even if the weights are unchanged.
  const REFRESH_INTERVAL_MS = 60000;
  let lastSignature = "", lastReport = 0, lastFailure = "", busy = false;
  function extract(){
    const headings=[...document.querySelectorAll("h1,h2,h3,[role=heading]")].filter(visible);
    const named = headings.find(h=>/[.\s:—–-]+портфель/i.test(h.innerText.trim()));
    const heading = named || headings.find(h=>/^портфель$/i.test(h.innerText.trim()));
    if(!heading) return null;
    let panel=heading.parentElement;
    for(let n=0;panel&&n<6;n++,panel=panel.parentElement){
      const text=panel.innerText||"";
      if(/компании/i.test(text)&&/активы/i.test(text)&&/%/.test(text))break;
    }
    if(!panel||panel===document.body||panel===document.documentElement ||
       !/компании/i.test(panel.innerText||"") || !/активы/i.test(panel.innerText||"") ||
       [...panel.querySelectorAll("[role=tab],button")].some(n=>/^(Посты|Ролики)$/.test(n.innerText.trim())))
       return null;
    const rows=[],seen=new Set();
    for(const node of panel.querySelectorAll("*")){
      if(!visible(node)||node.children.length||!/%/.test(node.innerText||""))continue;
      let row=node.parentElement;
      for(let depth=0;row&&row!==panel&&depth<4;depth++,row=row.parentElement){
        const label=(row.innerText||"").trim();
        if(label.length>240||(label.match(/%/g)||[]).length!==1)break;
        if(/\p{L}/u.test(label)&&!seen.has(label)){
          seen.add(label);
          const links=[...row.querySelectorAll("a[href]")].slice(0,30).map(a=>{
            try{
              const u=new URL(a.href);
              return u.protocol==="https:"&&["tbank.ru","www.tbank.ru",
                "tbank-online.com","www.tbank-online.com"].includes(u.hostname)
                ? u.origin+u.pathname : "";
            }catch{return "";}
          }).filter(Boolean);
          rows.push({text:label,links});
          break;
        }
      }
      if(rows.length>3000)return null;
    }
    if(!rows.length)return null;
    const path=location.pathname;
    const match=path.match(/^\/invest\/(?:social|pulse)\/profile\/([^/]+)(?:\/portfolio|\/operations)?\/?$/);
    if(!match)return null;
    const nickname=decodeURIComponent(match[1]);
    const author=headings.find(node=>node!==heading&&
      node.innerText.trim().toLocaleLowerCase()===nickname.toLocaleLowerCase());
    return {url:location.origin+path,heading:heading.innerText.trim(),
      author:author?.innerText.trim()||"",portfolio:true,rows};
  }
  async function scan(){
    if(busy)return;
    busy=true;
    try{
      const cfg=await chrome.storage.local.get("enabled");
      if(!cfg.enabled)return;
      const match=location.pathname.match(
        /^\/invest\/(?:social|pulse)\/profile\/([^/]+)(?:\/portfolio|\/operations)?\/?$/);
      if(!match)return;
      const config=await chrome.runtime.sendMessage({kind:"config"});
      if(!config?.ok){
        await chrome.storage.local.set({portfolio_error:
          "Нет связи с кабинетом: "+String(config?.error||"неверный ключ").slice(0,140)});
        return;
      }
      const selected=decodeURIComponent(new URL(config.data.profile_url)
        .pathname.split("/").filter(Boolean).pop()).toLowerCase();
      const opened=decodeURIComponent(match[1]).toLowerCase();
      if(selected!==opened)return;
      const screen=extract();
      if(!screen){
        // Do not invalidate from an ordinary Pulse operations tab.
        // Only a visible portfolio/companies view that failed parsing can
        // actively invalidate the previous result.
        const active=[...document.querySelectorAll('[role="tab"][aria-selected="true"],button[aria-selected="true"]')]
          .some(node=>visible(node)&&/^Компании$/i.test((node.innerText||"").trim()));
        const heading=[...document.querySelectorAll('h1,h2,h3,[role="heading"]')]
          .some(node=>visible(node)&&/портфель/i.test(node.innerText||""));
        if(active&&heading&&lastFailure!=="unrecognized"){
          const response=await chrome.runtime.sendMessage({kind:"portfolio_status",
            report:{kind:"portfolio_status",profile_url:config.data.profile_url,
              reason:"unrecognized"}});
          if(response?.ok)lastFailure="unrecognized";
        }
        await chrome.storage.local.set({portfolio_status:
          active&&heading?"Портфель открыт, но позиции пока не распознаны":
          "Для обновления портфеля держи открытым раздел «Портфель → Компании»"});
        return;
      }
      const signature=JSON.stringify(screen);
      if(signature===lastSignature && Date.now()-lastReport<REFRESH_INTERVAL_MS)return;
      const result=await chrome.runtime.sendMessage({kind:"portfolio",
        report:{kind:"portfolio",profile_url:config.data.profile_url,screen}});
      if(result?.ok){
        lastSignature=signature;
        lastReport=Date.now();
        lastFailure="";
        await chrome.storage.local.set({portfolio_status:"Портфель обновлён: "+
          result.data.rows+" строк, "+result.data.positions+" подтверждённых позиций",
          portfolio_last_ok:new Date().toISOString(),portfolio_error:""});
      }else{
        await chrome.storage.local.set({portfolio_error:String(result?.error||
          "Не удалось передать портфель").slice(0,200)});
      }
    }catch(error){
      // A disconnected browser is not evidence of an empty portfolio.
      // The server's 180s freshness timeout will revoke cached positions.
      await chrome.storage.local.set({portfolio_error:String(error.message||error).slice(0,200)});
    }finally{busy=false;}
  }
  setTimeout(scan,2000);
  setInterval(scan,15000);
  document.addEventListener("visibilitychange",()=>{if(!document.hidden)scan();});
  window.addEventListener("pageshow",scan);
})();
