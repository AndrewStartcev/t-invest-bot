const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync(path.join(__dirname,'../demo/index.html'),'utf8');
const tabs=['trades','portfolio','instruments','results','connection'].map(key=>({dataset:{overviewTab:key},attrs:{},handlers:{},setAttribute(k,v){this.attrs[k]=v;},addEventListener(k,fn){this.handlers[k]=fn;},focus(){this.focused=true;}}));
const panels=tabs.map(button=>({id:'overview-'+button.dataset.overviewTab}));
const context={document:{querySelectorAll:s=>s==='[data-overview-tab]'?tabs:panels}};vm.createContext(context);
vm.runInContext(html.slice(html.indexOf('    function showOverviewTab('),html.indexOf('    function showJournalTab(')),context);
tabs[1].handlers.click();assert.equal(panels[1].hidden,false);assert.equal(panels[0].hidden,true);assert.equal(tabs[1].attrs['aria-selected'],'true');
tabs[4].handlers.keydown({key:'ArrowRight',preventDefault(){}});assert.equal(panels[0].hidden,false);assert.ok(tabs[0].focused);
console.log('Overview tab navigation passed');
