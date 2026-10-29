/* Best-effort two-minute session check using the current bank tab.
 * Does not enumerate other authors, instruments, or transactions.
 * Ambiguous results are "unknown" rather than a false logout warning. */
"use strict";
(()=>{
  const CHECK_MS=120000;
  const BANKS=new Set(["www.tbank.ru","tbank.ru","www.tbank-online.com","tbank-online.com"]);
  let running=false;
  function request(kind,status) {
    return new Promise((resolve,reject)=>{
      chrome.runtime.sendMessage({kind,status},response=>{
        if (chrome.runtime.lastError) return reject(Error(chrome.runtime.lastError.message));
        if (!response?.ok) return reject(Error(response?.error||"Нет ответа"));
        resolve(response.data);
      });
    });
  }
  function currentName() {
    const chunks=location.pathname.split("/").filter(Boolean);
    if(chunks.length<4 || chunks[0]!=="invest" || chunks[2]!=="profile") return null;
    return decodeURIComponent(chunks[3]).toLowerCase();
  }
  function isOperations() {
    const chunks=location.pathname.split("/").filter(Boolean);
    return chunks.length === 5 && chunks[4] === "operations" &&
      ["social","pulse"].includes(chunks[1]);
  }
  async function check() {
    if (running) return;
    running=true;
    try {
      const local=await chrome.storage.local.get("enabled");
      if (!local.enabled || !isOperations()) return;
      const config=await request("config");
      const target=new URL(config.profile_url);
      const nickname=decodeURIComponent(target.pathname.split("/").filter(Boolean).pop()).toLowerCase();
      if (nickname!==currentName()) return;
      let status="unknown";
      // Checking the *page* cannot prove login just because HTML returned 200.
      // A redirect to the bank's sign-in domain is conclusive.
      const controller=new AbortController();
      const timeout=setTimeout(()=>controller.abort(),10000);
      try {
        const response=await fetch(location.href,{credentials:"include",cache:"no-store",
          redirect:"follow",signal:controller.signal});
        const final=new URL(response.url);
        if (/^id\.(?:tbank\.ru|tbank-online\.com)$/.test(final.hostname) ||
            BANKS.has(final.hostname) && /^\/auth(?:\/|$)/.test(final.pathname) ||
            response.status===401) {
          status="login_required";
        } else if (response.ok && final.origin===location.origin) {
          // Pulse hides the Deals tab while not signed in (verified against
          // our project's existing browser monitor). Do not infer from a
          // "No trades" message or from instrument counts.
          const body=document.body?.innerText || "";
          const hasDeals=/Сделки/.test(body);
          const signIn=/Войти в (?:Т-Банк|Тинькофф)|Войти в аккаунт|Авторизоваться/i.test(body);
          if(signIn && !hasDeals) status="login_required";
          else if(hasDeals && !/Создать профиль/.test(body)) status="authenticated";
        }
      } catch { status="unknown"; }
      finally {clearTimeout(timeout);}
      await request("report",status);
    } catch { /* Connectivity failure is handled by the server's watchdog. */ }
    finally {running=false;}
  }
  check();
  setInterval(check,CHECK_MS);
})();
