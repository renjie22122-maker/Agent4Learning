"""Project-first UX with a local folder chooser and automatic internal aliases."""
import html,json,os,re
from pathlib import Path
from . import ui
from .workspace_picker import PICKER_JS, STYLE
E=lambda value:html.escape(str(value),quote=True)

def render(demo,qs):
    groups=demo.ws_mgr.groups;key=qs.get('project','');group=groups.get(key);cards=[]
    for identifier,item in groups.items():
        folders=list(item['folders'].values());extra=f' · 另有 {len(folders)-1} 个文件夹' if len(folders)>1 else ''
        cards.append(f'''<article class="project-card"><h2>▣ {E(item['name'])}</h2><p class="folder-path">{E(folders[0])}</p><p class=mut>本机项目{extra}</p>
        <div class=project-actions><a class="btn pri" href="/agent?new=1&amp;project={E(identifier)}">新建对话</a><a class=btn href="/workspaces?project={E(identifier)}">项目设置</a></div></article>''')
    initial=list(group['folders'].values()) if group else ([qs['folder']] if qs.get('folder') else [])
    payload=E(json.dumps(initial,ensure_ascii=False))
    editor=f'''<section id="project-editor" class="project-editor" data-folders="{payload}">
      <h2>{'项目设置' if group else '添加项目'}</h2><p class=mut>{'管理项目名称和关联文件夹。' if group else '选择本机的项目文件夹，随后就能在项目下开始对话。'}</p>
      <form id="project-form" method="post" action="/workspaces/save">
        <input type="hidden" name="token" value="{E(demo.permissions_token)}"><input type="hidden" name="id" value="{E(key if group else '')}">
        <input type="hidden" name="folders_json" value="{payload}">
        <label for="project-paths">文件夹路径（每行一个，第一行为主文件夹）</label><textarea id="project-paths" rows="3" placeholder="粘贴一个或多个完整文件夹路径"></textarea>
        <div class=project-actions><button type="button" id="native-folders">打开文件管理器选择（可多选）</button><button type="button" id="choose-main">{'更换主文件夹 / 浏览目录' if initial else '浏览目录…'}</button></div><div id="project-folders"></div>
        <p><label>项目名称 <span class=mut>（默认使用文件夹名称）</span><input id="project-name" name="name" maxlength="80" value="{E(group['name'] if group else '')}" placeholder="选择文件夹后自动填写"></label></p>
        <button type="button" id="add-folder">＋ 添加关联文件夹</button>
        <p class=mut>新对话可访问全部关联文件夹。已有对话保持原来的目录范围；修改配置不会移动或删除磁盘文件。</p>
        <p id="project-error" role="alert"></p><div class=project-actions><button type="submit" class=pri>{'保存设置' if group else '添加项目并开始对话'}</button><a href="/agent">返回对话</a></div>
      </form></section>'''
    body=f'''{STYLE}<header class=project-head><div><h1>项目</h1><p class=mut>一个项目对应一个主文件夹，对话都收在项目下面。</p></div><a class="btn pri" href="/workspaces?new=1#project-editor">＋ 添加项目</a></header>
    <p><a href="/agent">← 返回对话</a></p><p>{E(qs.get('notice',''))}</p>
    {editor if group or qs.get('new') or qs.get('folder') or not groups else ''}
    <div class=project-grid>{''.join(cards) if cards else '<p class=mut>添加第一个项目后，这里会显示项目卡片。</p>'}</div>
    <dialog id="folder-picker"><h2>选择本机文件夹</h2><div class=picker-location><input id="folder-location" aria-label="文件夹位置" placeholder="也可以粘贴完整路径"><button type=button id="folder-go">前往</button></div>
    <div class=project-actions><button type=button id="folder-home">磁盘与常用位置</button><button type=button id="folder-up">上一级</button></div>
    <div id="folder-list"></div><p id="picker-status" role="status"></p><div class=project-actions><button type=button id="folder-select" class=pri disabled>选择此文件夹</button><button type=button id="folder-cancel">取消</button></div></dialog>{PICKER_JS}'''
    return ui.page('项目','agent',body)

def authorize(demo,form):
    import secrets
    if not secrets.compare_digest(form.get('token',''),demo.permissions_token):raise PermissionError('invalid form token')

def browse(demo,form):
    authorize(demo,form);raw=form.get('path','').strip().strip('"')
    if not raw:
        roots=[Path(f'{letter}:/') for letter in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ' if Path(f'{letter}:/').exists()] if os.name=='nt' else [Path('/')]
        choices=[Path.home(),demo.ws_mgr.current,*roots,*(Path(p) for p in demo.ws_mgr.recent)]
        return {'path':'','parent':'','selectable':False,'reason':'打开一个文件夹后，点击“选择此文件夹”。','folders':[{'name':p.name or str(p),'path':str(p)} for p in dict.fromkeys(choices) if p.is_dir()]}
    path=Path(raw).expanduser().resolve(strict=True)
    if not path.is_dir():raise ValueError('请选择文件夹')
    directories=[]
    for entry in sorted(path.iterdir(),key=lambda p:p.name.casefold()):
        if entry.name.startswith('.') or entry.is_symlink():continue
        try:
            if entry.is_dir():directories.append({'name':entry.name,'path':str(entry)})
        except OSError:continue
        if len(directories)>=500:break
    try:demo.ws_mgr.validate_folders({'main':str(path)});reason=''
    except Exception as exc:reason=str(exc)
    return {'path':str(path),'parent':str(path.parent) if path.parent!=path else '', 'folders':directories,'selectable':not reason,'reason':reason or '此文件夹将成为项目的文件访问范围。'}

def mutate(demo,path,form):
    authorize(demo,form)
    if path=='/workspaces/select':demo.ws_mgr.select_group(form.get('id',''));return form['id']
    if path!='/workspaces/save':raise ValueError('未知项目操作')
    key=form.get('id','');old=demo.ws_mgr.groups.get(key,{})
    if 'folders_json' in form:
        paths=json.loads(form['folders_json'])
        if not isinstance(paths,list) or not 1<=len(paths)<=16 or any(not isinstance(p,str) for p in paths):raise ValueError('请选择 1 到 16 个文件夹')
        old_aliases={os.path.normcase(str(Path(v).resolve())):k for k,v in old.get('folders',{}).items()};folders={}
        for index,value in enumerate(paths):
            folder=demo.ws_mgr.resolve(value);alias=old_aliases.get(os.path.normcase(str(folder)))
            if not alias:alias='main' if index==0 else re.sub('[^a-zA-Z0-9_-]','_',folder.name).strip('_') or 'folder'
            if not alias[0].isalpha() or not alias[0].isascii():alias='folder_'+alias
            alias=alias[:26];base=alias;number=2
            while alias in folders:alias=f'{base}_{number}';number+=1
            folders[alias]=str(folder)
        name=form.get('name','').strip() or Path(paths[0]).name
    else:
        folders={}
        for line in form.get('folders','').splitlines():
            if not line.strip():continue
            alias,sep,value=line.partition('=');alias=alias.strip()
            if not sep or alias in folders:raise ValueError('文件夹格式无效')
            folders[alias]=value.strip()
        name=form.get('name','')
    return demo.ws_mgr.save_group(name,folders,key)
