"""Local UI translations. Conversation and artifact content is never translated.

The catalog is inert data; translations are assigned with textContent/nodeValue,
never innerHTML. A language preference applies to chrome, not model requests.
"""
from functools import lru_cache
import json
from pathlib import Path


@lru_cache(maxsize=1)
def assets():
    path = Path(__file__).with_name('ui_catalog.json')
    catalog = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    catalog.update({'编码 Agent': 'Agent', '生产级 Agent 平台': 'Agent engineering lab',
                    '复制代码': 'Copy code', '已复制': 'Copied', '复制失败': 'Copy failed',
                    'Projects · 项目': 'Projects', 'Chats · 普通对话': 'Chats'})
    data = json.dumps(catalog, ensure_ascii=True).replace('<', '\\u003c')
    return '<style>.language-picker{margin-left:auto;font:12px inherit;padding:4px 6px;max-width:105px;border:1px solid var(--border,#384152);border-radius:6px;background:var(--panel,#161a22);color:inherit}.side-h .nm{flex:1}</style><script>' + SCRIPT.replace('__CATALOG__', data) + '</script>'


SCRIPT = r'''
(()=>{'use strict';
 const catalog=__CATALOG__, originals=new WeakMap(), attrOriginals=new WeakMap();
 const protectedSelector='[data-user-content],.say,.step-body,.reply-source,.hi-question,.hi-answer,.hi-command,.hi-brief,.hi-options,.attachment-list,td,pre,code,textarea,script,style,[contenteditable="true"]';
 // Only interface containers are eligible. Table data, arbitrary document text,
 // session titles and file names are not implicitly part of the UI catalog.
 const uiSelector='nav,header,.side-h,.side-tools,.side-b .item,.grp,.tools-heading,.tools-grid,.conversation-views,button,label,summary,h1,h2,h3,.lead,.hero,.composer,.dock,.top,.panel,.field,.hint,.btn,.kv .k,th,.pill,.who,.execution-boundary,.reply-actions,.step-preview,.steps-preview,[data-ui],.add,.empty';
 let lang='en';try{lang=localStorage.getItem('agent-ui-language')==='zh'?'zh':'en'}catch(e){}
 function eligible(el){return el && !el.closest(protectedSelector) && (!el.closest('option')||el.closest('option').hasAttribute('value')) && !!el.closest(uiSelector)}
 function translate(value){
   if(lang!=='en')return value;
   let k=value.trim().replace(/\s+/g,' '),out=catalog[k];
   if(!out){
     let m;
     if(m=k.match(/^执行步骤 · (\d+) 步$/))out='Execution steps · '+m[1];
     else if(m=k.match(/^定位对话 · (\d+)$/))out='Jump to turn · '+m[1];
     else if(m=k.match(/^(\d+) 次模型调用 (.*)$/))out=m[1]+' model calls '+m[2];
   }
   return out?value.replace(value.trim(),out):value;
 }
 function apply(root=document.body){
   for(const el of root.querySelectorAll('[data-ui-turn]')){const text=(lang==='zh'?'第 '+el.dataset.uiTurn+' 轮 · ':'Turn '+el.dataset.uiTurn+' · ');if(el.textContent!==text)el.textContent=text}
   const walker=document.createTreeWalker(root,NodeFilter.SHOW_TEXT);let n;
   while(n=walker.nextNode()){
     if(!eligible(n.parentElement))continue;
     let state=originals.get(n);if(!state||n.nodeValue!==state.last)state={source:n.nodeValue};
     const next=translate(state.source);if(n.nodeValue!==next)n.nodeValue=next;
     state.last=next;originals.set(n,state);
   }
   const elements=[];if(root.nodeType===1)elements.push(root);
   elements.push(...root.querySelectorAll('[placeholder],[aria-label],[title]'));
   for(const el of elements){
     const inputHint=el.tagName==='TEXTAREA'&&!el.closest('[data-user-content],.say,.reply-source,.hi-question,.hi-answer,.hi-command');
     if(!inputHint&&(el.closest(protectedSelector)||!eligible(el)))continue;
     let states=attrOriginals.get(el)||{};
     for(const attr of ['placeholder','aria-label','title']){
       if(!el.hasAttribute(attr))continue;
       const value=el.getAttribute(attr);let state=states[attr];
       if(!state||value!==state.last)state={source:value};
       const next=translate(state.source);if(value!==next)el.setAttribute(attr,next);
       state.last=next;states[attr]=state;
     }attrOriginals.set(el,states);
   }
   // A code-copy button is UI even though its code block is protected.
   for(const el of root.querySelectorAll('button.code-copy,button.copy-code,[data-copy-code]')){
     if(!el.closest('[data-user-content],.say')&&!el.closest('pre'))continue;
     let state=originals.get(el);if(!state||el.textContent!==state.last)state={source:el.textContent};
     const next=translate(state.source);if(el.textContent!==next)el.textContent=next;
     state.last=next;originals.set(el,state);
   }
 }
 function setLanguage(value){lang=value==='zh'?'zh':'en';try{localStorage.setItem('agent-ui-language',lang)}catch(e){}
   document.documentElement.lang=lang==='zh'?'zh-CN':'en';apply();
   const picker=document.getElementById('ui-language');if(picker)picker.value=lang;
   document.dispatchEvent(new CustomEvent('agent-language-change',{detail:lang}));
 }
 window.AgentI18n={t:translate,setLanguage,get language(){return lang},refresh:apply};
 function init(){
   const target=document.querySelector('.side-h,.hd');
   if(target&&!document.getElementById('ui-language')){
     const picker=document.createElement('select');picker.id='ui-language';picker.className='language-picker';picker.setAttribute('aria-label','Interface language');
     for(const [value,label] of [['en','English'],['zh','中文']]){const o=document.createElement('option');o.value=value;o.textContent=label;picker.appendChild(o)}
     picker.value=lang;picker.addEventListener('change',()=>setLanguage(picker.value));target.appendChild(picker);
   }
   setLanguage(lang);let scheduled=false;
   new MutationObserver(()=>{if(scheduled)return;scheduled=true;requestAnimationFrame(()=>{scheduled=false;apply()})})
     .observe(document.body,{childList:true,subtree:true,characterData:true,attributes:true,attributeFilter:['title','placeholder','aria-label']});
 }
 if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();
})();
'''
