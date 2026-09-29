"""Chat-local cards; textContent only for untrusted prompts and answers."""
HUMAN_JS = r'''<script>
(()=>{
const host=document.getElementById('human-input'); if(!host)return;
const history=document.getElementById('human-history')||host;
let signature='', busy=false;const drafts=new Map(),expanded=new Map();
const slotFor=id=>Array.from(document.querySelectorAll('[data-human-slot]')).find(x=>x.dataset.humanSlot===id);
window.addEventListener('agent-timeline-updated',()=>{for(const card of document.querySelectorAll('.hi-card')){const slot=slotFor(card.dataset.questionId);if(slot&&card.parentElement!==slot)slot.append(card);}refresh();});
const styles=document.createElement('style');styles.textContent=`
#human-input:empty,#human-history:empty{display:none}
#human-input{flex-shrink:0;max-height:42vh;overflow:auto}
#human-input .hi-wrap,#human-history{max-width:760px;margin:0 auto;padding:12px 20px;box-sizing:border-box;width:100%}
.hi-card{display:block;min-width:0;box-sizing:border-box;border:1px solid var(--ds-border-l2,#ddd);border-radius:16px;background:var(--ds-bg-layer-1,#fff);padding:18px 20px;margin:0 0 12px;color:var(--ds-label-primary,#222)}
.human-slot{margin:14px 0}.hi-card>summary{cursor:pointer;list-style:none}.hi-card>summary:before{content:"▸"}.hi-card[open]>summary:before{content:"▾"}.hi-card:not([open])>.hi-head{margin-bottom:0}
.hi-card.hi-pending{border-color:var(--ds-brand,#536dfe);box-shadow:0 4px 20px #00000008}
.hi-head{display:flex;align-items:center;gap:10px;margin-bottom:14px;font-size:12px;color:var(--ds-label-tertiary,#666)}
.hi-icon{display:grid;place-items:center;width:28px;height:28px;border-radius:9px;background:var(--ds-bg-layer-3,#eee);font-size:14px;flex-shrink:0}
.hi-head strong{font-weight:600;color:var(--ds-label-primary,#222)}
.hi-brief{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
.hi-badge{margin-left:auto;flex-shrink:0;padding:3px 9px;border-radius:20px;background:var(--ds-bg-layer-3,#eee);font-size:11px}
.hi-question,.hi-answer{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.7;font-size:14px;margin:0}
.hi-reply{margin-top:16px;padding-top:14px;border-top:1px solid var(--ds-border-l2,#ddd)}
.hi-label{display:block;font-size:11px;letter-spacing:.04em;color:var(--ds-label-tertiary,#666);margin-bottom:6px}
.hi-command{white-space:pre-wrap;overflow-wrap:anywhere;max-height:180px;overflow:auto;border-radius:9px;background:var(--ds-bg-layer-2,#f5f5f5);padding:12px;font-size:12px}
.hi-location{overflow-wrap:anywhere;font-size:11px;color:var(--ds-label-tertiary,#666);margin:8px 0}
.hi-form{display:flex;flex-direction:column;align-items:stretch;gap:12px;margin-top:16px;width:100%;min-width:0}
.hi-options,.hi-actions{display:flex;flex-wrap:wrap;gap:8px}
.hi-card button{border:1px solid var(--ds-border-l2,#ddd);border-radius:9px;padding:8px 13px;font:inherit;font-size:12px;color:inherit;background:transparent;cursor:pointer;white-space:normal;overflow-wrap:anywhere;text-align:left;max-width:100%}
.hi-options button:hover,.hi-options button[aria-pressed=true]{border-color:var(--ds-brand,#536dfe);background:var(--ds-bg-layer-3,#eee)}
.hi-card textarea{box-sizing:border-box;width:100%;min-height:84px;resize:vertical;max-height:220px;padding:12px;border:1px solid var(--ds-border-l2,#ddd);border-radius:10px;background:var(--ds-bg-layer-2,#f5f5f5);color:inherit;font-family:inherit;font-size:14px;line-height:1.6;outline:none}
.hi-card textarea:focus{border-color:var(--ds-brand,#536dfe)}
.hi-actions{justify-content:flex-end}
.hi-card button.hi-primary{background:var(--ds-brand,#536dfe);border-color:transparent;color:#fff}
.hi-card button:disabled{opacity:.55;cursor:wait}
.hi-feedback{font-size:12px;color:var(--ds-label-tertiary,#666);margin:10px 0 0;white-space:pre-wrap}
.hi-feedback:empty{display:none}
@media(max-width:600px){#human-input .hi-wrap,#human-history{padding:10px 12px}.hi-card{padding:14px;border-radius:12px}.hi-actions button{flex:1;text-align:center}}
`;document.head.append(styles);
const text=(tag,value,parent)=>{const e=document.createElement(tag);e.textContent=value;parent.append(e);return e;};
async function refresh(){
 if(busy)return;busy=true;
 try{
  const sid=new URL(location.href).searchParams.get('session')||host.dataset.session;if(!sid)return;
  const response=await fetch('/api/human-input?session='+encodeURIComponent(sid));if(!response.ok)return;
  const data=await response.json();const sig=JSON.stringify(data);if(sig===signature&&data.questions.every(q=>document.querySelector('.hi-card[data-question-id="'+q.id+'"]')))return;signature=sig;
  const sc=document.getElementById('scroll'),nearBottom=sc&&sc.scrollHeight-sc.scrollTop-sc.clientHeight<80;
  const active=document.activeElement,focusId=active?.tagName==='TEXTAREA'?active.dataset.questionId:null,selection=focusId?[active.selectionStart,active.selectionEnd]:null;
  for(const card of document.querySelectorAll('.hi-card')){if(!card.classList.contains('hi-pending'))expanded.set(card.dataset.questionId,card.open);card.remove();}
  host.replaceChildren();if(history!==host)history.replaceChildren();
  const pending=document.createElement('div');pending.className='hi-wrap';
  if(data.questions.some(q=>q.status==='pending'))host.append(pending);
  for(const q of data.questions){
   const done=q.status!=='pending',approval=q.kind==='approval';
   const labels={answered:approval?'已允许一次':'已回答',approved:'已允许一次',denied:'已拒绝',cancelled:'已取消',expired:'已过期'};
   const card=text('details','',slotFor(q.id)||(done?history:pending));card.className='hi-card'+(done?'':' hi-pending');card.dataset.questionId=q.id;card.open=done?(expanded.get(q.id)||false):true;
   const head=text('summary','',card);head.className='hi-head';text('span',approval?'◇':'↳',head).className='hi-icon';
   text('strong',done?'交互记录':approval?'请求操作授权':'需要你的确认',head);
   if(done)text('span',approval?q.payload.command:q.payload.question,head).className='hi-brief';
   text('span',done?(labels[q.status]||q.status):'等待回答',head).className='hi-badge';
   text('p',approval?q.payload.reason:q.payload.question,card).className='hi-question';
   if(approval){text('pre',q.payload.command,card).className='hi-command';text('p','执行位置 · '+q.payload.workspace,card).className='hi-location';text('p','宿主执行时限 · '+(q.payload.timeout_s??60)+' 秒（不含等待批准）',card);if(q.payload.scope)text('p',q.payload.scope,card);if(q.payload.risk)text('p',q.payload.risk,card);}
   if(done){const reply=text('div','',card);reply.className='hi-reply';text('span',approval?'你的决定':'你的回答',reply).className='hi-label';text('p',approval?(labels[q.status]||q.status):(q.answer||'未提供回答'),reply).className='hi-answer';drafts.delete(q.id);continue;}
   const form=text('form','',card);form.className='hi-form';
   const options=text('div','',form);options.className='hi-options';
   const input=text('textarea','',form);input.placeholder='补充你的想法，或选择上方选项…';input.setAttribute('aria-label','输入你的回答');input.dataset.questionId=q.id;input.required=!approval;input.hidden=approval;
   input.value=drafts.get(q.id)||'';input.oninput=()=>{drafts.set(q.id,input.value);for(const b of options.children)b.setAttribute('aria-pressed',String(b.textContent===input.value));};
   const actions=text('div','',form);actions.className='hi-actions';
   const status=text('p','',card);status.className='hi-feedback';status.setAttribute('role','status');
   async function send(answer){
    for(const b of form.querySelectorAll('button'))b.disabled=true;
    try{const r=await fetch('/agent/human-input',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:new URLSearchParams({session:sid,id:q.id,answer,token:document.querySelector('meta[name="conversation-token"]').content})});const result=await r.json();if(!r.ok)throw Error(result.error);status.textContent=result.notice;signature='';if(result.continuing)window.dispatchEvent(new CustomEvent('agent-resumed',{detail:{session:sid}}));}
    catch(e){status.textContent=e.message;for(const b of form.querySelectorAll('button'))b.disabled=false;}
   }
   if(approval)for(const [label,value] of [['拒绝','deny'],['允许这一次','allow']]){const b=text('button',label,actions);b.type='button';if(value==='allow')b.className='hi-primary';b.onclick=()=>send(value);}
   else{for(const option of q.payload.options||[]){const b=text('button',option,options);b.type='button';b.setAttribute('aria-pressed',String(input.value===option));b.onclick=()=>{input.value=option;drafts.set(q.id,option);for(const x of options.children)x.setAttribute('aria-pressed',String(x===b));};}const submit=text('button','提交回答并继续',actions);submit.className='hi-primary';form.onsubmit=e=>{e.preventDefault();send(input.value);};}
  }
  if(focusId){const field=[...document.querySelectorAll('.hi-card textarea')].find(e=>e.dataset.questionId===focusId);if(field){field.focus({preventScroll:true});field.setSelectionRange(...selection);}}
  if(nearBottom)requestAnimationFrame(()=>{sc.scrollTop=sc.scrollHeight;});
 }catch(e){}finally{busy=false;}
}
refresh();setInterval(refresh,1500);
})();</script>'''
