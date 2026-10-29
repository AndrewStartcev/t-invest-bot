/* Simulate one authenticated Brave polling cycle without any network use.
 * Confirms the 24-hour history URL keeps sessionId in the browser and the
 * outbound panel report contains only normalized published trades.
 */
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

async function runCycle(doneList, published=1) {
  const profile = "https://www.tbank.ru/invest/social/profile/LinMath/";
  const browser = "https://www.tbank.ru/invest/pulse/profile/LinMath/operations/";
  const endpoint = "https://www.tbank.ru/mybank/api/invest/social/v1/profile/uuid/instrument?sessionId=private-token";
  const nickname = "https://www.tbank.ru/mybank/api/invest/social/v1/profile/nickname/LinMath?sessionId=private-token";
  const timestamp = new Date(Date.now() - 60 * 60 * 1000).toISOString();
  const reports = [];
  const storage = {enabled: true, interval: 5};
  let historyCalls = 0;
  const chrome = {
    storage:{local:{
      async get(keys){
        if(typeof keys === "string") return {[keys]:storage[keys]};
        return Object.fromEntries(keys.map(key=>[key,storage[key]]));
      },
      async set(data){Object.assign(storage,data);}
    }},
    runtime:{
      lastError:undefined,
      onMessage:{addListener(){}},
      sendMessage(message,callback){
        if(message.kind==="config"){
          callback({ok:true,data:{profile_url:profile,counts:{"BSPB:TQBR":2},
            history_done:doneList,month_done:[],month_offsets:{}}});
        }else if(message.kind==="get_instrument_url"){
          callback({ok:true,data:{instrument_url:endpoint,nickname_url:nickname,headers:{}}});
        }else if(message.kind==="report"){
          reports.push(message.report);
          callback({ok:true,data:{ok:true,read_only:true,accepted:message.report.events?.length||0}});
        }else throw Error("Unexpected browser message: "+message.kind);
      }
    }
  };
  async function fetch(url){
    const request = new URL(url);
    assert.equal(request.origin,new URL(browser).origin,"bank requests stay in Brave");
    const path=request.pathname;
    if(path.endsWith("/nickname/LinMath"))
      return {ok:true,status:200,url:request.href,json:async()=>({payload:{id:"uuid"}})};
    if(path.endsWith("/instrument")){
      assert.equal(request.searchParams.get("sessionId"),"private-token");
      return {ok:true,status:200,url:request.href,json:async()=>({payload:{
        items:[{ticker:"BSPB",classCode:"TQBR",showName:"БСПБ",
          statistics:{totalOperationsCount:2,maxTradeDateTime:timestamp}}],
        hasNext:false
      }})};
    }
    if(path.includes("/operation/instrument/BSPB/TQBR")){
      historyCalls++;
      assert.equal(request.searchParams.get("sessionId"),"private-token",
        "the bank's sessionId stays in browser-side history URL");
      return {ok:true,status:200,url:request.href,json:async()=>({payload:{
        items:Array.from({length:published}, (_,i)=>({
          action:"buy",tradeDateTime:new Date(Date.parse(timestamp)-i*1000).toISOString(),
          averagePrice:123.45
        })),
        hasNext:false
      }})};
    }
    if(request.href===browser) return {ok:true,status:200,url:browser};
    throw Error("Unexpected bank API path: "+request.pathname);
  }
  const context={chrome,location:new URL(browser),URL,Date,AbortController,
    fetch,console,
    setTimeout(){return 1;},clearTimeout(){}};
  vm.runInNewContext(fs.readFileSync("extension/content.js","utf8"),context,
    {filename:"extension/content.js"});
  const wanted=(doneList.length?1:2)+Math.ceil(published/200);
  for(let tries=0;tries<200 && reports.length<wanted;tries++)
    await new Promise(resolve=>setTimeout(resolve,4));
  assert.equal(reports[0]?.kind,"trades");
  assert.equal(reports[0]?.events.length,0);
  assert.equal(reports[0]?.instruments.length,1);
  if(doneList.length) {
    assert.equal(historyCalls,1,"24h history must not rescan; monthly history still loads");
    assert.equal(reports.length,wanted);
  }else{
    assert.equal(historyCalls,2);
    assert.equal(reports[1]?.kind,"history");
    assert.equal(reports[1]?.events.length,1);
    assert.equal(reports[1]?.events[0].ticker,"BSPB");
    assert.equal(reports[1]?.events[0].tradeDateTime,timestamp);
  }
  const monthly=reports.find(row=>row.kind==="month_history");
  assert(monthly,"a month-history batch must be sent independently");
  assert.equal(monthly.batch_index,0);
  assert.equal(monthly.complete,true);
  assert.equal(monthly.events.length,Math.min(200,published));
  const monthlyReports=reports.filter(row=>row.kind==="month_history");
  assert.equal(monthlyReports.length,Math.ceil(published/200));
  assert(monthlyReports.at(-1).complete);
  assert.deepEqual(monthlyReports.map(row=>row.batch_index),
    Array.from({length:monthlyReports.length},(_,i)=>i));
  assert(!JSON.stringify(reports).includes("private-token"),
    "the bank's request sessionId must never leave the browser");
}

(async()=>{
  await runCycle([]);
  await runCycle(["BSPB:TQBR"]);
  await runCycle(["BSPB:TQBR"],205);
  process.stdout.write("Pulse browser 24h/30d paginated history bridge checks passed\n");
})().catch(error=>{console.error(error);process.exitCode=1;});
