/* Offline MV3 regressions. Never contacts bank, panel or brokerage. */
'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('extension/content.js','utf8');
async function settle(){for(let i=0;i<40;i++)await new Promise(r=>setImmediate(r));}
async function browser(options={}){
  const host=options.host||'www.tbank.ru';
  const location=new URL(`https://${host}/invest/pulse/profile/Alice/operations/`);
  const state={enabled:true,interval:5,endpoint:'https://invest.argokov.ru',key:'private-pairing'};
  const reports=[],calls=[],timers=[];
  let listener,author='Alice',mode=options.mode,observed=options.observed||{};
  const chrome={storage:{local:{async get(keys){return Object.fromEntries(keys.map(k=>[k,state[k]]));},
    async set(data){Object.assign(state,data);}}},runtime:{onMessage:{addListener(fn){listener=fn;}},
    sendMessage(msg,cb){
      if(msg.kind==='config')return cb(mode==='pairing'?{ok:false,error:'Ключ недействителен',scope:'dashboard',status:401}:
        {ok:true,data:{profile_url:`https://www.tbank.ru/invest/social/profile/${author}/`,counts:{}}});
      if(msg.kind==='get_instrument_url')return cb({ok:true,data:observed});
      if(msg.kind==='report'){reports.push(msg.report);return cb({ok:true,data:{ok:true}});}
      throw Error(msg.kind);
    }}};
  const ctx={chrome,location,URL,Date,AbortController,console,
    performance:{getEntriesByType(){return (options.resources||[]).map(name=>({name}));}},clearTimeout(){},
    setTimeout(fn,ms){if(ms===5000||ms===15000)timers.push(fn);return 1;},
    async fetch(value,opts){
      const u=new URL(value);calls.push(u);
      assert.equal(u.origin,location.origin);assert.equal(opts.redirect,'manual');
      if(mode==='capture-race' && u.pathname.endsWith('/instrument')) {
        listener({kind:'instrument_url',url:location.origin+'/mybank/api/invest/social/v1/profile/uuid-Bob/instrument'},{});
      }
      if(mode==='redirect')return {type:'opaqueredirect',ok:false,status:0};
      if(mode==='network')throw TypeError('Failed to fetch');
      if(mode==='navigate'){location.pathname='/invest/pulse/profile/Bob/operations/';mode='normal';}
      if(mode==='switch-key'){state.key='new-private-key';mode='normal';}
      if(mode==='stale-session' && u.searchParams.has('sessionId'))return {ok:false,status:401,url:u.href};
      if(mode==='401'||mode==='403'||mode==='404'||mode==='429')
        return {ok:false,status:Number(mode),url:u.href};
      if(mode==='html')return {ok:true,status:200,url:u.href,json:async()=>{throw SyntaxError('html');}};
      return {ok:true,status:200,url:u.href,json:async()=>u.pathname.includes('/nickname/')?
        {payload:{id:'uuid-'+author}}:{payload:{items:[],hasNext:false}}};
    }};
  vm.runInNewContext(source,ctx);await settle();
  const diag=()=>{let d;listener({kind:'diagnose'},{},v=>d=v);return d;};
  return {reports,calls,state,location,diag,setAuthor(a){author=a;},setMode(m){mode=m;},
    async next(){assert(timers.length);timers.shift()();await settle();}};
}
(async()=>{
  for(const host of ['tbank.ru','www.tbank.ru','tbank-online.com','www.tbank-online.com']){
    const b=await browser({host});assert.equal(b.diag().code,'ok');assert.equal(b.reports[0].status,'ok');
    assert.equal(b.calls.length,2,'bootstrap nickname + instrument without capture or F5');
    assert(!JSON.stringify(b.reports).includes('private-pairing'));
  }
  const recovered=await browser({resources:[
    'https://www.tbank.ru/mybank/api/invest/social/v1/profile/uuid-Alice/instrument?sessionId=local-secret']});
  assert.equal(recovered.diag().code,'ok');assert.equal(recovered.calls[0].searchParams.get('sessionId'),'local-secret');
  assert(!JSON.stringify(recovered.reports).includes('local-secret'));
  assert(!JSON.stringify(recovered.diag()).includes('local-secret'));
  const unknown=await browser({resources:['https://api.unknown.test/api/social/v1/profile/uuid-Alice/instrument?token=secret']});
  assert(unknown.diag().observedApi.some(s=>s.includes('api.unknown.test')));
  assert(!unknown.calls.some(u=>u.hostname==='api.unknown.test'));
  const redirect=await browser({mode:'redirect'});
  assert.equal(redirect.diag().code,'pulse_redirect');assert.equal(redirect.calls.length,1);
  assert(!redirect.diag().request.includes('?'));assert(redirect.diag().request.endsWith('/nickname/Alice'),'preserve nickname case');
  const pair=await browser({mode:'pairing'});assert.equal(pair.diag().code,'dashboard_401');
  assert.equal(pair.calls.length,0);assert.equal(pair.reports.length,0,'pairing failure cannot log out bank');
  for(const mode of ['401','403','404','429','network','html']){
    const b=await browser({mode});assert.notEqual(b.diag().code,'ok');
    assert.equal(b.reports.filter(r=>r.status==='ok').length,0);
    assert.equal(b.reports.some(r=>r.kind==='auth_status'),mode==='401');
    b.setMode('normal');await b.next();assert.equal(b.diag().code,'ok','recover '+mode);
  }
  const wrong=await browser({observed:{nickname_url:'https://www.tbank.ru/mybank/api/invest/social/v1/profile/nickname/Bob',
    instrument_url:'https://www.tbank.ru/mybank/api/invest/social/v1/profile/uuid-Bob/instrument'}});
  assert.equal(wrong.diag().code,'ok');assert(wrong.calls.at(-1).pathname.includes('/uuid-Alice/'));
  // Cached API for another UUID is replaced using the verified nickname identity.
  const stale=await browser({observed:{nickname_url:'https://www.tbank.ru/mybank/api/invest/social/v1/profile/nickname/Alice',
    instrument_url:'https://www.tbank.ru/mybank/api/invest/social/v1/profile/uuid-Bob/instrument'}});
  assert(stale.calls.at(-1).pathname.includes('/uuid-Alice/'));assert.equal(stale.diag().code,'ok');
  const foreign=await browser({observed:{nickname_url:'https://evil.test/mybank/api/invest/social/v1/profile/nickname/Alice'}});
  assert.equal(foreign.calls.length,0);assert.equal(foreign.diag().code,'api_address');
  for(const mode of ['navigate','switch-key']){
    const b=await browser({mode});assert.equal(b.reports.filter(r=>r.status==='ok').length,0,mode);
  }
  const racing=await browser({mode:'capture-race'});assert.equal(racing.diag().code,'ok');
  await racing.next();assert(racing.calls.at(-1).pathname.includes('/uuid-Alice/'));
  const expired=await browser({mode:'stale-session',observed:{
    nickname_url:'https://www.tbank.ru/mybank/api/invest/social/v1/profile/nickname/Alice?sessionId=old',
    instrument_url:'https://www.tbank.ru/mybank/api/invest/social/v1/profile/uuid-Alice/instrument?sessionId=old'}});
  assert.equal(expired.diag().code,'pulse_401');await expired.next();assert.equal(expired.diag().code,'ok');
  const spa=await browser();spa.location.pathname='/invest/pulse/profile/Bob/operations/';spa.setAuthor('Bob');
  await spa.next();assert(spa.calls.at(-1).pathname.includes('/uuid-Bob/'));
  spa.location.pathname='/invest/';await spa.next();assert.equal(spa.diag().code,'wrong_page');
  console.log('Pulse discovery, identity, pairing, navigation and failure regressions passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
