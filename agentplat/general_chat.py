"""Projectless chats own private artifacts, never the shared project workspace."""
from pathlib import Path


def storage(manager, session_id):
    return Path(manager.state_path).parent / '.chat-storage' / session_id / 'files'


def install(agent):
    from .document_assertions import install as install_assertions
    install_assertions(agent)
    # Scoped file tools still allow attachment-derived outputs. Shell and team
    # workers require a project because they can introduce broader file access.
    agent.ws.general_chat = True
    agent.ws.allow_shell = False
    agent.tool_guards.register('general-chat', lambda request, args:
        '普通对话未绑定项目；命令、宿主命令审批和项目委派需要先选择项目。'
        '文档产物请用 read_file 核对后调用 finish，由独立验收检查，不要为此申请宿主命令。'
        if request.shell or request.name in {'run_shell','spawn_agent','plan_team',
            'request_host_command','run_approved_command','create_git_worktree'} else None)


def is_general(active, summary):
    if active:
        return active.get('conversation_kind') == 'general'
    from .workspaces import DEFAULT_WORKSPACE
    return not summary.get('current_group') and Path(summary['current']) == DEFAULT_WORKSPACE
