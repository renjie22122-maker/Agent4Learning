"""Read-only team tree for a selected live session."""
import html,json
from . import ui


def render(demo,qs):
    sid=qs.get('session') or demo.agent_state.get('session_id','')
    entry=demo.live_sessions.get(sid)
    manager=entry[1].children if entry and entry[1] else None
    rows=[]
    coordination=''
    if manager:
        with manager.lock:tasks=[manager.get(k) for k in manager.tasks]
        for item in tasks:
            rows.append('<tr>'+''.join('<td>'+html.escape(str(value))+'</td>' for value in
                (item['agent_id'],item.get('parent_id') or '主 Agent',item.get('depth',1),item['status'],item['mode'],
                 item.get('used_tokens',0),item['task'][:180],
                 ('模型请求中 '+str(item['request_elapsed_seconds'])+' 秒') if item.get('request_started_at') and item['status'] not in ('completed','cancelled','failed','interrupted','budget_exceeded','blocked') else '当前无模型请求',
                 json.dumps(item.get('verification_progress',{}),ensure_ascii=False)))+'</tr>')
        state=json.dumps(manager.team_state(),ensure_ascii=False,indent=2)
        jobs=manager.coordination.jobs()
        job_rows=''.join('<tr>'+''.join('<td>'+html.escape(str(value))+'</td>' for value in
            (job['title'],job['owner'] or '未认领',job['status'],job['revision'],job.get('note','')))+'</tr>' for job in jobs)
        mail=manager.coordination.audit()
        mail_rows=''.join('<tr>'+''.join('<td>'+html.escape(str(value))+'</td>' for value in
            (m['id'][:12],m['sender'],m['recipient'],m['kind'],
             '已确认' if m['acknowledged'] else '已交给上下文' if m['delivered'] else '待送达',m['body'][:1500]))+'</tr>' for m in mail)
        coordination=f'<h2>团队任务板</h2><p>done 表示成员报告完成，仍需验收。成员结束但未交接的工作会标为 blocked。</p><table><tr><th>任务</th><th>负责人</th><th>状态</th><th>版本</th><th>说明</th></tr>{job_rows}</table><h2>最近通信</h2><p>只显示最近 100 条；已交给上下文不等于已处理。消息保存在本机。</p><table><tr><th>消息 ID</th><th>发送者</th><th>接收者</th><th>类型</th><th>送达状态</th><th>内容</th></tr>{mail_rows}</table>'

        if hasattr(manager,'planner'):
            coordination+='<h2>自动团队计划</h2><pre>'+html.escape(json.dumps([manager.planner.get(k) for k in manager.planner.plans],ensure_ascii=False,indent=2))+'</pre>'
        budget=f'团队累计计入预算 {manager.budget.spent:,} tokens；总上限 {manager.budget.total or "不限"}。中断且缺少 usage 的请求按预留估计计入。'
    else:state='{}';budget='此会话没有当前运行进程中的团队记录。历史子任务 JSON 和会话日志仍保留。'
    body=f'<h1>多 Agent 团队</h1><p>{html.escape(sid)}</p><p>{html.escape(budget)}</p><p>刷新查看状态；共享状态是参考资料，不替代独立验证。</p><table><tr><th>ID</th><th>父任务</th><th>深度</th><th>状态</th><th>权限</th><th>API tokens</th><th>任务</th><th>模型请求状态</th><th>验收计划 / 进度</th></tr>{"".join(rows)}</table>{coordination}<h2>共享状态</h2><pre>{html.escape(state)}</pre><a href="/agent?session={html.escape(sid,quote=True)}">返回会话</a>'
    return ui.page('团队状态','agent',body)
