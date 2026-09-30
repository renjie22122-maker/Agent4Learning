(()=>{
const style=document.createElement('style');style.textContent=`
.turn-elapsed{font-variant-numeric:tabular-nums;color:var(--ds-text-secondary);margin-right:12px}.file-diff pre{max-height:420px;overflow:auto;padding:12px}.file-diff pre span{display:block;white-space:pre}.diff-add{background:#267c4033;color:#82cc9b}.diff-remove{background:#b5384633;color:#eba5ad}.katex-display{overflow:auto;padding:8px}.live-connection{font-size:11px;min-height:20px;padding:0 20px;color:var(--ds-text-secondary)}.attention-center{position:fixed;top:6px;right:18px;z-index:1000;padding:7px 12px;border:1px solid #8885;border-radius:9px;background:var(--ds-bg-layer-2,#20232c);font-size:12px}.attention-center[open]{min-width:260px;max-width:85vw}.attention-items{display:grid;gap:8px;max-height:45vh;overflow:auto}.attention-items a{padding:9px;border:1px solid #8884;border-radius:6px}.hi-card:target{outline:2px solid #a695ff;outline-offset:4px}.math-source[data-display=true]{display:block}`;document.head.append(style);
function render(){
 for(const node of document.querySelectorAll('.math-source:not([data-rendered]),.markdown code.language-math:not([data-rendered]),.markdown code.language-latex:not([data-rendered])')){
  if(!window.katex)continue;node.dataset.rendered='1';
  try{window.katex.render(node.textContent,node,{displayMode:node.dataset.display==='true'||node.tagName==='CODE',throwOnError:false,trust:false,maxExpand:200,maxSize:20,strict:'ignore'});}catch{}
 }
 for(const node of document.querySelectorAll('.markdown pre code:not([data-highlighted]):not(.language-math):not(.language-latex)')){
  if(window.hljs&&node.textContent.length<100000)try{window.hljs.highlightElement(node);}catch{}
 }
}
function timers(){
 for(const e of document.querySelectorAll('[data-start][data-end]')){
  const start=Number(e.dataset.start),end=Number(e.dataset.end)||Date.now()/1000,seconds=Math.max(0,Math.floor(end-start));
  e.textContent='本轮耗时 · '+Math.floor(seconds/60)+':'+String(seconds%60).padStart(2,'0');
  e.title='墙钟时间，包含模型、工具及等待用户的时间';
 }
}
const center=document.createElement('details');center.className='attention-center';const summary=document.createElement('summary');summary.textContent='待处理 · 0';center.append(summary);
const enable=document.createElement('button');enable.textContent='开启系统通知';center.append(enable);
enable.onclick=async()=>{
 if(!('Notification' in window)){enable.textContent='浏览器不支持；页面提醒仍可用';return;}
 try{const p=await Notification.requestPermission();localStorage.setItem('agent-notifications',p==='granted'?'yes':'no');enable.textContent=p==='granted'?'系统通知已开启':'未获授权，请检查浏览器设置';}catch{enable.textContent='通知不可用，使用页面提醒';}
};
const items=document.createElement('div');items.className='attention-items';center.append(items);document.body.append(center);
let remembered=[];try{remembered=JSON.parse(sessionStorage.getItem('agent-attention-seen')||'[]');}catch{}
const seen=new Set(remembered);
if(!document.getElementById('scroll')){center.style.top='auto';center.style.bottom='16px';}
let busy=false,failures=0,timer;
function jump(){
 if(!location.hash.startsWith('#input-'))return;
 const id=location.hash.slice(7);const card=[...document.querySelectorAll('.hi-card')].find(x=>x.dataset.questionId===id);
 if(card&&!card.dataset.jumped){card.id='input-'+id;card.dataset.jumped='1';card.open=true;card.scrollIntoView({block:'center'});}
}
async function poll(){
 if(busy)return;busy=true;
 try{
  const response=await fetch('/api/attention',{signal:AbortSignal.timeout(15000)});if(!response.ok)throw Error();
  const data=await response.json();failures=0;summary.textContent='待处理 · '+data.pending.length;
  for(const state of data.sessions||[]){
   const row=[...document.querySelectorAll('.conversation-row')].find(r=>r.dataset.session===state.id);const label=row?.querySelector('.sub');
   if(label){const dot=document.createElement('i');dot.className='dot '+({running:'run',done:'ok',failed:'bad'}[state.status]||'idle');label.replaceChildren(dot,document.createTextNode(state.status+' · '+state.model_calls+' calls · $'+Number(state.usd||0).toFixed(3)));}
  }
  items.replaceChildren();
  for(const q of data.pending){
   const a=document.createElement('a');a.href='/agent?session='+encodeURIComponent(q.session)+'#input-'+encodeURIComponent(q.id);a.textContent=(q.kind==='approval'?'操作授权':'需要回答')+' · '+q.session;items.append(a);
   if(!seen.has(q.id)){
    seen.add(q.id);
    if('Notification' in window&&Notification.permission==='granted'&&localStorage.getItem('agent-notifications')==='yes')try{
     const n=new Notification('Agent4Learning',{body:'有任务需要你的回答或授权',tag:q.id});n.onclick=()=>{window.focus();location.href=a.href;n.close();};
    }catch{}
   }
  }
  sessionStorage.setItem('agent-attention-seen',JSON.stringify([...seen].slice(-500)));
  timer=setTimeout(poll,4000);
 }catch{
  failures++;summary.textContent='提醒连接中断 · '+Math.min(failures,5)+'/5';
  if(failures<=5)timer=setTimeout(poll,Math.min(16000,1000*2**(failures-1)));
  else{items.replaceChildren();const retry=document.createElement('button');retry.textContent='重新连接提醒';retry.onclick=()=>{failures=0;poll();};items.append(retry);}
 }finally{busy=false;}
}
let scheduled=false;
new MutationObserver(()=>{if(scheduled)return;scheduled=true;requestAnimationFrame(()=>{scheduled=false;render();jump();});}).observe(document.getElementById('scroll')||document.body,{childList:true,subtree:true});
window.addEventListener('agent-timeline-updated',render);window.addEventListener('hashchange',jump);
render();timers();setInterval(timers,1000);poll();
window.addEventListener('pagehide',()=>clearTimeout(timer),{once:true});
})();