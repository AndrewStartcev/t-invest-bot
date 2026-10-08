const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),test=require('node:test');
const html=fs.readFileSync(path.join(__dirname,'../demo/index.html'),'utf8');
const code=html.slice(html.indexOf('    const settingsTabs='),html.indexOf('    function showOverviewTab('));
function setup(){
 const keys=['general','limits','policy','api','telegram','account'];
 const tabs=keys.map(key=>({dataset:{settingsTab:key},hidden:key==='account',attrs:{},listeners:{},setAttribute(k,v){this.attrs[k]=v;},addEventListener(k,v){this.listeners[k]=v;},focus(){this.focused=true;}}));
 const panels=keys.map(key=>({id:'settings-'+key,hidden:key!=='general'}));
 const savebar={hidden:false},form={elements:[]};
 const context=vm.createContext({document:{querySelectorAll(selector){return selector==='[data-settings-tab]'?tabs:panels;},querySelector(){return savebar;}},el(){return form;}});
 vm.runInContext(code,context);return {context,tabs,panels,savebar,form};
}
test('tabs switch without replacing form fields and preserve unsaved input',()=>{
 const h=setup(),field={value:'45678'};h.form.elements.push(field);
 h.tabs[1].listeners.click();assert.equal(h.panels[1].hidden,false);assert.equal(h.panels[0].hidden,true);
 assert.equal(h.tabs[1].attrs['aria-selected'],'true');assert.equal(h.tabs[0].tabIndex,-1);
 h.tabs[3].listeners.click();h.tabs[1].listeners.click();assert.equal(field.value,'45678');
 h.context.showSettingsTab('account');assert.equal(h.savebar.hidden,true);h.context.showSettingsTab('general');assert.equal(h.savebar.hidden,false);
});
test('keyboard wraps through available tabs and skips unavailable account',()=>{
 const h=setup();let prevented=false;h.tabs[4].listeners.keydown({key:'ArrowRight',preventDefault(){prevented=true;}});
 assert.ok(prevented);assert.equal(h.tabs[0].attrs['aria-selected'],'true');assert.ok(h.tabs[0].focused);
 h.tabs[0].listeners.keydown({key:'End',preventDefault(){}});assert.equal(h.tabs[4].attrs['aria-selected'],'true');
});
test('saving reveals an invalid field in a hidden tab before reporting validity',()=>{
 const h=setup();let reported=false;h.form.elements=[{willValidate:true,validity:{valid:false},closest(){return h.panels[1];},reportValidity(){reported=true;}}];
 assert.equal(h.context.validateSettings(),false);assert.equal(h.panels[1].hidden,false);assert.ok(reported);
 h.form.elements[0].validity.valid=true;assert.equal(h.context.validateSettings(),true);
});
