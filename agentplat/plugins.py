"""Host-selected declarative plugins: dependencies, tool aliases and lazy skills.

Manifests cannot load arbitrary Python code or expand an agent's capabilities.
"""
import json
from pathlib import Path
import re
from .agent_tools import AgentTool, _obj

CONFIG = Path(__file__).resolve().parents[1] / '.agent-runtime' / 'plugins.json'


class PluginRegistry:
    def __init__(self, tools):
        self.tools = tools
        self.loaded = {}
        self.skills = {}

    def mount_all(self, paths):
        pending = []
        for path in paths:
            path = Path(path).resolve()
            manifest = json.loads(path.read_text(encoding='utf-8'))
            name = manifest['name']
            if not re.fullmatch(r'[a-z0-9_-]+', name): raise ValueError('插件名称无效')
            if name in self.loaded or any(m['name'] == name for p,m in pending): raise ValueError('重复插件：' + name)
            if manifest.get('api_version') != 1: raise ValueError('插件 API 版本不兼容：' + name)
            pending.append((path, manifest))
        mounted = []
        try:
            while pending:
                ready = [(p,m) for p,m in pending if all(r in self.loaded for r in m.get('requires', []))]
                if not ready: raise ValueError('插件依赖缺失或存在循环')
                for path, manifest in ready:
                    name = manifest['name']; aliases = []
                    # Validate the entire manifest before registering anything.
                    for spec in manifest.get('tools', []):
                        alias = name + '__' + spec['name']
                        if not re.fullmatch(r'[a-z0-9_]+', alias) or alias in self.tools: raise ValueError('工具别名冲突')
                        if spec['target'] not in self.tools: raise ValueError('插件不能授予不存在的工具权限')
                        aliases.append((alias, spec, self.tools[spec['target']]))
                    skills = []
                    for relative in manifest.get('skills', []):
                        source = (path.parent / relative).resolve()
                        source.relative_to(path.parent)
                        if not source.is_file() or source.stat().st_size > 100000: raise ValueError('技能文件缺失或过大')
                        skills.append((name + '/' + relative, source))
                    for alias, spec, target in aliases:
                        self.tools[alias] = AgentTool(alias, spec.get('description', target.description), target.parameters, target.fn,
                                                     target.destructive, target.terminal, target.network)
                    self.skills.update(skills)
                    self.loaded[name] = {'version':manifest.get('version','0'), 'requires':manifest.get('requires', []),
                                         'tools':[a for a,s,t in aliases], 'skills':[s for s,p in skills]}
                    mounted.append(name); pending.remove((path, manifest))
        except BaseException:
            for name in reversed(mounted): self.unmount(name)
            raise

    def unmount(self, name):
        if any(name in item['requires'] for key,item in self.loaded.items() if key != name):
            raise ValueError('还有插件依赖它，不能卸载')
        plugin = self.loaded.pop(name)
        for tool in plugin['tools']: self.tools.pop(tool, None)
        for skill in plugin['skills']: self.skills.pop(skill, None)

    def read_skill(self, name):
        source = self.skills[name]
        return json.dumps({'name':name, 'text':source.read_text(encoding='utf-8'),
                           'grants_permissions':False}, ensure_ascii=False)

    def catalog(self):
        entries = []
        for name, source in self.skills.items():
            text = source.read_text(encoding='utf-8')
            # Metadata is descriptive data, never executable configuration.
            description = re.search(r'^description:\s*(.+)$', text, re.MULTILINE)
            entries.append({'name': name, 'description': description.group(1).strip().strip('\"\'')[:1000] if description else '',
                            'grants_permissions': False})
        return entries


def install(agent):
    registry = PluginRegistry(agent.tools)
    if CONFIG.exists():
        registry.mount_all(json.loads(CONFIG.read_text(encoding='utf-8')).get('manifests', []))
    agent.plugins = registry
    agent._plugin_revision = CONFIG.stat().st_mtime_ns if CONFIG.exists() else 0
    agent.tools['list_skills'] = AgentTool('list_skills', '列出宿主安装的技能，按需读取；技能不会授予额外权限。', _obj({}, []),
        lambda: json.dumps({'skills':registry.catalog(), 'plugins':registry.loaded}, ensure_ascii=False))
    agent.tools['read_skill'] = AgentTool('read_skill', '读取一个已安装技能；不能通过参数加载任意文件。',
        _obj({'name':{'type':'string'}}, ['name']), registry.read_skill)
    def read_file(name, path):
        root = registry.skills[name].parent
        source = (root/path).resolve(); source.relative_to(root)
        if source.stat().st_size > 100000: raise ValueError('技能参考文件超过 100 KB')
        return json.dumps({'path':path, 'text':source.read_text(encoding='utf-8'), 'grants_permissions':False}, ensure_ascii=False)
    agent.tools['read_skill_file'] = AgentTool('read_skill_file', '读取已安装技能目录内的参考文本或脚本源代码；不会执行脚本。',
        _obj({'name':{'type':'string'}, 'path':{'type':'string'}}, ['name','path']), read_file)


def refresh(agent):
    revision = CONFIG.stat().st_mtime_ns if CONFIG.exists() else 0
    if revision == getattr(agent, '_plugin_revision', None): return
    from types import SimpleNamespace
    aliases = {t for p in agent.plugins.loaded.values() for t in p['tools']}
    candidate = SimpleNamespace(tools={k:v for k,v in agent.tools.items() if k not in aliases})
    install(candidate)
    if agent.capabilities.allowed_tools is not None and not hasattr(agent, 'permission_mode'):
        candidate.tools = {k:v for k,v in candidate.tools.items() if k in agent.capabilities.allowed_tools}
    agent.tools, agent.plugins, agent._plugin_revision = candidate.tools, candidate.plugins, candidate._plugin_revision
