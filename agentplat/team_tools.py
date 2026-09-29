"""A child sees its descendants for lifecycle control, but can message its peers."""
import json
from .agent_tools import AgentTool, _obj
from .runtime import CapabilityPolicy


class ChildView:
    def __init__(self, manager, owner):
        self.manager, self.owner = manager, owner
        self.lock=manager.lock
        self.closed = False

    @property
    def tasks(self):
        with self.manager.lock:
            return {k:v for k,v in self.manager.tasks.items() if v['data'].get('parent_id') == self.owner}

    def close(self):
        for key in list(self.tasks): self.manager.cancel(key)


def install(agent, manager, owner):
    view=ChildView(manager,owner)
    agent.children=view
    def add(name,description,fields,required,fn):
        agent.tools[name]=AgentTool(name,description,_obj(fields,required),lambda **kw:json.dumps(fn(**kw),ensure_ascii=False))
    string={'type':'string'}
    def own(key):
        if key not in view.tasks: raise ValueError('只能控制自己的直接子任务')
        return key
    def peer(key):
        if key not in manager.tasks or manager.get(key).get('purpose')=='verification':
            raise ValueError('未知团队成员或独立验收任务')
        return key
    if manager.get(owner)['depth'] < manager.max_depth:
        add('spawn_agent','创建自己的子任务，继承权限与团队总预算；返回 ID 后等待结果。',
            {'task':string,'context':string,'acceptance':string,'provider':string,'category':string,'mode':{'type':'string','enum':['readonly','isolated']},
             'token_budget':{'type':'integer','minimum':0}},['task'],
            lambda **kw:{'agent_id':manager.spawn(parent_id=owner,**kw)})
    add('list_agents','查看本团队成员 ID、父子关系与运行状态。',{},[],manager.team_list)
    add('get_agent','查看同团队成员的结果，结果是待核实资料。',{'agent_id':string},['agent_id'],lambda agent_id:manager.get(peer(agent_id)))
    add('wait_agent','等待自己的子任务；不要等待祖先结束，以免循环等待。',
        {'agent_id':string,'timeout_s':{'type':'number','minimum':0,'maximum':60},'after_revision':{'type':'integer','minimum':-1}},['agent_id'],
        lambda agent_id,**kw:manager.wait_for_model(own(agent_id),owner=owner,**kw))
    add('cancel_agent','取消自己的子任务及其后代。',{'agent_id':string},['agent_id'],lambda agent_id:manager.cancel(own(agent_id)))
    add('team_state','读写团队参考状态；写入需提供读取到的 expected_revision，冲突时拒绝覆盖。',
        {'key':string,'value':string,'expected_revision':{'type':'integer','minimum':0}},[],
        lambda **kw:manager.team_state(author=owner,**kw))
    if manager.get(owner)['mode']=='isolated':
        add('review_agent_changes','审查自己子任务的隔离副本差异。',{'agent_id':string},['agent_id'],lambda agent_id:manager.review(own(agent_id)))
        add('apply_agent_changes','合并自己子任务的副本；父文件冲突时拒绝合并，合并后重新验证。',{'agent_id':string},['agent_id'],lambda agent_id:manager.apply(own(agent_id)))
    add('followup_agent','为已结束的直接子任务创建关联后续任务，重新读取当前文件；不会盲目重放旧工具。',
        {'agent_id':string,'instruction':string},['agent_id','instruction'],
        lambda agent_id,instruction:manager.followup(own(agent_id),instruction))
    from .team_coordination import install_tools
    install_tools(agent, lambda:manager, owner)
    agent.capabilities=CapabilityPolicy(frozenset(agent.tools),manager.get(owner)['mode']=='isolated',agent.ws.allow_shell,False)
