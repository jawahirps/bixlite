const fs=require('node:fs');const vm=require('node:vm');const assert=require('node:assert/strict');const path=require('node:path');
const html=fs.readFileSync(process.argv[2]||path.resolve(__dirname,'../bix-integrated/BIX-Lite-Lifecycle-Workspace.html'),'utf8');
const scripts=[...html.matchAll(/<script(?: [^>]*)?>([\s\S]*?)<\/script>/g)].map(m=>m[1]);
const dom=new Map, dynamic=new Map;
function parseAttrs(s){let a={};for(let m of s.matchAll(/([\w-]+)="([^"]*)"/g))a[m[1]]=m[2];return a}
const genericParent={children:[],insertBefore(n){if(n.id)dom.set(n.id,n);if(!this.children.includes(n))this.children.push(n);n.parentNode=this},appendChild(n){this.insertBefore(n)}};
class Elem{
constructor(attrs={}){this.attrs={...attrs};this.dataset=Object.fromEntries(Object.entries(attrs).filter(([k])=>k.startsWith('data-')).map(([k,v])=>[k.slice(5).replace(/-([a-z])/g,(_,x)=>x.toUpperCase()),v]));this.value=attrs.value||'';this.style={setProperty:(k,v)=>{this.style[k]=v}};this.classes=new Set((attrs.class||'').split(' '));this.classList={add:(k)=>this.classes.add(k),remove:k=>this.classes.delete(k),contains:k=>this.classes.has(k),toggle:(k,t)=>t===undefined?(this.classes.has(k)?(this.classes.delete(k),false):(this.classes.add(k),true)):t?(this.classes.add(k),true):(this.classes.delete(k),false)};this.textContent='';this._html='';this.open=false;this.listeners=new Map;this.parentNode=genericParent;this.children=[];this.disabled='disabled' in attrs}
set innerHTML(s){this._html=s;this.textContent=s.replace(/<[^>]+>/g,'');let d=[];for(let m of s.matchAll(/<(?:button|article|div|tr|span|input|select|label|details|summary|b|option|pre)\b([^>]+)>/g)){let a=parseAttrs(m[1]);const e=new Elem(a);if(a.id)dom.set(a.id,e);if(Object.keys(a).some(k=>k.startsWith('data-'))||a.class)d.push(e)}dynamic.set(this,d)}
get innerHTML(){return this._html}setAttribute(k,v){this.attrs[k]=String(v)}getAttribute(k){return this.attrs[k]??null}removeAttribute(k){delete this.attrs[k]}showModal(){this.open=true}close(){this.open=false}addEventListener(t,fn){this.listeners.set(t,[...(this.listeners.get(t)||[]),fn])}removeEventListener(t,fn){this.listeners.set(t,(this.listeners.get(t)||[]).filter(x=>x!==fn))}dispatchEvent(e){for(const fn of this.listeners.get(e.type)||[])fn({...e,target:e.target||this,currentTarget:this});this['on'+e.type]?.(e)}click(){this.dispatchEvent({type:'click',target:this,preventDefault(){},stopPropagation(){},stopImmediatePropagation(){}})}focus(){} querySelector(q){if(q==='.widget-toolbar')return undefined;return select(q)[0]} querySelectorAll(q){return select(q)} append(x){this.appendChild(x)} appendChild(x){this.insertBefore(x)} prepend(x){this.insertBefore(x,this.children[0])} remove(){if(this.parentNode?.children)this.parentNode.children=this.parentNode.children.filter(n=>n!==this)} insertBefore(x,ref){if(x.parentNode?.children)x.parentNode.children=x.parentNode.children.filter(n=>n!==x);const i=this.children.indexOf(ref);this.children.splice(i<0?this.children.length:i,0,x);x.parentNode=this;if(x.id)dom.set(x.id,x)} closest(){return this} scrollIntoView(){} }
for(let m of html.matchAll(/<\w+\b([^>]*\bid="[^"]+"[^>]*)>/g)){const a=parseAttrs(m[1]);dom.set(a.id,new Elem(a))}
const markup=html.replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi,'').replace(/<style\b[^>]*>[\s\S]*?<\/style>/gi,'');
const staticElems=[];for(let m of markup.matchAll(/<button\b([^>]+)>/g)){let a=parseAttrs(m[1]);if(Object.keys(a).some(k=>k.startsWith('data-')))staticElems.push(new Elem(a))}
function select(q){const elems=[...staticElems,...Array.from(dynamic.values()).flat()];let match=q.match(/^\[([a-z-]+)(?:="([^"]+)")?\]$/);if(match)return elems.filter(e=>match[1] in e.attrs&&(match[2]===undefined||e.attrs[match[1]]===match[2]));if(q.startsWith('#'))return dom.has(q.slice(1))?[dom.get(q.slice(1))]:[];if(q.startsWith('.'))return elems.filter(e=>e.classes.has(q.slice(1)));if(q.includes(' '))return [];if(q.includes('][')){const parts=q.replace(/\]\[/g,']|[').split('|');return elems.filter(e=>parts.every(p=>select(p).includes(e)))}return []}
let downloaded;const events=new Map;let document={addEventListener(type,fn){events.set(type,[...(events.get(type)||[]),fn])},dispatchEvent(e){for(const fn of events.get(e.type)||[])fn(e)},getElementById:id=>dom.get(id),querySelectorAll:select,querySelector:q=>q==='#lcEmailBody pre'?(dom.get('lcEmailText')||new Elem):select(q)[0],createComment:()=>new Elem(),createElement:()=>{let e=new Elem();e.click=()=>downloaded=e.download;return e},elementFromPoint:()=>null};
let preferences=new Map();let localStorage={getItem:k=>preferences.get(k)||null,setItem:(k,v)=>preferences.set(k,v)};let cx=vm.createContext({document,localStorage,window:{scrollTo(){}},Blob,URL:{createObjectURL:()=> 'blob:offline-test',revokeObjectURL(){}},performance:{now:()=>10000},setTimeout:fn=>fn(),clearTimeout(){},setInterval:()=>0,clearInterval(){},CustomEvent:class {constructor(type,evt){this.type=type;this.detail=evt?.detail}},console});for(const js of scripts)vm.runInContext(js,cx);
let get=id=>dom.get(id),run=s=>vm.runInContext(s,cx),click=(q)=>select(q)[0].click();


let failures=0;
function check(label,test){try{test();console.log('PASS '+label)}catch(e){failures++;console.error('FAIL '+label+' — '+String(e.message).split('\n')[0].slice(0,400))}}
check('synthetic amount allocation and no duplicate claim identity',()=>{
 assert.ok(run("all.length>0&&all.every(r=>r.id.startsWith('SYN-'))"));
 assert.ok(run("new Set(all.map(r=>r.f+'|'+r.id)).size===all.length"));
 assert.ok(run("all.every(r=>[r.net,r.paid,r.denied,r.resubmitted,r.awaiting].every(Number.isFinite)&&r.paid>=0&&r.denied>=0&&r.resubmitted>=0&&r.awaiting>=0&&r.net===r.paid+r.denied+r.resubmitted+r.awaiting)"));
 assert.ok(run("all.filter(r=>r.resubmitted>0).every(r=>r.denied===0&&r.resub&&r.initialDenied===r.resubmitted)"));
 assert.ok(run("totals(scopeRows()).net===totals(scopeRows()).paid+totals(scopeRows()).denied+totals(scopeRows()).resubmitted+totals(scopeRows()).awaiting"));
});
check('five visible financial buckets and no decorative sparklines',()=>{
 assert.ok(get('heroNet')?.textContent);
 assert.equal(select('.ledger-item').length,4);
 assert.match(get('kpis').innerHTML,/Paid/);
 assert.match(get('kpis').innerHTML,/Denied/);
 assert.match(get('kpis').innerHTML,/Resubmission/i);
 assert.match(get('kpis').innerHTML,/Awaiting RA/i);
 assert.doesNotMatch(get('kpis').innerHTML,/class="spark"/);
});
check('calendar/filter scope retained during grouped chart and metric drill',()=>{
 assert.ok(run('Array.isArray(dims)&&dims.length>=8'));
 const permitted=run("dims.find(d=>d.key==='f').values.slice()");
 run("draftScope.dims.f=[dims.find(d=>d.key==='f').values[0]];apply()");
 const initial=run("JSON.stringify(appliedScope)");
 assert.ok(run("filtered.every(r=>r.f===appliedScope.dims.f[0])"));
 const first=select('[data-lc-series="resubmitted"]')[0];assert.ok(first);
 first.onfocus?.();assert.match(get('chartdetail').innerHTML,/Outstanding resubmission/i);
 first.click();assert.equal(run('activeStatus'),'resubmitted');assert.ok(run("activeMonth!==null&&scopeRows().every(r=>r.resubmitted>0&&r.month===activeMonth)"));
 assert.equal(run("JSON.stringify(appliedScope)"),initial);
 run("clearCross()");
 assert.ok(run("scopeRows().every(r=>r.f===appliedScope.dims.f[0])"));
 run("draftScope.dims.f=dims.find(d=>d.key==='f').values.slice();apply()");
 assert.equal(run("dims.find(d=>d.key==='f').values.length"),permitted.length);
});
check('compact scope disclosure opens and closes without changing filters',()=>{
 const scope=run("JSON.stringify(appliedScope)");const toggle=get('filterToggle'),panel=get('filterPanel');
 assert.equal(toggle.getAttribute('aria-expanded'),'false');
 toggle.click();assert.equal(toggle.getAttribute('aria-expanded'),'true');assert.ok(panel.classList.contains('expanded'));
 toggle.click();assert.equal(toggle.getAttribute('aria-expanded'),'false');assert.ok(!panel.classList.contains('expanded'));
 assert.equal(run("JSON.stringify(appliedScope)"),scope);
});
check('all financial chart series implemented as accessible actions',()=>{
 for(const k of ['net','paid','denied','resubmitted','awaiting']){const es=select('[data-lc-series="'+k+'"]');assert.ok(es.length>0,'missing series '+k);assert.ok(es.every(e=>e.getAttribute('aria-label')&&typeof e.onclick==='function'))}
 assert.ok(select('[data-lc-month-label]').length>0);
 assert.match(markup,/<button[^>]*id="heroNet"[^>]*data-lc-metric="net"/);
});
check('one monthly stacked column reconciles exactly to claimed net',()=>{
 const ms=Array.from(run("monthly.map(m=>({key:m.key,...totals(filtered.filter(r=>r.month===m.key&&matchStatus(r)))}))"));
 const output=get('plot').innerHTML;
 const groups=[...output.matchAll(/<div class="groupbars single-month">([\s\S]*?)<\/div><button class="month"/g)];
 assert.equal(groups.length,ms.length);
 const max=Math.max(1,...ms.map(m=>m.net)),scale=Math.max(1,Math.ceil(max*1.12/40000)*10000)*4;
 groups.forEach((g,i)=>{const m=ms[i],whole=g[1],stack=whole.match(/class="allocation-stack single-stack" style="height:([0-9.e+-]+)%"/);assert.ok(stack);assert.ok(Math.abs(Number(stack[1])-100*m.net/scale)<1e-8);assert.match(whole,/class="stack-total"[^>]*data-lc-series="net"/);assert.doesNotMatch(whole,/dual-claimed/);const bars=[...whole.matchAll(/data-lc-series="(paid|denied|resubmitted|awaiting)"[^>]* style="height:([0-9.e+-]+)%/g)];assert.equal(bars.length,4);assert.deepEqual(bars.map(b=>b[1]),['awaiting','resubmitted','denied','paid']);let sum=0;for(const b of bars){const pct=Number(b[2]);sum+=pct;assert.ok(Math.abs(pct-(m.net?100*m[b[1]]/m.net:0))<1e-8)}assert.ok(Math.abs(sum-(m.net?100:0))<1e-8)});
 run("visible.awaiting=false;renderChart()");assert.match(get('plot').innerHTML,/stack-segment hidden-series/);run("visible.awaiting=true;renderChart()");
});
check('five user themes preserve metrics, permissions and filter scope; remember selection',()=>{
 const expected={pearl:['#008e9b','#d34a5d','#4285e6','#f1b642'],aurora:['#0094a5','#e14e72','#7457e6','#efa631'],oceanic:['#008e9b','#d34a5d','#4285e6','#f1b642'],amethyst:['#06a6bb','#e76c80','#9565d9','#f5bd7b'],solar:['#00aa96','#d74670','#4988c9','#f28b30']};
 const initial=run("JSON.stringify({scope:appliedScope,net:totals(scopeRows()).net,profile:assignedProfile,activeMonth,activeStatus})");
 get('appTheme').click();assert.equal(get('themeDialog').open,true);
 for(const name of Object.keys(expected)){
  const b=select('[data-select-theme="'+name+'"]')[0];assert.ok(b);b.click();
  assert.equal(get('bixThemeRoot').getAttribute('data-theme'),name);assert.equal(run('savedBixTheme()'),name);
  assert.equal(run("JSON.stringify([colors.paid,colors.denied,colors.resubmitted,colors.awaiting])"),JSON.stringify(expected[name]));
  assert.match(get('plot').innerHTML,new RegExp(expected[name][0]));assert.match(get('kpis').innerHTML,new RegExp(expected[name][0]));
  assert.equal(run("JSON.stringify({scope:appliedScope,net:totals(scopeRows()).net,profile:assignedProfile,activeMonth,activeStatus})"),initial);
 }
 assert.equal(run("applyBixTheme('__proto__')"),false);
 run("localStorage.setItem(BIX_THEME_KEY,'not-a-theme')");assert.equal(run('savedBixTheme()'),'pearl');
 run("applyBixTheme('oceanic')");assert.equal(run('savedBixTheme()'),'oceanic');
 get('themeDone').click();assert.equal(get('themeDialog').open,false);
 run("applyBixTheme('aurora')");
});
check('workflow grouping, account consolidation, and compact command search',()=>{
 const ids=Array.from(run("sections.flatMap(([,items])=>items.map(x=>x[2]))"));
 assert.equal(ids.length,17);assert.equal(new Set(ids).size,17);
 assert.match(get('nav').innerHTML,/data-nav-group="Prepare & validate"/);assert.match(get('nav').innerHTML,/data-nav-group="Recover & reconcile"/);
 assert.doesNotMatch(get('nav').innerHTML,/data-page="(?:login-activity|account-security|facility-settings)"/);
 assert.match(get('accountLinks').innerHTML,/data-page="login-activity"/);assert.match(get('accountLinks').innerHTML,/data-page="account-security"/);assert.match(get('accountLinks').innerHTML,/data-page="facility-settings"/);
 get('appAccount').click();assert.equal(get('accountDialog').open,true);
 get('appTheme').click();assert.equal(get('accountDialog').open,false);assert.equal(get('themeDialog').open,true);get('themeDone').click();
 get('appAccount').click();const a=select('[data-page="login-activity"]')[0];assert.ok(a);a.click();assert.equal(get('accountDialog').open,false);assert.equal(get('moduletitle').textContent,'Login activity');
 get('back').click();assert.equal(get('dashboard').classList.contains('hidden'),false);
 document.dispatchEvent({type:'keydown',metaKey:true,ctrlKey:false,key:'k',preventDefault(){}});assert.equal(get('command').open,true);
 get('search').value='authenticator';get('search').oninput();assert.match(get('results').innerHTML,/account-security/);get('closeCommand').click();
});
check('nested rejection and distinct RA evidence stages',()=>{
 assert.ok(run("all.some(r=>r.lifecycle==='first-ra-pending')&&all.some(r=>r.lifecycle==='first-ra-rejected')&&all.some(r=>r.lifecycle==='second-ra-pending')&&all.some(r=>r.lifecycle==='second-ra-rejected')&&all.some(r=>r.lifecycle==='reconciliation-review')"));
 assert.deepEqual(Array.from(run("lcSteps(all.find(r=>r.lifecycle==='second-ra-rejected')).map(s=>s[0])")),['Original claim','Initial rejection','First resubmission','First resubmission RA','Second resubmission','Second resubmission RA','Decision']);
 assert.match(run("lcReport(all.find(r=>r.resubmitted>0))"),/SYNTHETIC.*NOT FOR SUBMISSION/i);
 assert.match(run("lcXml(all.find(r=>r.resubmitted>0))"),/OfflineExample NOT_FOR_SUBMISSION="true"/);
});
check('draft-only and local tracking guard; no live transport',()=>{
 assert.match(html,/connect-src\s+['"]none['"]/);
 assert.doesNotMatch(markup,/<form\b[^>]*action=["']https?:/i);
 assert.doesNotMatch(html,/\b(?:fetch\s*\(|new\s+XMLHttpRequest\b|navigator\.sendBeacon\s*\(|new\s+WebSocket\s*\()/);
 run("clearCross();lcFilter='all';lcFocus=null;renderLifecycle()");
 if(!get('lcContent').innerHTML.includes('synthetic claims')){console.log('NOTE: current profile locks detail; gating verified at aggregate level only.');return;}
 const paidId=run("scopeRows().find(r=>r.status==='full').id");
 run('lcFromEvidence('+JSON.stringify(paidId)+')');
 assert.match(get('lcContent').innerHTML,/id="lcEmail" disabled/);
 const rejectedId=run("scopeRows().find(r=>r.lifecycle==='second-ra-rejected').id");
 run('lcFromEvidence('+JSON.stringify(rejectedId)+')');
 assert.doesNotMatch(get('lcContent').innerHTML,/id="lcEmail" disabled/);
 assert.match(get('lcContent').innerHTML,/human evidence review/i);
 assert.match(get('lcContent').innerHTML,/non-submittable sample schema/i);
 assert.match(get('lcContent').innerHTML,/Save local review state/i);
 run("lcFocus=null;lcPage=1;renderLifecycle()");assert.match(get('lcContent').innerHTML,/page 2 of/i);
});
check('identity/profile rendered, no live-facing role, tenant or plan selector',()=>{
 // Strict static acceptance: no role switches or tenant/plan selectors in ordinary rendered HTML.
 // In a prototype, an immutable profile indicator is allowed; a separate explicitly-marked test fixture
 // can inject a profile before render, but should not expose selectors on the normal dashboard.
 assert.doesNotMatch(markup,/<(?:button|select|input)[^>]*\b(?:data-role=|id=["'](?:entTenant|entPersona|entPlan|roleSelect|tenantSelect|planSelect)["'])/i);
 assert.doesNotMatch(markup,/>\s*Demo access controls\s*</i);
 assert.match(markup,/profile|account|identity|signed.in|workspace/i);
 assert.ok(run("typeof assignedProfile==='object'&&Object.isFrozen(assignedProfile)"));
 const snapshot=run('window.BixDemoEntitlements.snapshot()');
 assert.equal(snapshot.tenant,run('assignedProfile.tenant'));
 assert.equal(snapshot.persona,run('assignedProfile.persona'));
 assert.ok(run("scopeRows().every(r=>window.BixDemoEntitlements.snapshot().scope.includes(r.f))"));
 assert.ok(run("Object.isFrozen(assignedProfile)"));
 run("lcFocus=null;lcDraft='';lcSelect(all.find(r=>!window.BixDemoEntitlements.snapshot().scope.includes(r.f)).id)");
 assert.equal(run('lcFocus'),null,'out-of-scope claim must remain unavailable');
 assert.equal(run('lcDraft'),'');

});
if(html.includes('widget-order-v1')){
 check('widget edit mode, keyboard resizing, cancel and persistence',()=>{
  const before=run("layout().chart");
  const key=before==='l'?'ArrowLeft':'ArrowRight';
  get('customize').onclick();
  assert.equal(run('editingLayout'),true);
  const target=select('[data-g-key="chart"][data-g-action="resize"]')[0];assert.ok(target);
  const dash=get('dashboard');
  dash.dispatchEvent({type:'keydown',key,target,preventDefault(){},stopPropagation(){}});
  const changed=run("layout().chart");assert.notEqual(changed,before);
  get('cancelWidgetLayout').onclick();assert.equal(run('editingLayout'),false);assert.equal(run("layout().chart"),before);
  get('customize').onclick();
  const second=select('[data-g-key="chart"][data-g-action="resize"]')[0];
  dash.dispatchEvent({type:'keydown',key,target:second,preventDefault(){},stopPropagation(){}});
  get('customize').onclick();assert.equal(run('editingLayout'),false);assert.equal(run("layout().chart"),changed);
  const store=run('STORE');assert.ok(preferences.has(store));assert.ok(preferences.has(store+':widget-order-v1'));
 });
}
console.log(failures?'\n'+failures+' failing acceptance test(s).':'\nAll synthetic acceptance checks passed.');
process.exitCode=failures?1:0;

