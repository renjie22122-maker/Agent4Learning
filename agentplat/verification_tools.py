"""Reviewer capability inventory and incremental plan reporting."""
import json
from pathlib import Path
import shutil
from .agent_tools import AgentTool, _obj


def install(agent, manager, owner):
    def environment():
        return json.dumps({'shell_allowed':agent.ws.allow_shell,'execution_mode':agent.ws.execution_mode,
            'knowledge_tools':[k for k in agent.tools if 'knowledge' in k],
            'knowledge_source':'宿主提供的任务知识库快照；用 read_knowledge_chunk 核实原始分块，作者转录文件不是独立来源。',
            'browser_tools':[k for k in agent.tools if k.startswith('browser_')],
            'configured_browser_backend':(Path(__file__).resolve().parents[1]/'.agent-runtime/browser-policy.json').exists(),
            'host_path_probes':{k:bool(shutil.which(k)) for k in ('node','python','deno','bun')},
            'note':'宿主 PATH 探测不保证沙箱命令可用；网页请优先使用浏览器工具。环境不足报告 blocked，不要自制解释器。'},ensure_ascii=False)
    def progress(stage,checks,blockers=''):
        with manager.lock:
            manager.tasks[owner]['data']['verification_progress']={'stage':stage,'checks':checks,'blockers':blockers}
            manager._save(manager.tasks[owner])
        agent.session.append('verification/progress',stage=stage,checks=checks,blockers=blockers)
        return json.dumps({'recorded':True,'next':'按计划完成检查后提交结论；受阻时用 blocked 结束，不扩大任务范围。'},ensure_ascii=False)
    agent.tools['verification_environment']=AgentTool('verification_environment','查看验收可用工具与环境线索；不把宿主探测当作沙箱执行成功。',_obj({},[]),environment)
    agent.tools['report_verification_progress']=AgentTool('report_verification_progress','记录有限验收计划与进度供主机查看；不要将新开发测试基础设施当成交付验收。',
        _obj({'stage':{'type':'string','enum':['plan','checking','blocked','reporting']},
              'checks':{'type':'array','items':{'type':'string'},'maxItems':20},
              'blockers':{'type':'string','maxLength':2000}},['stage','checks']),progress)
