/* Layout gesture adapter; local design preferences only, not authorization. */
(()=>{
'use strict';
const dashboard=$('dashboard'),sizes=['s','m','l'],allKeys=Object.keys(layoutDefaults[lens]);
const labels={summary:'Financial summary',paid:'Paid',denied:'Denied',resubmitted:'Resubmission',awaiting:'Awaiting',chart:'Monthly revenue',status:'Claim status',evidence:'Evidence explorer',operations:'Facility review',reconciliation:'Data reconciliation',breakdown:'Breakdown'};
const groups={ledger:['paid','denied','resubmitted','awaiting'],cycle:['chart','status'],operations:['operations','reconciliation'],primary:['summary','evidence'],breakdown:['breakdown']};
const groupFor=k=>Object.keys(groups).find(g=>groups[g].includes(k));
function validOrder(raw,defaults){return Array.isArray(raw)&&raw.length===defaults.length&&new Set(raw).size===defaults.length&&raw.every(k=>defaults.includes(k))?raw.slice():defaults.slice()}
let order=Object.fromEntries(Object.entries(groups).map(([g,v])=>[g,v.slice()]));
const orderStore=STORE+':widget-order-v1';
try{const previously=JSON.parse(localStorage.getItem(orderStore)||'{}');for(const [g,v] of Object.entries(groups))order[g]=validOrder(previously[g],v)}catch{}
const clone=x=>JSON.parse(JSON.stringify(x));
let before=null,press=null,heldId=null,gesture=null,suppressClickUntil=0;
const editButton=$('customize'),reset=$('resetLayout');
const cancel=document.createElement('button');cancel.id='cancelWidgetLayout';cancel.className='btn';cancel.type='button';cancel.textContent='Cancel';reset.parentNode.insertBefore(cancel,reset);
const guide=document.createElement('p');guide.id='gestureGuide';guide.setAttribute('role','status');guide.textContent='Drag the move handle or use its arrow keys. Drag the resize grip, or use + and −. Reorder stays within each layout group.';reset.parentNode.appendChild(guide);
const announce=t=>{guide.textContent=t};
function nodeFor(k){if(groups.ledger.includes(k))return dashboard.querySelector('[data-gesture-widget="'+k+'"]');return $(k==='breakdown'?'breakdown':'widget-'+k)}
function registerNodes(){for(const k of allKeys){const n=nodeFor(k);if(n)n.dataset.gestureWidget=k}}
// Keep fixed surrounding sections and order only the group's existing slots.
function applyOrders(){
 for(const [g,keys] of Object.entries(order)){
  const nodes=keys.map(nodeFor).filter(Boolean);
  if(nodes.length<2||!nodes.every(n=>n.parentNode===nodes[0].parentNode))continue;
  const p=nodes[0].parentNode,actual=Array.from(p.children).filter(n=>nodes.includes(n));
  if(actual.every((n,i)=>n===nodes[i]))continue;
  const placeholders=actual.map(n=>{const c=document.createComment('layout slot');p.insertBefore(c,n);return c});
  nodes.forEach((n,i)=>p.insertBefore(n,placeholders[i]));
  placeholders.forEach(c=>c.remove());
 }
}
const originalToolbar=toolbar;
toolbar=function(k){const n=sizes.indexOf(layout()[k]),name=labels[k]||k, group=groups[groupFor(k)]||[];
 return '<button type="button" class="gesture-control" data-g-action="drag" data-g-key="'+k+'" title="Move '+name+'" aria-label="Move '+name+' with arrow keys" '+(group.length<2?'disabled':'')+'>↕</button>'+
 '<button type="button" class="gesture-control" data-g-action="minus" data-g-key="'+k+'" aria-label="Make '+name+' smaller" '+(n===0?'disabled':'')+'>−</button>'+
 '<button type="button" class="gesture-control" data-g-action="resize" data-g-key="'+k+'" aria-label="Resize '+name+' with arrow keys; current '+sizes[n]+'" title="Drag resize grip">◩</button>'+
 '<button type="button" class="gesture-control" data-g-action="plus" data-g-key="'+k+'" aria-label="Make '+name+' larger" '+(n===2?'disabled':'')+'>+</button>'};
const coreApply=applyLayout;
applyLayout=function(){coreApply();registerNodes();applyOrders();editButton.textContent=editingLayout?'Done':'Edit layout'};
function begin(k){
 if(editingLayout)return;
 before={presets:clone(presetsByLens),order:clone(order)};
 editingLayout=true;applyLayout();
 announce('Edit mode · drag to reorder / resize, or use arrow keys. Done saves your layout.');
 if(k){const n=nodeFor(k);if(n)n.classList.add('gesture-active')}
}
function finish(save){
 if(!editingLayout)return;
 if(save){saveLayouts();try{localStorage.setItem(orderStore,JSON.stringify(order))}catch{}}
 else if(before){presetsByLens=before.presets;order=before.order}
 editingLayout=false;before=null;gesture=null;applyLayout();
 dashboard.querySelectorAll('.gesture-active').forEach(n=>n.classList.remove('gesture-active'));
}
editButton.onclick=()=>editingLayout?finish(true):begin();
reset.onclick=()=>{if(!editingLayout)begin();delete presetsByLens[lens];order=Object.fromEntries(Object.entries(groups).map(([g,v])=>[g,v.slice()]));applyLayout();announce('Default layout restored. Done saves, Cancel restores your previous layout.')};
cancel.onclick=()=>finish(false);
function changeSize(k,num){if(!allKeys.includes(k))return;const i=Math.min(2,Math.max(0,num));if(layout()[k]===sizes[i])return;presetsByLens[lens]={...(presetsByLens[lens]||{}),[k]:sizes[i]};applyLayout();announce(labels[k]+' · '+['Compact','Standard','Expanded'][i]);}
function move(k,target){const g=groupFor(k);if(!g||!order[g].includes(target)||target===k)return false;let list=order[g],i=list.indexOf(k),j=list.indexOf(target);list.splice(i,1);list.splice(j,0,k);applyOrders();announce(labels[k]+' moved to position '+(j+1)+' of '+list.length);return true}
function reFocus(k,a){const b=dashboard.querySelector('[data-g-key="'+k+'"][data-g-action="'+a+'"]');if(b)b.focus()}
function clearPress(){if(press){clearTimeout(press.timer);press=null}}
function clickHandler(e){
 if(performance.now()<suppressClickUntil){e.preventDefault();e.stopImmediatePropagation();return}
 const b=e.target.closest('[data-g-action]');if(!editingLayout||!b)return;
 e.preventDefault();e.stopPropagation();
 if(b.dataset.gAction==='minus'||b.dataset.gAction==='plus'){const k=b.dataset.gKey;changeSize(k,sizes.indexOf(layout()[k])+(b.dataset.gAction==='plus'?1:-1));reFocus(k,'resize')}
}
dashboard.addEventListener('click',clickHandler,true);
dashboard.addEventListener('contextmenu',e=>{if(e.target.closest('[data-gesture-widget]')&&performance.now()<suppressClickUntil)e.preventDefault()});
dashboard.addEventListener('pointerdown',e=>{
 if(e.button!==0&&e.pointerType==='mouse')return;
 const b=e.target.closest('[data-g-action]');
 if(editingLayout&&b&&['drag','resize'].includes(b.dataset.gAction)&&!b.disabled){
  gesture={id:e.pointerId,k:b.dataset.gKey,type:b.dataset.gAction,x:e.clientX,y:e.clientY,start:sizes.indexOf(layout()[b.dataset.gKey]),changed:false};
  dashboard.setPointerCapture?.(e.pointerId);nodeFor(gesture.k)?.classList.add('gesture-active');e.preventDefault();return;
 }
 if(editingLayout||e.target.closest('input,select,textarea,a,[contenteditable]'))return;
 const widget=e.target.closest('[data-gesture-widget]');if(!widget)return;
 clearPress();press={id:e.pointerId,x:e.clientX,y:e.clientY,timer:setTimeout(()=>{const k=widget.dataset.gestureWidget;heldId=e.pointerId;clearPress();suppressClickUntil=performance.now()+800;begin(k)},480)};
});
dashboard.addEventListener('pointermove',e=>{
 if(press&&e.pointerId===press.id&&Math.hypot(e.clientX-press.x,e.clientY-press.y)>10)clearPress();
 if(!gesture||e.pointerId!==gesture.id)return;
 e.preventDefault();
 if(gesture.type==='resize'){const delta=Math.abs(e.clientX-gesture.x)>=Math.abs(e.clientY-gesture.y)?e.clientX-gesture.x:e.clientY-gesture.y;const steps=Math.sign(delta)*Math.floor((Math.abs(delta)+15)/65);changeSize(gesture.k,gesture.start+steps);gesture.changed=gesture.changed||steps!==0;}
 else{
  const target=document.elementFromPoint(e.clientX,e.clientY)?.closest('[data-gesture-widget]');
  const k=target?.dataset.gestureWidget;if(k&&k!==gesture.last&&move(gesture.k,k)){gesture.last=k;gesture.changed=true}
 }
});
function end(e){clearPress();if(heldId===e.pointerId){heldId=null;suppressClickUntil=performance.now()+500}if(gesture&&gesture.id===e.pointerId){const n=nodeFor(gesture.k);n?.classList.remove('gesture-active');if(dashboard.hasPointerCapture?.(e.pointerId))dashboard.releasePointerCapture(e.pointerId);if(gesture.changed)suppressClickUntil=performance.now()+350;gesture=null}}
dashboard.addEventListener('pointerup',end);dashboard.addEventListener('pointercancel',end);dashboard.addEventListener('lostpointercapture',end);
dashboard.addEventListener('keydown',e=>{
 if(e.key==='Escape'&&editingLayout){e.preventDefault();finish(false);editButton.focus();return}
 if(!editingLayout)return;
 const b=e.target.closest('[data-g-action]');if(!b)return;const k=b.dataset.gKey,a=b.dataset.gAction;
 const dir=['ArrowLeft','ArrowUp','-'].includes(e.key)?-1:['ArrowRight','ArrowDown','+','='].includes(e.key)?1:0;if(!dir)return;
 e.preventDefault();
 if(a==='drag'){const list=order[groupFor(k)],j=list.indexOf(k)+dir;if(j>=0&&j<list.length)move(k,list[j]);reFocus(k,'drag')}
 else if(a==='resize'){changeSize(k,sizes.indexOf(layout()[k])+dir);reFocus(k,'resize')}
});
applyLayout();
})();
