STYLE='''<style>
.project-head{display:flex;justify-content:space-between;align-items:center;gap:20px}.project-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px;margin:22px 0}.project-card,.project-editor{border:1px solid #ffffff24;border-radius:12px;padding:22px;background:#191919}.project-card h2{margin-top:0}.folder-path{overflow-wrap:anywhere;font-family:monospace;font-size:12px}.project-actions{display:flex;align-items:center;gap:14px;flex-wrap:wrap}.project-editor{max-width:780px;margin:24px auto}.project-editor input:not([type=hidden]),#project-paths{display:block;width:95%;margin-top:8px}.project-folder{display:flex;align-items:center;gap:12px;border:1px solid #ffffff20;border-radius:8px;padding:12px;margin:10px 0}.project-folder span{flex:1;overflow-wrap:anywhere}#project-error{color:#ffaaaa}
#folder-picker{background:#202020;color:#eee;border:1px solid #555;border-radius:12px;width:min(680px,90vw);padding:22px}#folder-picker::backdrop{background:#0009}.picker-location{display:flex;gap:8px;margin:14px 0}.picker-location input{flex:1;min-width:0}#folder-list{height:320px;overflow:auto;border:1px solid #444;padding:5px;margin:12px 0}#folder-list button{display:block;width:100%;text-align:left;background:transparent;color:inherit;border:0;border-radius:5px;padding:10px}#folder-list button:hover{background:#ffffff15}#picker-status{font-size:12px;color:#bbb;overflow-wrap:anywhere}
</style>'''

PICKER_JS=r'''<script>
(()=>{
 const editor=document.getElementById('project-editor');if(!editor)return;
 const form=document.getElementById('project-form'),name=document.getElementById('project-name'),error=document.getElementById('project-error');
 let folders=JSON.parse(editor.dataset.folders),selection=null,revision=0,target='main';
 const paths=document.getElementById('project-paths');const dialog=document.getElementById('folder-picker');
 const token=form.querySelector('[name=token]').value;
 async function request(url,values){const response=await fetch(url,{method:'POST',headers:{Accept:'application/json'},body:new URLSearchParams({token,...values})});const result=await response.json();if(!response.ok)throw new Error(result.error||'操作失败');return result;}
 function redraw(sync=true){const list=document.getElementById('project-folders');list.replaceChildren();folders.forEach((path,index)=>{const row=document.createElement('div');row.className='project-folder';const label=document.createElement('span');label.textContent=(index===0?'主文件夹 · ':'关联文件夹 · ')+path;row.append(label);if(index){const remove=document.createElement('button');remove.type='button';remove.textContent='移除';remove.onclick=()=>{folders.splice(index,1);redraw();};row.append(remove);}list.append(row);});
   form.querySelector('[name=folders_json]').value=JSON.stringify(folders);form.querySelector('[type=submit]').disabled=!folders.length;
   if(sync)paths.value=folders.join('\n');if(!name.value&&folders[0])name.value=folders[0].replace(/[\\/]+$/,'').split(/[\\/]/).pop();
 }
 function use(values,mode){if(!values.length)return;if(mode==='main')folders=[...values,...folders.slice(1).filter(p=>!values.includes(p))];else folders=[...folders,...values.filter(p=>!folders.includes(p))];redraw();}
 async function browse(path){const ticket=++revision;selection=null;document.getElementById('folder-select').disabled=true;document.getElementById('picker-status').textContent='正在读取…';
  try{const data=await request('/workspaces/browse',{path});if(ticket!==revision)return;selection=data;document.getElementById('folder-location').value=data.path;document.getElementById('picker-status').textContent=data.reason;document.getElementById('folder-select').disabled=!data.selectable;
   const list=document.getElementById('folder-list');list.replaceChildren();for(const folder of data.folders){const b=document.createElement('button');b.type='button';b.textContent='▣ '+folder.name+'  ›';b.title=folder.path;b.onclick=()=>browse(folder.path);list.append(b);}if(!data.folders.length)list.textContent='此目录中没有子文件夹，可以直接选择当前目录。';
  }catch(e){if(ticket===revision)document.getElementById('picker-status').textContent=e.message;}
 }
 function open(mode){target=mode;dialog.showModal();browse(mode==='main'&&folders[0]?folders[0]:'');}
 document.getElementById('choose-main').onclick=()=>open('main');document.getElementById('add-folder').onclick=()=>open('extra');
 document.getElementById('native-folders').onclick=async event=>{const button=event.target;button.disabled=true;error.textContent='请在系统窗口选择文件夹，可按 Ctrl / Shift 多选。';try{const result=await request('/workspaces/pick',{});use(result.paths,folders.length?'extra':'main');error.textContent='';}catch(e){error.textContent=e.message+'；也可以直接填写路径或使用“浏览目录”。';}finally{button.disabled=false;}};
 paths.addEventListener('input',()=>{folders=paths.value.split(/\r?\n/).map(p=>p.trim().replace(/^"|"$/g,'')).filter(Boolean);redraw(false);});
 document.getElementById('folder-go').onclick=()=>browse(document.getElementById('folder-location').value);
 document.getElementById('folder-location').onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();browse(e.target.value);}};
 document.getElementById('folder-home').onclick=()=>browse('');document.getElementById('folder-up').onclick=()=>browse(selection?.parent||'');
 document.getElementById('folder-cancel').onclick=()=>dialog.close();document.getElementById('folder-select').onclick=()=>{if(selection?.selectable){use([selection.path],target);dialog.close();}};
 form.onsubmit=async e=>{e.preventDefault();error.textContent='';const button=form.querySelector('[type=submit]');button.disabled=true;try{const result=await request(form.action,Object.fromEntries(new FormData(form)));location.href=result.redirect;}catch(e){error.textContent=e.message;button.disabled=false;}};
 redraw();
})();</script>'''
