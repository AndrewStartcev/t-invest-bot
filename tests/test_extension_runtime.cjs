/* Exercise the real service worker and content script together, offline.
 * The bank double rejects the old prefix, missing query, and missing app
 * headers. Only an instrument request is pre-observed, not nickname.
 */
'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const origin='https://www.tbank.ru',base=origin+'/mybank/api/social-api-gateway/social/v1/profile/';
const page=origin+'/invest/pulse/profile/SpaceForcez/operations/';
const uuid='8cd0ecdb-d1ee-42f4-a05f-4856e97331f5';
const tab={id:23,url:page};
const state={enabled:true,endpoint:'https://invest.argokov.ru',key:'test-only-pairing-'.repeat(3)};
const session={},events={},reports=[],calls=[];
let workerListener,contentListener,id=0;
const event=name=>({addListener(fn){events[name]=fn;}});
const storage=cache=>({async get(keys){if(typeof keys==='string')keys=[keys];return Object.fromEntries(keys.map(k=>[k,cache[k]]));},async set(v){Object.assign(cache,v);},async remove(keys){for(const k of keys)delete cache[k];}});
const workerChrome={storage:{local:storage(state),session:storage(session)},
  webRequest:{onBeforeSendHeaders:event('headers'),onCompleted:event('complete'),onErrorOccurred:event('error'),onBeforeRedirect:event('redirect')},
  permissions:{async contains(){return true;}},
  tabs:{onUpdated:event('update'),onRemoved:event('remove'),async query(){return [tab];},async sendMessage(tabId,msg){
    assert.equal(tabId,tab.id);if(contentListener){let result;contentListener(msg,{},v=>result=v);return result;}
  }},runtime:{onMessage:{addListener(fn){workerListener=fn;}},getManifest(){return {version:'test'};}}};
async function panelFetch(url,options){
  assert.equal(url,state.endpoint+'/api/extension/report');
  assert.equal(options.headers['X-TInvest-Extension-Key'],state.key);
  const report=JSON.parse(options.body);reports.push(report);
  return {ok:true,status:200,json:async()=>report.kind==='config'?
    {ok:true,profile_url:origin+'/invest/social/profile/SpaceForcez/',counts:{}}:{ok:true}};
}
vm.runInNewContext(fs.readFileSync('extension/background.js','utf8'),{chrome:workerChrome,URL,Date,Map,Set,AbortSignal,fetch:panelFetch});
async function bankFetch(value,opts){
  const u=new URL(value),requestId=String(++id);calls.push(u);
  await events.headers({tabId:tab.id,requestId,url:u.href,
    requestHeaders:Object.entries(opts.headers||{}).map(([name,value])=>({name,value}))});
  const valid=u.href.startsWith(base) && u.searchParams.get('sessionId')==='bank-local-secret' &&
    opts.headers['x-app-name']==='invest' && opts.headers['x-platform']==='web';
  const status=valid?200:404;
  await events.complete({tabId:tab.id,requestId,url:u.href,statusCode:status});
  return {ok:valid,status,url:u.href,json:async()=>u.pathname.endsWith('/nickname/SpaceForcez')?
    {payload:{id:uuid}}:{payload:{items:[],hasNext:false}}};
}
(async()=>{
  // The site's real list request completed before the content script mounted.
  const instrument=base+uuid+'/instrument?sessionId=bank-local-secret&limit=30';
  await events.headers({tabId:tab.id,requestId:'native',url:instrument,requestHeaders:[
    {name:'x-app-name',value:'invest'},{name:'x-platform',value:'web'},
    {name:'Cookie',value:'never-export-this'}]});
  await events.complete({tabId:tab.id,requestId:'native',url:instrument,statusCode:200});
  const chrome={storage:workerChrome.storage,runtime:{onMessage:{addListener(fn){contentListener=fn;}},
    sendMessage(message,reply){workerListener(message,{tab},reply);}}};
  vm.runInNewContext(fs.readFileSync('extension/content.js','utf8'),{chrome,URL,Date,AbortController,
    location:new URL(page),fetch:bankFetch,setTimeout(){return 1;},clearTimeout(){}});
  for(let i=0;i<100;i++)await new Promise(r=>setImmediate(r));
  const trades=reports.filter(r=>r.kind==='trades');
  assert.equal(trades.length,1);assert.equal(trades[0].status,'ok');
  assert.equal(calls[0].pathname,base.replace(origin,'')+'nickname/SpaceForcez');
  assert.equal(calls[1].pathname,base.replace(origin,'')+uuid+'/instrument');
  assert(calls.every(u=>u.searchParams.get('limit')==='30'));
  let diag;contentListener({kind:'diagnose'},{},v=>diag=v);
  assert.equal(diag.code,'ok');assert.equal(diag.verified,true);
  for(const secret of ['bank-local-secret','never-export-this',state.key]) {
    assert(!JSON.stringify(reports).includes(secret));assert(!JSON.stringify(diag).includes(secret));
  }
  console.log('Real MV3/content bridge: canonical gateway, missing nickname capture, headers and query passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
