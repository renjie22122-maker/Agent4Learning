ATTACHMENT_JS=r'''
<style>
.attachment-bar{display:flex;align-items:center;gap:8px;padding:6px 12px;color:#aaa;font-size:12px}.attachment-add{background:transparent;border:1px solid #666;color:inherit;border-radius:6px;padding:3px 9px;cursor:pointer}
.attachment-list{display:flex;flex-wrap:wrap;gap:6px;padding:0 12px}.attachment-chip{max-width:100%;display:flex;align-items:center;gap:6px;background:#ffffff0b;border:1px solid #444;border-radius:7px;padding:5px 8px;font-size:12px}.attachment-chip img{width:38px;height:38px;object-fit:cover}.attachment-chip button{background:none;color:inherit;border:0;cursor:pointer}.attachment-chip.error{border-color:#e88787}.file-drop-active .box{outline:2px dashed #73a5ff}.message-attachments{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
</style>
<script>
(()=>{
 const form=document.getElementById('sendform');if(!form)return;
 const picker=document.getElementById('attachment-picker'),list=document.getElementById('attachment-list');
 const items=[];let depth=0;
 function render(){list.replaceChildren();for(const item of items){const chip=document.createElement('div');chip.className='attachment-chip'+(item.error?' error':'');
   if(item.preview){const img=document.createElement('img');img.src=item.preview;img.alt='图片预览';chip.append(img);}
   const label=document.createElement('span');label.textContent=item.file.name+' · '+(item.error|| (item.id?'已就绪':'上传并解析中…'));chip.append(label);
   chip.title=(item.warnings||[]).join('\n');const remove=document.createElement('button');remove.type='button';remove.textContent='×';remove.setAttribute('aria-label','移除 '+item.file.name);
   remove.onclick=()=>{items.splice(items.indexOf(item),1);if(item.preview)URL.revokeObjectURL(item.preview);render();};chip.append(remove);list.append(chip);
 }form.querySelector('input[name=attachments]').value=JSON.stringify(items.filter(i=>i.id).map(i=>i.id));}
 async function add(files){for(const file of files){if(items.length>=10){alert('每条消息最多 10 个附件');break;}
   const item={file};items.push(item);if(file.type.startsWith('image/'))item.preview=URL.createObjectURL(file);render();
   try{if(!file.size||file.size>25000000)throw new Error('文件为空或超过 25 MB');
     const data=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(String(reader.result).split(',')[1]);reader.onerror=()=>reject(new Error('文件读取失败'));reader.readAsDataURL(file);});
     const response=await fetch('/agent/attachments',{method:'POST',headers:{'Content-Type':'application/json','X-Form-Token':document.querySelector('meta[name="conversation-token"]').content},body:JSON.stringify({name:file.name,data})});
     const result=await response.json();if(!response.ok)throw new Error(result.error||'附件解析失败');Object.assign(item,result);
   }catch(error){item.error=error.message;}render();
 }}
 document.getElementById('attachment-add').onclick=()=>picker.click();picker.onchange=()=>{add([...picker.files]);picker.value='';};
 const isFiles=e=>[...(e.dataTransfer?.types||[])].includes('Files');
 form.addEventListener('dragenter',e=>{if(isFiles(e)){e.preventDefault();depth++;form.classList.add('file-drop-active');}});
 form.addEventListener('dragover',e=>{if(isFiles(e)){e.preventDefault();e.dataTransfer.dropEffect='copy';}});
 form.addEventListener('dragleave',e=>{if(isFiles(e)&&--depth<=0)form.classList.remove('file-drop-active');});
 form.addEventListener('drop',e=>{if(isFiles(e)){e.preventDefault();depth=0;form.classList.remove('file-drop-active');add([...e.dataTransfer.files]);}});
 document.addEventListener('dragover',e=>{if(isFiles(e))e.preventDefault();});document.addEventListener('drop',e=>{if(isFiles(e))e.preventDefault();});
 document.getElementById('m').addEventListener('paste',e=>{const files=[...(e.clipboardData?.files||[])];if(files.length){e.preventDefault();add(files);}});
 window.agentAttachments={ids:()=>items.filter(i=>i.id).map(i=>i.id),ready:()=>items.every(i=>i.id&&!i.error),clear:ids=>{for(let i=items.length-1;i>=0;i--)if(ids.includes(items[i].id)){if(items[i].preview)URL.revokeObjectURL(items[i].preview);items.splice(i,1);}render();}};
})();
</script>
'''
