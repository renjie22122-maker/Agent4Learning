"""Reviewer capability inventory and incremental plan reporting."""
import json
from pathlib import Path
import shutil
from .agent_tools import AgentTool, _obj


def install(agent, manager, owner):
    if agent.ws.allow_shell:
        from .permission_recovery import install_execution_tools
        install_execution_tools(agent)
    def scratch():
        import tempfile
        path=Path(tempfile.mkdtemp(prefix='verification-',dir=agent.ws.root)).resolve()
        path.relative_to(agent.ws.root.resolve())
        return json.dumps({'path':agent.ws.rel(path),'purpose':'在新目录内创建测试输入与输出；不要覆盖已有交付文件。'},ensure_ascii=False)
    def environment():
        return json.dumps({'shell_allowed':agent.ws.allow_shell,'execution_mode':agent.ws.execution_mode,
            'host_execution':{'available':'request_execution' in agent.tools,
                'workspace':str(agent.ws.root),
                'policy':'必要检查缺少环境时，可申请精确宿主命令的新单次授权；作者的批准不能复用。检查本验收副本，不修改交付文件；拒绝后报告 blocked。'},
            'knowledge_tools':[k for k in agent.tools if 'knowledge' in k],
            'test_output_policy':'create_verification_scratch 创建独立测试目录。已有测试若会重写固定样例，应报告路径并要求改用临时目录；不能忽略文件修改来放行。',
            'knowledge_source':'宿主提供的任务知识库快照；用 read_knowledge_chunk 核实原始分块，作者转录文件不是独立来源。',
            'source_snapshot':manager.get(owner).get('source_snapshot', []),
            'source_note':'本次交付引用的网页落盘证据已按 SHA256 复制；仅供读取，修改证据会使验收无效。missing 表示平台未能提供证据，应报告 blocked，不要推断作者没有原文。',
            'browser_tools':[k for k in agent.tools if k.startswith('browser_')],
            'configured_browser_backend':(Path(__file__).resolve().parents[1]/'.agent-runtime/browser-policy.json').exists(),
            'host_path_probes':{k:bool(shutil.which(k)) for k in ('node','python','deno','bun')},
            'note':'宿主 PATH 探测不保证沙箱命令可用；网页请优先使用浏览器工具。环境不足先区分沙箱限制与宿主状态；有授权工具时可申请必要检查，未获批或不可用则报告 blocked，不要自制解释器。'},ensure_ascii=False)
    def progress(stage,checks,blockers=''):
        with manager.lock:
            manager.tasks[owner]['data']['verification_progress']={'stage':stage,'checks':checks,'blockers':blockers}
            manager._save(manager.tasks[owner])
        agent.session.append('verification/progress',stage=stage,checks=checks,blockers=blockers)
        return json.dumps({'recorded':True,'next':'按计划完成检查后提交结论；受阻时用 blocked 结束，不扩大任务范围。'},ensure_ascii=False)
    agent.tools['create_verification_scratch']=AgentTool('create_verification_scratch','在验收工作区新建唯一测试目录；只用于新测试输入和输出，不改变交付代码保护规则。',_obj({},[]),scratch,True)
    agent.tools['verification_environment']=AgentTool('verification_environment','查看验收可用工具与环境线索；不把宿主探测当作沙箱执行成功。',_obj({},[]),environment)
    agent.tools['report_verification_progress']=AgentTool('report_verification_progress','记录有限验收计划与进度供主机查看；不要将新开发测试基础设施当成交付验收。',
        _obj({'stage':{'type':'string','enum':['plan','checking','blocked','reporting']},
              'checks':{'type':'array','items':{'type':'string'},'maxItems':20},
              'blockers':{'type':'string','maxLength':2000}},['stage','checks']),progress)
