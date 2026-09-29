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


def request_execution(agent, command, reason):
    from .human_input import request_command
    from .approvals import execute
    decision = request_command(agent, command, reason)
    if decision.get('status') != 'approved':
        return {**decision, 'executed': False}
    result = execute(agent, decision['request_id'])
    return {**decision, 'executed': True, 'result': result}
