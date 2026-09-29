"""Host-only skill package import. Copy data; never execute install hooks."""
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
import uuid
import zipfile
from . import plugins

ROOT = Path(__file__).resolve().parents[1] / '.agent-runtime' / 'skills'
LOCK = threading.RLock()


def config():
    return json.loads(plugins.CONFIG.read_text(encoding='utf-8')) if plugins.CONFIG.exists() else {'manifests':[]}


def save(value):
    plugins.CONFIG.parent.mkdir(parents=True, exist_ok=True)
    temp = plugins.CONFIG.with_suffix('.' + uuid.uuid4().hex + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(plugins.CONFIG)


def linked(path):
    return path.is_symlink() or bool(getattr(path.lstat(), 'st_file_attributes', 0) & 0x400)


def import_skill(source):
    if not str(source).strip(): raise ValueError('请指定技能目录、SKILL.md 或 ZIP 的完整路径')
    source = Path(source).expanduser()
    if linked(source): raise ValueError('不导入链接或 junction')
    source = source.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix='skill-import-') as td:
        staging = Path(td)
        if source.suffix.lower() == '.zip':
            with zipfile.ZipFile(source) as archive:
                entries = archive.infolist()
                if len(entries)>2000 or sum(e.file_size for e in entries)>25_000_000:
                    raise ValueError('技能包超过 2000 个文件或展开体积超过 25 MB')
                for entry in entries:
                    relative = entry.filename.replace('\\','/')
                    target = (staging/relative).resolve()
                    target.relative_to(staging)
                    if ':' in relative or ((entry.external_attr >> 16) & 0o170000) == 0o120000:
                        raise ValueError('技能 ZIP 不允许链接或特殊路径')
                    if entry.is_dir(): target.mkdir(parents=True, exist_ok=True)
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(archive.read(entry))
        else:
            folder = source.parent if source.is_file() and source.name == 'SKILL.md' else source
            if not folder.is_dir(): raise ValueError('只接受技能目录、SKILL.md 或 ZIP')
            files = []; total = 0
            for directory, dirs, names in os.walk(folder, followlinks=False):
                dirs[:] = [d for d in dirs if d not in ('.git','__pycache__','node_modules','.agent-runtime')]
                for name in dirs + names:
                    path = Path(directory)/name
                    if linked(path): raise ValueError('技能目录包含链接或 junction')
                for name in names:
                    path = Path(directory)/name; total += path.stat().st_size; files.append(path)
                    if total>25_000_000 or len(files)>2000: raise ValueError('技能包过大')
            for path in files:
                target = staging/path.relative_to(folder); target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path,target)
        main_files = list(staging.rglob('SKILL.md'))
        if not main_files: raise ValueError('没有找到 SKILL.md')
        if len(main_files)>50: raise ValueError('单次最多导入 50 个技能')
        imported = []
        with LOCK:
            current = config()
            ROOT.mkdir(parents=True, exist_ok=True)
            for main in main_files:
                if main.stat().st_size>100000: raise ValueError('SKILL.md 超过 100 KB')
                content = main.read_text(encoding='utf-8')
                match = re.search(r'^name:\s*[\'"]?([A-Za-z0-9_-]+)', content, re.MULTILINE)
                label = match.group(1).lower() if match else (main.parent.name if main.parent != staging else source.stem)
                label = re.sub('[^a-z0-9_-]+','-',label.lower()).strip('-') or 'skill'
                name = label[:48] + '-' + uuid.uuid4().hex[:8]
                package = ROOT/name
                shutil.copytree(main.parent, package)
                manifest = package/'agent-plugin.json'
                manifest.write_text(json.dumps({'name':name,'api_version':1,'version':'1','skills':['SKILL.md'],'source':source.name+' / '+main.relative_to(staging).as_posix()}), encoding='utf-8')
                current['manifests'].append(str(manifest)); imported.append(name)
            save(current)
        return {'imported':imported, 'scripts_executed':False, 'effective':'下一模型步骤或新任务'}


def set_enabled(name, enabled):
    if not re.fullmatch('[a-z0-9_-]+', name): raise ValueError('技能名称无效')
    manifest = ROOT/name/'agent-plugin.json'
    if not manifest.is_file(): raise ValueError('技能不存在')
    with LOCK:
        current = config(); paths = current.get('manifests', [])
        paths = [p for p in paths if Path(p).resolve() != manifest.resolve()]
        if enabled: paths.append(str(manifest.resolve()))
        current['manifests'] = paths; save(current)


def list_skills():
    enabled = {str(Path(p).resolve()) for p in config().get('manifests', [])}
    from .skill_metadata import metadata
    result=[]
    for p in ROOT.glob('*/agent-plugin.json'):
        text=(p.parent/'SKILL.md').read_text(encoding='utf-8')
        manifest=json.loads(p.read_text(encoding='utf-8'))
        result.append({'name':p.parent.name,'enabled':str(p.resolve()) in enabled,'text':text,
                       'source':manifest.get('source','未记录来源'),'skill_name':metadata(text).get('name',p.parent.name)})
    return result
