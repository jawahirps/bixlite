const fs=require('node:fs');const vm=require('node:vm');const assert=require('node:assert/strict');const path=require('node:path');
const html=fs.readFileSync(path.resolve(__dirname,'output.html'),'utf8');
const scripts=[...html.matchAll(/<script(?: [^>]*)?>([\s\S]*?)<\/script>/g)].map(m=>m[1]);
const dom=new Map, dynamic=new Map;
function parseAttrs(s){let a={};for(let m of s.matchAll(/([\w-]+)="([^"]*)"/g))a[m[1]]=m[2];return a}
class Elem{
constructor(attrs={}){this.attrs={...attrs};this.dataset=Object.fromEntries(Object.entries(attrs).filter(([k])=>k.startsWith('data-')).map(([k,v])=>[k.slice(5).replace(/-([a-z])/g,(_,x)=>x.toUpperCase()),v]));this.value=attrs.value||'';this.style={setProperty:(k,v)=>{this.style[k]=v}};this.classes=new Set((attrs.class||'').split(' '));this.classList={add:(k)=>this.classes.add(k),remove:k=>this.classes.delete(k),contains:k=>this.classes.has(k),toggle:(k,t)=>t===undefined?(this.classes.has(k)?(this.classes.delete(k),false):(this.classes.add(k),true)):t?(this.classes.add(k),true):(this.classes.delete(k),false)};this.textContent='';this._html='';this.open=false}
set innerHTML(s){this._html=s;this.textContent=s.replace(/<[^>]+>/g,'');let d=[];for(let m of s.matchAll(/<(?:button|article|div|tr|span|input|select|label|details|summary|b|option|pre)\b([^>]+)>/g)){let a=parseAttrs(m[1]);const e=new Elem(a);if(a.id)dom.set(a.id,e);if(Object.keys(a).some(k=>k.startsWith('data-'))||a.class)d.push(e)}dynamic.set(this,d)}
get innerHTML(){return this._html}setAttribute(k,v){this.attrs[k]=String(v)}getAttribute(k){return this.attrs[k]??null}removeAttribute(k){delete this.attrs[k]}showModal(){this.open=true}close(){this.open=false}click(){this.onclick?.()}focus(){} querySelector(){return undefined} append(x){this.children=(this.children||[]).concat(x)} prepend(x){this.children=(this.children||[]).concat(x)} remove(){} insertBefore(){} closest(){return this} scrollIntoView(){} }
for(let m of html.matchAll(/<\w+\b([^>]*\bid="[^"]+"[^>]*)>/g)){const a=parseAttrs(m[1]);dom.set(a.id,new Elem(a))}
const staticElems=[];for(let m of html.matchAll(/<button\b([^>]+)>/g)){let a=parseAttrs(m[1]);if(a['data-range']||a['data-role'])staticElems.push(new Elem(a))}
function select(q){const elems=[...staticElems,...Array.from(dynamic.values()).flat()];let match=q.match(/^\[([a-z-]+)(?:="([^"]+)")?\]$/);if(match)return elems.filter(e=>match[1] in e.attrs&&(match[2]===undefined||e.attrs[match[1]]===match[2]));if(q.startsWith('.'))return elems.filter(e=>e.classes.has(q.slice(1)));if(q.includes(' '))return [];if(q.includes('][')){const parts=q.replace(/\]\[/g,']|[').split('|');return elems.filter(e=>parts.every(p=>select(p).includes(e)))}return []}
let downloaded;let document={addEventListener(){},dispatchEvent(){},getElementById:id=>dom.get(id),querySelectorAll:select,querySelector:q=>select(q)[0],createElement:()=>{let e=new Elem();e.click=()=>downloaded=e.download;return e}};
let preferences=new Map();let localStorage={getItem:k=>preferences.get(k)||null,setItem:(k,v)=>preferences.set(k,v)};let cx=vm.createContext({document,localStorage,window:{scrollTo(){}},Blob,URL:{createObjectURL:()=> 'blob:offline-test',revokeObjectURL(){}},setTimeout:fn=>fn(),setInterval:()=>0,clearInterval(){},CustomEvent:class {constructor(type,evt){this.type=type;this.detail=evt?.detail}},console});for(const js of scripts)vm.runInContext(js,cx);
let get=id=>dom.get(id),run=s=>vm.runInContext(s,cx),click=(q)=>select(q)[0].click();


const api=()=>run('window.BixDemoEntitlements');
assert.deepEqual(Array.from(api().snapshot().scope),['Demo Clinic A','Demo Clinic B']);
assert.equal(api().snapshot().persona,'owner');
assert.equal(run('lens'),'executive');
assert.equal(get('entTenant'),undefined);assert.equal(get('entPersona'),undefined);
assert.ok(run("filtered.every(r=>r.f==='Demo Clinic A'||r.f==='Demo Clinic B')"));
assert.ok(run("dims.find(x=>x.key==='f').values.length===2"));
run("draftScope.dims.f=['Demo Clinic B'];apply()");
assert.ok(run("filtered.every(r=>r.f==='Demo Clinic B')"));
run("lcSelect(scopeRows()[0].id)");assert.ok(run("lcFocus!==null"));
const prev=run('lcFocus');
run("lcSelect(all.find(r=>r.f==='Demo Clinic C').id)");assert.equal(run('lcFocus'),prev);
get('customize').click();assert.equal(run('editingLayout'),true);get('resetLayout').click();
assert.equal(run('lens'),'executive');
assert.equal(run("assignedProfile.persona"),'owner');
assert.ok(run("Object.isFrozen(assignedProfile)"));
console.log('PASS: assigned owner profile, no role or tenant selector, scoped filters, lifecycle and layout controls. Mock DOM only.');
