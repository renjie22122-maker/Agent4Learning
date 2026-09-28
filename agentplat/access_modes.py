"""Host-selected session permissions; model arguments cannot change the mode."""
from .runtime import CapabilityPolicy

MODES = {'readonly': '只读', 'auto': '自动（沙箱与逐次审批）', 'full': '完全访问（宿主命令）'}
READ_TOOLS = frozenset({'list_dir','read_file','read_chunk','grep','finish','list_skills','read_skill','read_skill_file','search_knowledge','read_knowledge_chunk','list_knowledge','git_review','search_memory','list_attachments','read_attachment','search_attachment'})


def apply(agent, mode):
    if mode not in MODES: raise ValueError('未知权限模式')
    if not hasattr(agent, '_sandbox_mode'): agent._sandbox_mode = agent.ws.execution_mode
    agent.permission_mode = mode
    agent.ws.trusted_host_commands = mode == 'full'
    agent.ws.allow_shell = mode != 'readonly'
    agent.ws.execution_mode = 'local' if mode == 'full' else agent._sandbox_mode
    agent.capabilities = CapabilityPolicy(READ_TOOLS | {'request_user_input'}, False, False, False) if mode == 'readonly' else CapabilityPolicy()
    agent.session.append('permission/applied', mode=mode)


def apply_pending(agent):
    mode = getattr(agent, 'pending_permission_mode', None)
    if mode is not None:
        agent.pending_permission_mode = None
        apply(agent, mode)
