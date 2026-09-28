"""Chat-local cards; textContent only for untrusted prompts and answers."""
HUMAN_JS = r'''<script>
(()=>{
const host=document.getElementById('human-input'); if(!host)return;
let signature='', busy=false;const drafts=new Map();
const text=(tag,value,parent)=>{const e=document.createElement(tag);e.textContent=value;parent.append(e);return e;};
async function refresh(){
 if(busy)return;busy=true;
 try{
  const sid=new URL(location.href).searchParams.get('session')||host.dataset.session;if(!sid)return;
  const response=await fetch('/api/human-input?session='+encodeURIComponent(sid));if(!response.ok)return;
  const data=await response.json();const sig=JSON.stringify(data);if(sig===signature)return;signature=sig;host.replaceChildren();
  for(const q of data.questions){
   const card=text('section','',host);card.className='card';card.style.margin='8px 16px';
   text('strong',q.status!=='pending'?'交互记录':q.kind==='approval'?'请求操作授权':'等待你的回答',card);
   text('p',q.kind==='approval'?q.payload.reason:q.payload.question,card);
   if(q.kind==='approval'){text('pre',q.payload.command,card).style.whiteSpace='pre-wrap';text('p','宿主执行 · '+q.payload.workspace,card);}
   if(q.status!=='pending'){const labels={answered:'已回答',approved:'已允许',denied:'已拒绝',cancelled:'已取消',expired:'已过期'};text('p','状态：'+(labels[q.status]||q.status)+' '+q.answer,card);continue;}
   const form=text('form','',card);const input=text('textarea','',form);input.placeholder='输入你的回答';input.required=q.kind!=='approval';input.style.width='100%';input.hidden=q.kind==='approval';
   input.value=drafts.get(q.id)||'';input.oninput=()=>drafts.set(q.id,input.value);
   const status=text('p','',card);
   async function send(answer){
    for(const b of form.querySelectorAll('button'))b.disabled=true;
    try{const r=await fetch('/agent/human-input',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:new URLSearchParams({session:sid,id:q.id,answer,token:document.querySelector('meta[name="conversation-token"]').content})});const result=await r.json();if(!r.ok)throw Error(result.error);status.textContent=result.notice;signature='';if(result.continuing)window.dispatchEvent(new CustomEvent('agent-resumed',{detail:{session:sid}}));}
    catch(e){status.textContent=e.message;for(const b of form.querySelectorAll('button'))b.disabled=false;}
   }
   if(q.kind==='approval')for(const [label,value] of [['拒绝','deny'],['允许这一次','allow']]){const b=text('button',label,form);b.type='button';b.onclick=()=>send(value);}
   else{for(const option of q.payload.options||[]){const b=text('button',option,form);b.type='button';b.onclick=()=>{input.value=option;drafts.set(q.id,option);};}text('button','提交回答并继续',form);form.onsubmit=e=>{e.preventDefault();send(input.value);};}
  }
 }catch(e){}finally{busy=false;}
}
refresh();setInterval(refresh,1500);
})();</script>'''
