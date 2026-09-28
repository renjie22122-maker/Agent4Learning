MENU_JS = r'''
<style>
.tools-direct{display:block!important;padding:0!important;margin:0!important;border:0!important}.tools-heading{white-space:nowrap}.tools-grid a{white-space:nowrap}
.side-f,.side-tools{flex-shrink:0}.side-tools{padding:0 10px 10px;border-bottom:1px solid #ffffff14}.tools-heading{font-weight:600;padding:8px 4px}.tools-grid{display:grid;grid-template-columns:1fr 1fr;gap:3px}.tools-grid a{padding:6px 4px;font-size:12px;border-radius:5px}.tools-grid a:hover{background:#ffffff12}.conversation-views{display:flex;justify-content:space-around;padding:8px 0;font-size:11px}.top-tools{display:none}@media(max-width:840px){.top-tools{display:block}}
.conversation-row{position:relative}.conversation-row>a{padding-right:30px!important}
.conversation-menu-button{position:absolute;right:3px;top:6px;background:transparent;color:inherit;border:0;cursor:pointer;font-size:19px;padding:1px 6px}
.conversation-context{position:fixed;z-index:10000;width:218px;padding:6px;background:#242424;border:1px solid #555;border-radius:10px;box-shadow:0 8px 30px #0009;color:#eee}
.conversation-context button{display:block;width:100%;text-align:left;background:transparent;border:0;border-radius:5px;color:inherit;padding:8px 12px;cursor:pointer;font:inherit}
.conversation-context button:hover,.conversation-context button:focus{background:#424242;outline:none}
.conversation-context .danger{color:#ff9d9d}.conversation-dialog{background:#242424;color:#eee;border:1px solid #555;border-radius:12px;max-width:440px;padding:22px}
.conversation-dialog input,.conversation-dialog select{box-sizing:border-box;width:100%;padding:8px;margin:12px 0;background:#151515;color:#eee;border:1px solid #777}
</style>
<script>
(()=>{
 let menu=null;
 const close=()=>{menu?.remove();menu=null;};
 function notice(text){const el=document.createElement('div');el.role='status';el.textContent=text;el.style.cssText='position:fixed;bottom:24px;right:24px;background:#333;color:#fff;padding:12px 18px;border-radius:8px;z-index:10001';document.body.append(el);setTimeout(()=>el.remove(),2500);}
 function edit(title,initial,choices){return new Promise(resolve=>{
   const dialog=document.createElement('dialog');dialog.className='conversation-dialog';
   const heading=document.createElement('h3');heading.textContent=title;dialog.append(heading);
   const field=document.createElement(choices?'select':'input');
   if(choices){for(const [value,label] of Object.entries(choices)){const o=document.createElement('option');o.value=value;o.textContent=label;field.append(o);}
     const note=document.createElement('p');note.textContent='只调整会话分类，原文件访问范围保持不变。要使用新项目的目录，请在该项目中新建对话。';dialog.append(note);
   }else{field.value=initial;field.maxLength=120;}
   dialog.append(field);const cancel=document.createElement('button');cancel.textContent='取消';
   const save=document.createElement('button');save.textContent='保存';dialog.append(cancel,save);
   const finish=value=>{dialog.close();dialog.remove();resolve(value);};
   cancel.onclick=()=>finish(null);save.onclick=()=>finish(field.value);
   dialog.addEventListener('cancel',e=>{e.preventDefault();finish(null);});
   field.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();finish(field.value);}});
   document.body.append(dialog);dialog.showModal();field.focus();if(!choices)field.select();
 });}
 async function mutate(row,action,value){
   const response=await fetch('/agent/conversation',{method:'POST',headers:{Accept:'application/json'},body:new URLSearchParams({session:row.dataset.session,action,value:String(value),token:document.querySelector('meta[name="conversation-token"]').content})});
   const result=await response.json();if(!response.ok)throw new Error(result.error||'操作失败');
   if(action==='deleted'&&value&&new URL(location.href).searchParams.get('session')===row.dataset.session){location.href='/agent?new=1&project=__general__';return;}
   const page=await fetch(location.href);if(!page.ok)throw new Error('操作已保存，但列表刷新失败，请手动刷新');
   const doc=new DOMParser().parseFromString(await page.text(),'text/html');
   const next=doc.querySelector('aside.side');if(next){const top=document.querySelector('.side-b').scrollTop;document.querySelector('aside.side').replaceWith(next);next.querySelector('.side-b').scrollTop=top;}notice('会话设置已保存');
 }
 function show(row,x,y){
   close();const state=JSON.parse(row.dataset.state);menu=document.createElement('div');menu.className='conversation-context';menu.role='menu';
   const add=(label,fn,danger=false)=>{const b=document.createElement('button');b.type='button';b.role='menuitem';b.textContent=label;if(danger)b.className='danger';
     b.onclick=async()=>{close();try{await fn();}catch(e){alert(e.message);}};menu.append(b);};
   add('在新标签页打开',()=>window.open('/agent?session='+encodeURIComponent(row.dataset.session),'_blank','noopener'));
   add('复制对话链接',async()=>{const link=new URL('/agent?session='+encodeURIComponent(row.dataset.session),location.origin).href;await navigator.clipboard.writeText(link);notice('对话链接已复制');});
   add('重命名',async()=>{const value=await edit('重命名对话',row.dataset.title);if(value!==null)await mutate(row,'rename',value);});
   add(state.pinned?'取消置顶':'置顶',()=>mutate(row,'pinned',!state.pinned));
   add(state.unread?'标记为已读':'标记为未读',()=>mutate(row,'unread',!state.unread));
   add('移动到项目 / 普通对话',async()=>{const projects=JSON.parse(document.querySelector('aside.side').dataset.projects);const value=await edit('移动会话分类','',{'__general__':'Chats · 普通对话',...projects});if(value!==null)await mutate(row,'move',value);});
   add('导出 Markdown',()=>{location.href='/agent/export?session='+encodeURIComponent(row.dataset.session);});
   add(state.archived?'取消归档':'归档',()=>mutate(row,'archived',!state.archived));
   add(state.deleted?'从回收站恢复':'移入回收站',async()=>{if(state.deleted||confirm('将此对话移入回收站？可恢复，不会删除日志或项目文件。'))await mutate(row,'deleted',!state.deleted);},!state.deleted);
   document.body.append(menu);menu.style.left=Math.max(8,Math.min(x,innerWidth-menu.offsetWidth-8))+'px';menu.style.top=Math.max(8,Math.min(y,innerHeight-menu.offsetHeight-8))+'px';menu.firstElementChild.focus();
 }
 document.addEventListener('contextmenu',e=>{const row=e.target.closest('.conversation-row');if(row){e.preventDefault();show(row,e.clientX,e.clientY);}});
 document.addEventListener('click',e=>{document.querySelectorAll('.tools-menu[open]').forEach(d=>{if(!d.contains(e.target))d.open=false;});const button=e.target.closest('.conversation-menu-button');if(button){e.preventDefault();const rect=button.getBoundingClientRect();show(button.closest('.conversation-row'),rect.left,rect.bottom);}else if(menu&&!menu.contains(e.target))close();});
 document.addEventListener('keydown',e=>{if(e.key==='Escape')close();if(menu&&['ArrowDown','ArrowUp'].includes(e.key)){e.preventDefault();const buttons=[...menu.querySelectorAll('button')];buttons[(buttons.indexOf(document.activeElement)+(e.key==='ArrowDown'?1:-1)+buttons.length)%buttons.length].focus();}});
 window.addEventListener('resize',close);
})();
</script>
'''
