ASSETS = r'''
<style>
.reply-actions{display:flex;align-items:center;flex-wrap:wrap;gap:6px;margin:-12px 0 28px}.reply-actions button,.copy-code{border:1px solid #34445b;border-radius:7px;padding:6px 9px;background:#192436;color:#b8c8e0;font-size:12px;cursor:pointer}.reply-actions button:hover,.reply-actions button[aria-pressed=true]{background:#314a76;color:#fff}.reply-notice{color:#afc8ef;font-size:12px}.branch-notice{padding:12px 14px;background:#1a2941;border:1px solid #3c5278;border-radius:10px;margin-bottom:20px;font-size:12px}.markdown pre{position:relative;padding-top:45px}.copy-code{position:absolute;top:8px;right:8px}.feedback-dialog{background:#1c2637;color:#e1e9f7;border:1px solid #4d6080;border-radius:14px;max-width:440px;width:85vw;padding:20px}.feedback-dialog::backdrop{background:#0009}.feedback-dialog textarea{width:100%;min-height:100px;margin:12px 0;background:#101827;color:#fff;border:1px solid #4d6080;padding:10px;box-sizing:border-box}.feedback-dialog button{padding:8px 14px;margin:6px;cursor:pointer}.feedback-dialog select{padding:8px;background:#152038;color:#fff}
</style>
<script>
(()=>{
const column=document.querySelector('#scroll .col'),sid=document.querySelector('#sendform [name=session]')?.value;
if(!column||!sid)return;
let votes={};const pending=new Set();
async function post(path,fields){const token=document.querySelector('meta[name="conversation-token"]')?.content||'';const r=await fetch(path,{method:'POST',body:new URLSearchParams({session:sid,token,...fields})});const data=await r.json();if(!r.ok)throw Error(data.error||'操作失败');return data;}
async function copy(text){try{await navigator.clipboard.writeText(text);}catch(error){const area=document.createElement('textarea');area.value=text;area.style.position='fixed';area.style.opacity='0';document.body.append(area);area.select();const ok=document.execCommand('copy');area.remove();if(!ok)throw Error('浏览器未允许复制，请手动选择文本复制');}}
function paint(){
 column.querySelectorAll('.reply-actions').forEach(row=>{const vote=votes[row.dataset.replyTurn]?.vote||'';row.querySelectorAll('[aria-pressed]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.replyAction===vote)));});
 column.querySelectorAll('.markdown pre').forEach(pre=>{if(pre.querySelector('.copy-code'))return;const code=pre.querySelector('code');const text=code?code.textContent:pre.textContent;const button=document.createElement('button');button.type='button';button.className='copy-code';button.textContent='复制代码';button.onclick=async()=>{try{await copy(text);button.textContent='已复制';}catch(e){button.textContent=e.message;}};pre.prepend(button);});
}
function reasonDialog(existing){return new Promise(resolve=>{
 const dialog=document.createElement('dialog');dialog.className='feedback-dialog';
 dialog.innerHTML='<form method="dialog"><h3>回复反馈</h3><p>仅保存在本机，可更改或撤销。</p><select aria-label="回复评分"><option value="down">不满意</option><option value="up">满意</option><option value="">撤销评分</option></select><textarea maxlength="2000" aria-label="反馈原因" placeholder="哪里有帮助，或哪里需要改进？（可选）"></textarea><div><button value="cancel">取消</button><button value="save">保存反馈</button></div></form>';
 dialog.querySelector('select').value=existing?.vote||'down';dialog.querySelector('textarea').value=existing?.reason||'';
 dialog.addEventListener('close',()=>{const result=dialog.returnValue==='save'?{vote:dialog.querySelector('select').value,reason:dialog.querySelector('textarea').value}:null;dialog.remove();resolve(result);},{once:true});document.body.append(dialog);dialog.showModal();
});}
function branchDialog(row,turn){return new Promise(resolve=>{
 const dialog=document.createElement('dialog');dialog.className='feedback-dialog';
 dialog.innerHTML='<form method="dialog"><h3>从第 '+turn+' 轮创建分支</h3><p>只继承到这一轮的对话，后续消息不会带入。文件默认独立，原会话保持不变。</p><select aria-label="分支文件版本"></select><p>历史快照与独立副本不含 Git 元数据、依赖目录和运行缓存；这不会创建 Git commit，也不会自动合并修改。</p><div><button value="cancel">取消</button><button value="create">创建分支</button></div></form>';
 const select=dialog.querySelector('select');
 if(row.dataset.version==='true')select.add(new Option('所选轮次的文件快照（独立版本）','snapshot'));
 select.add(new Option('当前文件的独立副本（不是历史版本）','current'));
 if(row.dataset.files==='project')select.add(new Option('共享当前项目文件（修改相互影响）','shared'));
 dialog.addEventListener('close',()=>{const mode=dialog.returnValue==='create'?select.value:null;dialog.remove();resolve(mode);},{once:true});document.body.append(dialog);dialog.showModal();
});}
column.addEventListener('click',async event=>{
 const button=event.target.closest('[data-reply-action]');if(!button)return;
 const row=button.closest('.reply-actions'),turn=row.dataset.replyTurn,action=button.dataset.replyAction,notice=row.querySelector('[role=status]');
 if(pending.has(turn))return;pending.add(turn);button.disabled=true;
 try{
  if(action==='copy'){await copy(row.querySelector('.reply-source').value);notice.textContent='回复已复制（Markdown）';}
  else if(action==='branch'){
   const mode=await branchDialog(row,turn);if(!mode)return;
   notice.textContent='正在创建分支…';const result=await post('/agent/branch',{turn,files:mode});location.href='/agent?session='+encodeURIComponent(result.session);
  }else{
   let value=action==='reason'?await reasonDialog(votes[turn]):{vote:votes[turn]?.vote===action?'':action,reason:votes[turn]?.reason||''};if(!value)return;
   votes[turn]=await post('/agent/reply-feedback',{turn,...value});paint();notice.textContent=value.vote?'反馈已保存到本机':'评分已撤销';
  }
 }catch(error){notice.textContent=error.message;}finally{pending.delete(turn);button.disabled=false;}
});
paint();new MutationObserver(paint).observe(column,{childList:true,subtree:true});
fetch('/api/reply-feedback?session='+encodeURIComponent(sid)).then(r=>{if(!r.ok)throw Error('反馈加载失败');return r.json();}).then(data=>{votes=data;paint();}).catch(()=>{});
})();
</script>
'''
