"""Demonstrate why a skill catalog must refresh at a model step boundary."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
import json
from agentlab.util import lab
from agentplat import plugins, skill_import


def main():
    with lab('lab-37-skill-revocation', '技能导入与即时停用', '停用后缓存技能是否还能被读取'):
        with TemporaryDirectory() as td:
            root = Path(td); source = root/'source'; source.mkdir()
            (source/'SKILL.md').write_text('---\nname: evidence\ndescription: Collect evidence\n---\nRead first.', encoding='utf-8')
            with patch.object(plugins, 'CONFIG', root/'plugins.json'), patch.object(skill_import, 'ROOT', root/'installed'):
                name = skill_import.import_skill(source)['imported'][0]
                agent = SimpleNamespace(tools={}, capabilities=SimpleNamespace(allowed_tools=None))
                plugins.install(agent)
                skill_import.set_enabled(name, False)
                before = len(json.loads(agent.tools['list_skills'].fn())['skills'])
                assert before == 1
                print('[BROKEN-REPRODUCED] 宿主停用后，旧内存目录仍保留技能')
                plugins.refresh(agent)
                after = len(json.loads(agent.tools['list_skills'].fn())['skills'])
                assert after == 0
                print('[FIX-APPLIED] 下一模型步骤原子替换技能目录及读取工具')
                print(f'[VERIFY] stale_skills: {before} -> {after}')
                print('[TAKEAWAY] 技能是工作方法；停用在步骤边界生效，已经进入上下文的文字不会被撤回。')
    return 0


if __name__ == '__main__': raise SystemExit(main())
