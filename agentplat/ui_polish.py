"""Responsive conversation presentation and explicit continuation controls."""
ASSETS = r'''
<style>
.step-group>summary{min-width:0}.step-preview{display:block;margin:8px 0 2px 14px;color:#94a3b8;font-size:11px;font-weight:400;line-height:1.75;min-width:0;max-width:100%}.step-preview-line{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:100%}.step-preview-index{display:inline-block;min-width:2em;color:#708198;font-variant-numeric:tabular-nums}.step-group[open]>summary .step-preview{display:none}
body.chat{--ds-bg-base:#10141d;--ds-bg-layer-1:#19202c;--ds-bg-layer-2:#202a38;--ds-label-primary:#edf2fa;--ds-label-secondary:#c5d0df;--ds-label-tertiary:#a6b4c8;--ds-label-caption:#94a3b8;--ds-border-l2:#293445;--ds-brand:#526fe5;--ds-link:#a9bcff}
.chat .side{width:260px;background:#131925}.chat .side-h{padding:22px 18px 16px}.chat .side-h .nm{font-size:16px}
.chat .side-tools{padding:8px 14px 14px;border-bottom:1px solid #293445;max-height:38vh;overflow:auto}
.chat .tools-grid{gap:6px}.chat .tools-grid a{padding:8px;border-radius:8px;background:#1b2331}
.chat .side-b{padding:12px}.chat .item{padding:10px 12px;margin-bottom:4px}.chat .item.on{background:#283755;border:1px solid #405781}
.chat .top{min-height:58px;height:auto;padding:10px 20px;gap:10px}.chat .top-tools{width:auto!important;white-space:nowrap;padding:8px 12px;height:auto}
.chat .col{max-width:900px;padding:30px 28px}.chat .turn{margin-bottom:30px}.chat .who{margin-bottom:12px;font-size:13px}.chat .who .av{width:27px;height:27px;border-radius:9px}
.chat .say{font-size:15px;line-height:1.85}.chat .turn.u .say{background:#202c40;border:1px solid #344761;border-radius:16px 16px 5px 16px;padding:16px 20px}
.chat .trace{border-left:2px solid #34445e;padding-left:16px;margin:16px 0}.chat .trace .ln{padding:8px 10px;margin:4px 0;border-radius:8px;background:#171e2a;align-items:flex-start;flex-wrap:wrap}
.chat .trace .ln .v{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.65;min-width:0;flex:1 1 220px}.chat .trace .ln .k{font-size:12px}
.step-detail{margin:8px 0;border:1px solid #2b384d;border-radius:10px;background:#171f2c;overflow:hidden}.step-detail summary{padding:10px 12px;cursor:pointer;color:#cedaf0;font-size:13px;overflow-wrap:anywhere}.step-body{padding:0 14px 12px;white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.7 var(--ds-mono);color:#acbcd2}.step-detail.error{border-color:#854545}.step-detail.finish{border-color:#357059}
.turn-navigation{position:relative;flex-shrink:0;font-size:12px}.turn-navigation>summary{cursor:pointer;list-style:none;border:1px solid #354760;padding:8px 10px;border-radius:8px;color:#cedaf0;white-space:nowrap}.turn-navigation nav{position:absolute;right:0;top:40px;width:min(320px,80vw);max-height:55vh;overflow:auto;background:#1b2535;border:1px solid #40516c;border-radius:12px;padding:8px;z-index:65;box-shadow:0 12px 40px #0006}.turn-navigation nav a{display:block;padding:10px;border-radius:7px;line-height:1.6;color:#d5e0f3;text-decoration:none;overflow-wrap:anywhere}.turn-navigation nav a:hover,.turn-navigation nav a[aria-current]{background:#304466}.turn-navigation nav p{padding:8px;color:#a6b4c8}.turn[id]{scroll-margin-top:20px}
.chat .stats{display:block}.chat .panel:not(.open){visibility:hidden}.usage-details{margin-top:16px;padding:10px 14px;border:1px solid #293445;border-radius:10px;font-size:12px}.usage-details summary{cursor:pointer;color:#a6b4c8}.usage-details p{margin:8px 0;overflow-wrap:anywhere}
.chat .dock{border-top:1px solid #253044;background:#10141d;padding:10px 0 14px}.chat .dockin{max-width:900px;padding:0 28px}.chat .box{border:1px solid #3b4b63;border-radius:18px;padding:14px 16px;box-shadow:0 5px 24px #0002}.chat .box:focus-within{border-color:#829bff;box-shadow:0 0 0 3px #526fe522}.chat .box textarea{min-height:48px;font-size:15px;line-height:1.65}.chat .boxrow{flex-wrap:wrap}.chat .send{min-height:38px;padding:10px 20px}.chat .foot{line-height:1.6;font-size:11px}.chat .adv{font-size:12px}.chat .attachment-bar{font-size:11px;color:#94a3b8}
.quick-actions{display:flex;align-items:center;gap:12px;margin:0 0 10px;flex-wrap:wrap;font-size:12px}.quick-actions button,.quick-actions a{border:1px solid #354760;border-radius:9px;background:#1b2638;color:#dce7ff;padding:8px 12px;cursor:pointer;text-decoration:none}.quick-actions button:hover,.quick-actions a:hover{background:#293b58}.quick-actions [hidden]{display:none!important}#resume-feedback{color:#f0bd79;max-width:100%;overflow-wrap:anywhere}#quick-resume{border-color:#617fe5;background:#293d6a}
.chat :is(button,a,input,summary):focus-visible{outline:2px solid #9aafff;outline-offset:3px}.chat .nav-toggle{display:none}.nav-scrim{display:none}.chat pre{max-width:100%;overflow:auto}.chat table{max-width:100%}
@media(max-width:1100px){.chat .panel.open{position:fixed;right:0;top:0;bottom:0;width:min(92vw,392px);z-index:50;box-shadow:-16px 0 50px #0008}.chat .panel .pin{min-width:0}.panel-close{position:sticky;top:8px;float:right;z-index:1}}
@media(max-width:840px){.chat .nav-toggle{display:grid;flex-shrink:0}.chat .side{display:none}.chat.nav-open .side{display:flex;position:fixed;left:0;top:0;bottom:0;z-index:80;width:min(85vw,300px);box-shadow:20px 0 60px #0009}.chat.nav-open .nav-scrim{display:block;position:fixed;inset:0;background:#0008;border:0;z-index:70}.chat .top{padding:8px 12px;gap:7px}.chat .top .wschip{display:none}.chat .top .ttl{white-space:nowrap;font-size:13px}.chat .top-tools{font-size:12px}.chat .col{padding:22px 16px}.chat .dockin{padding:0 12px}.chat .foot{max-height:38px;overflow:auto}.chat .quick-actions{gap:7px}.chat .side-tools{max-height:40vh}.chat .boxrow>a{display:none}}
@media(prefers-reduced-motion:reduce){.chat *{scroll-behavior:auto!important;transition:none!important}}
</style>
<script>
(()=>{
const nav=document.querySelector('.nav-toggle');
const scrim=document.createElement('button');scrim.className='nav-scrim';scrim.setAttribute('aria-label','关闭会话列表');document.body.append(scrim);
const toggle=open=>{document.body.classList.toggle('nav-open',open);nav?.setAttribute('aria-expanded',String(open));if(!open)nav?.focus();};
nav?.addEventListener('click',()=>toggle(!document.body.classList.contains('nav-open')));scrim.onclick=()=>toggle(false);
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&document.body.classList.contains('nav-open'))toggle(false);});
const panel=document.querySelector('.panel.open');if(panel){const close=document.createElement('button');close.className='panel-close';close.textContent='关闭详情 ×';close.onclick=()=>{panel.classList.remove('open');};panel.prepend(close);}
const resume=document.getElementById('quick-resume'),stop=document.getElementById('quick-stop'),feedback=document.getElementById('resume-feedback');
const turnMenu=document.getElementById('turn-navigation'),scroll=document.getElementById('scroll');
let turnKey='';
function updateTurns(){
 if(!turnMenu||!scroll)return;
 const turns=[...scroll.querySelectorAll('.turn.u[data-turn]')];
 const rows=turns.map(el=>({id:el.id,n:el.dataset.turn,text:el.querySelector('.say')?.textContent?.trim()||'未命名提问'}));
 const key=JSON.stringify(rows);if(key===turnKey)return;turnKey=key;
 const list=turnMenu.querySelector('nav');list.replaceChildren();
 turnMenu.querySelector('summary').textContent='定位对话'+(rows.length?' · '+rows.length:'');
 for(const row of rows){const link=document.createElement('a');link.href='#'+row.id;link.title=row.text;link.textContent='第 '+row.n+' 轮 · '+row.text.slice(0,52);list.append(link);}
 if(!rows.length){const empty=document.createElement('p');empty.textContent='发送消息后可定位每一轮对话';list.append(empty);}
 const latest=document.createElement('a');latest.href='#latest-message';latest.textContent='↓ 回到最新消息';list.append(latest);
}
turnMenu?.addEventListener('click',event=>{
 const link=event.target.closest('nav a');if(!link)return;event.preventDefault();
 const id=link.getAttribute('href').slice(1),target=document.getElementById(id);
 if(id==='latest-message')scroll.scrollTop=scroll.scrollHeight;
 else if(target)scroll.scrollTop+=target.getBoundingClientRect().top-scroll.getBoundingClientRect().top-20;
 turnMenu.querySelectorAll('[aria-current]').forEach(x=>x.removeAttribute('aria-current'));link.setAttribute('aria-current','true');turnMenu.open=false;
});
turnMenu?.addEventListener('keydown',event=>{if(event.key==='Escape'){turnMenu.open=false;turnMenu.querySelector('summary').focus();}});
updateTurns();const column=scroll?.querySelector('.col');if(column)new MutationObserver(updateTurns).observe(column,{childList:true,subtree:true});
window.addEventListener('agent-status',e=>{if(resume)resume.hidden=!e.detail.session||['running','done'].includes(e.detail.status);if(stop){stop.hidden=e.detail.status!=='running';stop.href='/agent/stop?session='+encodeURIComponent(e.detail.session);}});
resume?.addEventListener('click',async()=>{
if(resume.disabled)return;resume.disabled=true;feedback.textContent='正在核对记录…';
try{
const session=resume.closest('[data-session]').dataset.session;
const token=document.querySelector('meta[name="conversation-token"]')?.content||'';
const response=await fetch('/agent/continue',{method:'POST',body:new URLSearchParams({session,token})});
const result=await response.json();if(!response.ok)throw Error(result.error||'未能恢复任务');
feedback.textContent='已继续，正在更新进度';resume.hidden=true;if(stop)stop.hidden=false;
const form=document.getElementById('sendform');if(form){form.dataset.running='true';form.action='/agent/chat';form.querySelector('textarea').name='message';}
window.dispatchEvent(new CustomEvent('agent-resumed',{detail:{session:result.session}}));
}catch(error){feedback.textContent=error.message;}finally{resume.disabled=false;}
});
})();
</script>
'''
