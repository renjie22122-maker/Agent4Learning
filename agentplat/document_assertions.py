"""Executable UTF-8 document assertions, without a shell or arbitrary code."""
import json
from .runtime import Evidence, workspace_digest
from .agent_tools import AgentTool, _obj


def install(agent):
    def check_file_text(path, expected):
        agent.evidence = Evidence()
        file = agent.ws.resolve(path)
        if file.suffix.lower() not in {'.txt', '.md', '.csv', '.json'}:
            raise ValueError('文本断言仅用于 txt/md/csv/json；不证明代码运行正确')
        if file.stat().st_size > 1_000_000:
            raise ValueError('单个文本断言最多 1 MB')
        actual = file.read_bytes().decode('utf-8')
        if actual != expected:
            raise AssertionError('文件内容与预期不完全相同（含空白和换行）')
        agent.evidence = Evidence('check_file_text '+path, 0, workspace_digest(agent.ws.scope))
        agent.session.append('verification/evidence', evidence_type='document_assertion',
            command=agent.evidence.command,exit_code=0,digest=agent.evidence.digest)
        return json.dumps({'passed':True,'path':path,'characters':len(actual),'scope':'exact text only'})
    agent.tools['check_file_text'] = AgentTool('check_file_text',
        '验证文本产物与预期内容逐字一致，含空白换行；失败会拒绝。仅 txt/md/csv/json，不执行代码、不替代代码测试。',
        _obj({'path':{'type':'string'},'expected':{'type':'string'}},['path','expected']),check_file_text)
