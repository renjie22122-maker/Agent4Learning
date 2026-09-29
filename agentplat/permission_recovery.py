"""Actionable failure guidance; diagnostics never grant or replay authority."""


def guidance(agent, name, output):
    text = str(output)
    if name in {'request_execution', 'request_host_command', 'run_approved_command'}:
        return ''
    if getattr(agent.ws, 'general_chat', False):
        return '\n[宿主恢复指引] 普通对话没有项目命令权限。需要命令时请用户在界面选择项目；文字回答不改变权限。'
    if getattr(agent, 'permission_mode', '') == 'readonly':
        return '\n[宿主恢复指引] 当前为只读模式；需要用户在执行权限界面切换，禁止通过宿主命令绕过。'
    if any(word in text.lower() for word in ('timeout', '超时', '结果未知', '中止')):
        return '\n[宿主恢复指引] 先检查执行状态和实际产物；结果未知时不得自动重放或转到宿主执行。'
    if name in {'run_shell', 'start_process'}:
        markers = ('permission', 'access is denied', 'modulenotfounderror', 'no module named',
                   'filenotfounderror', 'not recognized', 'command not found', '拒绝', '禁止',
                   '权限', '不可用', '找不到', '不是内部或外部命令')
        if not any(word in text.lower() for word in markers):
            return ''
        return ('\n[宿主恢复指引] 先区分代码错误、缺依赖与权限限制，并尝试现有权限内的方案。'
                '若确需宿主环境，调用 request_execution，提交必要的精确命令及原因；'
                '用户允许一次后宿主执行并返回结果。可能已发生的副作用须先核对，禁止盲目重跑。'
                '缺包本身不是授权，用户拒绝后不得换写法重复申请。')
    return ('\n[宿主恢复指引] 检查实际授权范围；文件范围通过项目设置调整，网页访问通过网络权限设置调整。'
            '需要用户选择时用 request_user_input 等待回答；回答本身不授予文件或网络权限。'
            '不得把受限文件或网页操作改写成宿主命令绕过授权。')


def request_execution(agent, command, reason, timeout_s=60):
    from .human_input import request_command
    from .approvals import execute
    decision = request_command(agent, command, reason, timeout_s=timeout_s)
    if decision.get('status') != 'approved':
        return {**decision, 'executed': False}
    result = execute(agent, decision['request_id'])
    return {**decision, 'executed': True, 'result': result}


def host_failure_guidance(result):
    if result.get('success'): return ''
    text=result.get('output','').lower()
    if 'could not determine home directory' in text or 'expanduser' in text:
        return '宿主进程无法定位用户目录：检查宿主环境变量与配置路径，不能据此认定软件安装损坏。'
    if result.get('status') in ('timeout','cancelled'):
        return '宿主命令未正常完成；先核对现有产物和进程结果。超时不等于安装损坏，不应直接重复安装或创建环境。'
    if result.get('cleanup_error'):
        return '命令结果已保留，但宿主临时资源清理失败；这是执行器问题，不是被调用软件损坏的证据。'
    return '这是宿主执行结果。根据具体错误区分缺依赖、配置缺失、权限和代码错误；不能从沙箱结果推断宿主环境。'


def install_execution_tools(agent):
    """Shared approval surface; registration never grants execution authority."""
    import json
    from .agent_tools import AgentTool, _obj
    string = {'type': 'string'}
    def add(name, description, properties, required, fn, writes=False):
        def invoke(**args):
            return json.dumps(fn(**args), ensure_ascii=False)
        agent.tools[name] = AgentTool(name, description, _obj(properties, required), invoke, writes)
    from . import approvals
    from .human_input import request_command
    from .permission_recovery import request_execution
    add('request_execution', '沙箱或环境确实无法完成必要操作时，请求精确宿主命令的单次授权；聊天中等待，获批后直接执行并返回结果。不会永久扩权。拒绝或取消后不执行；不得重放副作用未知的命令或绕过只读/项目范围限制。',
        {'command':string, 'reason':string, 'timeout_s':{'type':'number','minimum':1,'maximum':3600,'description':'执行时限，默认60秒；长任务应显式设置，将随命令提交用户确认。'}}, ['command','reason'],
        lambda command, reason, timeout_s=60: request_execution(agent, command, reason, timeout_s), True)
    add('request_host_command', '当沙箱确实无法完成必要命令时，在聊天中请求批准精确宿主命令并等待答复。仅明确获批后可调用 run_approved_command，不轮询、不绕过禁止。',
        {'command':string, 'reason':string, 'timeout_s':{'type':'number','minimum':1,'maximum':3600}}, ['command','reason'],
        lambda command, reason, timeout_s=60: request_command(agent, command, reason, timeout_s))
    add('run_approved_command', '执行人类已批准的精确宿主命令；授权绑定当前会话与工作区，只能使用一次。',
        {'request_id':string}, ['request_id'], lambda request_id: approvals.execute(agent, request_id), True)
