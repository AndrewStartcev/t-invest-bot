'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('extension/background.js','utf8');
const manifest=JSON.parse(fs.readFileSync('extension/manifest.json','utf8'));
const hosts=['tbank.ru','www.tbank.ru','tbank-online.com','www.tbank-online.com'];
for(const host of hosts){
  assert(manifest.host_permissions.includes(`https://${host}/*`));
  assert(manifest.content_scripts.some(s=>s.matches.includes(`https://${host}/invest/*`)));
}
assert(!manifest.host_permissions.some(s=>s.includes('*.')),'no unverified API subdomains');
const saved={},sent=[],events={},tabs=[{id:7,url:'https://www.tbank.ru/invest/pulse/profile/Alice/operations/'}];
function event(name){return {addListener(fn,filter){events[name]=fn;if(filter)assert.deepEqual(Array.from(filter.urls).sort(),hosts.map(h=>`https://${h}/*`).sort());}};}
function worker(){
  const chrome={webRequest:{onBeforeSendHeaders:event('headers'),onCompleted:event('complete'),onErrorOccurred:event('error'),onBeforeRedirect:event('redirect')},
    permissions:{async contains(){return true;}},
    storage:{local:{async get(){return {endpoint:'https://invest.argokov.ru',key:'x'.repeat(40),enabled:true};},async set(){}},
      session:{async get(keys){return Object.fromEntries(keys.map(k=>[k,saved[k]]));},async set(v){Object.assign(saved,v);},async remove(keys){for(const k of keys)delete saved[k];}}},
    tabs:{onRemoved:event('remove'),onUpdated:event('update'),async query(){return tabs;},
      async sendMessage(id,msg){sent.push({id,msg});if(msg.kind==='diagnose')throw Error('No receiver');}},
    runtime:{onMessage:event('message'),getManifest(){return manifest;}}};
  vm.runInNewContext(source,{chrome,URL,Date,Map,Set,AbortSignal,fetch:async()=>({ok:false,status:401,json:async()=>({error:'Invalid pairing'})})});
}
async function msg(message,sender={tab:tabs[0]}){
  return new Promise(resolve=>events.message(message,sender,resolve));
}
(async()=>{
  worker();
  const base='https://www.tbank.ru/mybank/api/invest/social/v1/profile/uuid/instrument?sessionId=private&nextCursor=2';
  await events.headers({tabId:7,url:base,requestHeaders:[{name:'Cookie',value:'SECRET'},{name:'Authorization',value:'SECRET'},{name:'X-App-Name',value:'invest'}]});
  await events.complete({tabId:7,url:base,statusCode:200});
  const captured=await msg({kind:'get_instrument_url'});
  assert(captured.ok);assert(!captured.data.instrument_url.includes('nextCursor'));
  assert(!JSON.stringify(captured).includes('SECRET'));
  worker(); // MV3 service worker restart: session cache survives.
  assert.equal((await msg({kind:'get_instrument_url'})).data.instrument_url,captured.data.instrument_url);
  await events.complete({tabId:7,url:base.replace('www.tbank.ru','evil.test'),statusCode:200});
  assert.equal((await msg({kind:'get_instrument_url'})).data.instrument_url,captured.data.instrument_url);
  await events.redirect({tabId:7,requestId:'redirect-1',url:base,statusCode:302,
    redirectUrl:'https://unknown.test/private/login?sessionId=private#secret'});
  await events.error({tabId:7,requestId:'redirect-1',url:base,error:'net::ERR_ABORTED'});
  const redirected=await msg({kind:'diagnostics'},{});
  assert.equal(redirected.data.tabs[0].network.redirectHost,'unknown.test');
  assert.equal((await msg({kind:'resolve_redirect',url:base})).data.url,'https://unknown.test/private/login?sessionId=private#secret');
  assert(!(await msg({kind:'resolve_redirect',url:base},{tab:{id:8,url:'https://evil.test/'}})).ok);
  assert.equal((await msg({kind:'resolve_redirect',url:base},{tab:{id:8,url:tabs[0].url}})).data.url,null);
  assert(!JSON.stringify(redirected).includes('sessionId'));
  assert(!JSON.stringify(redirected).includes('/private/login'));
  await events.error({tabId:7,requestId:'other',url:base,error:'net::ERR_CERT_AUTHORITY_INVALID'});
  const diag=await msg({kind:'diagnostics'},{});
  assert.equal(diag.data.tabs[0].state,null);assert(diag.data.tabs[0].network.error.includes('CERT'));
  assert(!JSON.stringify(diag).includes('private'),'diagnostics never exposes API query');
  assert(!(await msg({kind:'diagnostics'})).ok,'content script cannot inspect other tabs');
  const pairing=await msg({kind:'config'},{});assert.equal(pairing.scope,'dashboard');assert.equal(pairing.status,401);
  assert(!(await msg({kind:'get_instrument_url'},{tab:{id:7,url:'https://evil.test/invest/pulse/profile/Alice/'}})).ok);
  events.update(7,{url:tabs[0].url.replace('Alice','Bob')});
  assert.equal((await msg({kind:'get_instrument_url'})).data.instrument_url,null);
  console.log('MV3 permissions, capture, restart, channels and redacted diagnostics passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
