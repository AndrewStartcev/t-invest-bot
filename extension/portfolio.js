/* Read published author allocations from the user's visible Pulse portfolio.
 * Nothing from the bank is posted until the author, section, and rows match.
 * No cookies, tokens, raw page HTML, or arbitrary network request headers.
 */
"use strict";
(() => {
  const visible = node => Boolean(node?.getClientRects().length) &&
    getComputedStyle(node).visibility !== "hidden";
  let lastSignature = "", lastReport = 0, busy = false;
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
      const screen=extract();
      if(!screen)return;
      const config=await chrome.runtime.sendMessage({kind:"config"});
      if(!config?.ok) return;
      const selected=new URL(config.data.profile_url).pathname.split("/").filter(Boolean).pop();
      const opened=location.pathname.match(/\/profile\/([^/]+)/)?.[1];
      if(!selected||!opened||decodeURIComponent(selected).toLowerCase()!==
         decodeURIComponent(opened).toLowerCase())return;
      const signature=JSON.stringify(screen);
      if(signature===lastSignature&&Date.now()-lastReport<180000)return;
      const result=await chrome.runtime.sendMessage({kind:"portfolio",
        report:{kind:"portfolio",profile_url:config.data.profile_url,screen}});
      if(result?.ok){
        lastSignature=signature;
        lastReport=Date.now();
        await chrome.storage.local.set({portfolio_status:"Портфель: "+
          result.data.rows+" строк, "+result.data.positions+" позиций",
          portfolio_last_ok:new Date().toISOString(),portfolio_error:""});
      }else{
        await chrome.storage.local.set({portfolio_error:String(result?.error||
          "Не удалось передать портфель").slice(0,200)});
      }
    }catch(error){
      await chrome.storage.local.set({portfolio_error:String(error.message||error).slice(0,200)});
    }finally{busy=false;}
  }
  setTimeout(scan,2000);
  setInterval(scan,15000);
})();
