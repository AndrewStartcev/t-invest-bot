/* The published portfolio remains fresh only while the customer's own
 * browser periodically verifies the visible Companies view. No bank secrets.
 */
"use strict";
const assert=require("node:assert/strict");
const fs=require("node:fs");
const vm=require("node:vm");

async function main(){
  let tick=Date.now(), showRows=true, showCompanies=true;
  const reports=[], storage={enabled:true};
  const profile="https://www.tbank.ru/invest/social/profile/LinMath/";
  const view=new URL(profile);
  const leaf={innerText:"Роснефть 5%",children:[],getClientRects(){return [1];}};
  const link={href:"https://www.tbank.ru/invest/stocks/ROSN/"};
  const row={innerText:"Роснефть 5%",querySelectorAll(){return [link];}};
  leaf.parentElement=row;
  const panel={innerText:"Компании активы Роснефть 5%",parentElement:null,
               querySelectorAll(){return showRows?[leaf]:[];}};
  const heading={innerText:"LinMath. Портфель",getClientRects(){return [1];},
                 parentElement:panel};
  const tab={innerText:"Компании",getClientRects(){return [1];}};
  const document={
    body:{},documentElement:{},
    querySelectorAll(selector){
      if(selector.startsWith("h1,"))return [heading];
      if(selector.includes("[role="))return showCompanies?[tab]:[];
      return [];
    },
    addEventListener(){}
  };
  const callbacks={};
  const chrome={
    storage:{local:{
      async get(){return {enabled:storage.enabled};},
      async set(value){Object.assign(storage,value);}
    }},
    runtime:{async sendMessage(request){
      if(request.kind==="config")return {ok:true,data:{profile_url:profile}};
      if(request.kind==="portfolio"||request.kind==="portfolio_status"){
        reports.push(request);
        return {ok:true,data:{rows:1,positions:1,checked_at:new Date(tick).toISOString()}};
      }
      throw Error("Unexpected browser request: "+request.kind);
    }}
  };
  class Clock extends Date {static now(){return tick;}}
  const context={document,window:{addEventListener(){}},location:view,
    chrome,Date:Clock,URL,getComputedStyle(){return {visibility:"visible"};},
    setTimeout(callback){callbacks.initial=callback;},
    setInterval(callback){callbacks.interval=callback;},console};
  vm.runInNewContext(fs.readFileSync("extension/portfolio.js","utf8"),context,
    {filename:"extension/portfolio.js"});
  assert.equal(typeof callbacks.initial,"function");
  await callbacks.initial();
  assert.equal(reports.length,1);
  assert.equal(reports[0].kind,"portfolio");
  assert.equal(reports[0].report.screen.rows[0].text,"Роснефть 5%");
  assert(!JSON.stringify(reports).includes("sessionId"));

  tick+=15000;
  await callbacks.interval();
  assert.equal(reports.length,1,"An identical snapshot need not be resent every 15s");
  tick+=60000;
  await callbacks.interval();
  assert.equal(reports.length,2,"A verified unchanged snapshot must be renewed before expiry");

  showRows=false;
  tick+=15000;
  await callbacks.interval();
  assert.equal(reports.length,2,"Loading animations must not invalidate a good portfolio instantly");
  tick+=15000;
  await callbacks.interval();
  assert.equal(reports.length,3);
  assert.equal(reports[2].kind,"portfolio_status");
  assert.equal(reports[2].report.reason,"unrecognized");

  showRows=true;
  tick+=15000;
  await callbacks.interval();
  assert.equal(reports.length,4,"Recovery must send a fresh snapshot immediately");
  assert.equal(reports[3].kind,"portfolio");
  assert.equal(storage.portfolio_error,"");
  assert(!JSON.stringify(reports).includes("cookie"));
  console.log("Portfolio periodic refresh, invalidation and recovery tests passed");
}
main().catch(error=>{console.error(error);process.exitCode=1;});
