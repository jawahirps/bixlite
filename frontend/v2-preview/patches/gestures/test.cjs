const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const js=fs.readFileSync(__dirname+'/gestures.js','utf8');
const allkeys=['summary','paid','denied','resubmitted','awaiting','chart','status','evidence','operations','reconciliation','breakdown'];
const elements={},handlers={},stored=new Map(),timers=new Map();let timer=0,activeElm;
class E {
 constructor(id=''){this.id=id;this.children=[];this.dataset={};this.classList={add(){},remove(){}};}
 appendChild(e){return this.insertBefore(e,null)}
 insertBefore(e,ref){if(e.parentNode){let i=e.parentNode.children.indexOf(e);if(i>=0)e.parentNode.children.splice(i,1)}e.parentNode=this;let i=ref?this.children.indexOf(ref):-1;if(i<0)this.children.push(e);else this.children.splice(i,0,e);return e}
 remove(){if(this.parentNode){let i=this.parentNode.children.indexOf(this);if(i>=0)this.parentNode.children.splice(i,1)}}
 setAttribute(){} focus(){} addEventListener(k,fn){handlers[k]=fn}
 querySelector(s){return this.querySelectorAll(s)[0]||null}
 querySelectorAll(s){let out=[];for(const ch of this.children){let m=s.match(/^\[data-gesture-widget="([^"]+)"\]$/);if(m&&ch.dataset.gestureWidget===m[1])out.push(ch);out.push(...ch.querySelectorAll(s))}return out}
 closest(s){if(s==='[data-gesture-widget]'&&this.dataset.gestureWidget)return this;if(s==='[data-g-action]'&&this.dataset.gAction)return this;return null}
}
const dashboard=elements.dashboard=new E('dashboard'),bar=new E(),ledger=new E(),story=new E(),ops=new E();
for(const id of ['customize','resetLayout'])bar.appendChild(elements[id]=new E(id));dashboard.appendChild(bar);
for(const [group,ks] of [[dashboard,['summary','evidence','breakdown']],[ledger,['paid','denied','resubmitted','awaiting']],[story,['chart','status']],[ops,['operations','reconciliation']]]){if(group!==dashboard)dashboard.appendChild(group);for(const k of ks){const n=new E(k);n.dataset.gestureWidget=k;elements[k==='breakdown'?'breakdown':'widget-'+k]=n;group.appendChild(n)}}
let cx=vm.createContext({document:{createElement:()=>new E(),createComment:()=>new E(),elementFromPoint:()=>activeElm},localStorage:{getItem:k=>stored.get(k),setItem:(k,v)=>stored.set(k,v)},performance:{now:()=>10000},setTimeout:fn=>{timers.set(++timer,fn);return timer},clearTimeout:id=>timers.delete(id),$:id=>elements[id],console});
vm.runInContext("const lens='executive',STORE='profile';let presetsByLens={},editingLayout=false;const layoutDefaults={executive:"+JSON.stringify(Object.fromEntries(allkeys.map(k=>[k,'s'])))+"};function layout(){return {...layoutDefaults[lens],...(presetsByLens[lens]||{})}};function saveLayouts(){localStorage.setItem(STORE,JSON.stringify(presetsByLens))};function toolbar(){return ''};function applyLayout(){}",cx);
vm.runInContext(js,cx);const run=s=>vm.runInContext(s,cx);
const event=(target,extras={})=>({target,button:0,pointerType:'mouse',pointerId:1,clientX:0,clientY:0,preventDefault(){},stopPropagation(){},stopImmediatePropagation(){},...extras});
handlers.pointerdown(event(elements['widget-paid']));assert.equal(run('editingLayout'),false);[...timers.values()][0]();assert.equal(run('editingLayout'),true);
const key=(k,a,key)=>{const e=new E();e.dataset={gKey:k,gAction:a};handlers.keydown(event(e,{key}))};
key('paid','resize','ArrowRight');assert.equal(run("layout().paid"),'m');assert.equal(stored.has('profile'),false);
key('paid','drag','ArrowDown');assert.deepEqual(ledger.children.map(e=>e.id),['denied','paid','resubmitted','awaiting']);
elements.customize.onclick();assert.equal(run('editingLayout'),false);assert.ok(stored.has('profile'));assert.ok(stored.get('profile:widget-order-v1').includes('denied'));
elements.customize.onclick();key('paid','resize','ArrowRight');assert.equal(run("layout().paid"),'l');handlers.keydown(event(elements.customize,{key:'Escape'}));assert.equal(run("layout().paid"),'m');
elements.customize.onclick();elements.resetLayout.onclick();assert.equal(run("layout().paid"),'s');assert.deepEqual(ledger.children.map(e=>e.id),['paid','denied','resubmitted','awaiting']);
console.log('PASS mock: long press, resize and reorder keyboard, live layout, save on Done, Escape rollback, reset');
