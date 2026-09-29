"""Host-owned DAG scheduling: independent review, conflict-aware merge, no replay."""
import json
import os
from pathlib import Path
import threading
import time
import uuid

FINAL={'ready_for_final_review','blocked','cancelled','interrupted'}


class TeamPlanner:
    def __init__(self,manager):
        self.manager=manager;self.root=manager.directory/'plans';self.root.mkdir(exist_ok=True)
        self.lock=threading.RLock();self.plans={};self.closed=False
        for path in self.root.glob('*.json'):
            value=json.loads(path.read_text(encoding='utf-8'))
            if value['status'] not in FINAL:
                value['status']='interrupted';self.save(value)
            self.plans[value['id']]=value
        self.thread=threading.Thread(target=self._work,daemon=True,name='team-plan-scheduler');self.thread.start()

    def save(self,plan):
        plan['revision']=plan.get('revision',0)+1
        path=self.root/(plan['id']+'.json');temp=path.with_suffix('.tmp')
        temp.write_text(json.dumps(plan,ensure_ascii=False,indent=2),encoding='utf-8');os.replace(temp,path)

    def create(self,goal,tasks):
        if not goal.strip() or not 1<=len(tasks)<=min(24,self.manager.max_queue):raise ValueError('计划需要目标及 1–队列容量 个任务')
        names=[t['id'] for t in tasks]
        if len(set(names))!=len(names) or any(not n or len(n)>80 for n in names):raise ValueError('节点 ID 必须唯一')
        nodes={}
        for task in tasks:
            if not task.get('task','').strip() or not task.get('acceptance','').strip():raise ValueError('每个节点必须有任务及验收条件')
            deps=task.get('depends_on',[])
            if any(d not in names or d==task['id'] for d in deps):raise ValueError('未知或自循环依赖')
            if task.get('mode','isolated') not in ('isolated','readonly'):raise ValueError('未知执行模式')
            nodes[task['id']]={**task,'mode':task.get('mode','isolated'),'depends_on':deps,'state':'pending','attempts':0}
        seen=set()
        while len(seen)<len(nodes):
            ready={k for k,v in nodes.items() if k not in seen and set(v['depends_on'])<=seen}
            if not ready:raise ValueError('任务依赖存在环')
            seen.update(ready)
        plan={'id':uuid.uuid4().hex,'goal':goal,'nodes':nodes,'status':'running','revision':0,'history':[]}
        with self.lock:self.plans[plan['id']]=plan;self.save(plan)
        return self.get(plan['id'])

    def get(self,key):
        with self.lock:return json.loads(json.dumps(self.plans[key]))

    def _work(self):
        while not self.closed and not self.manager.closed:
            with self.lock:
                for plan in self.plans.values():
                    if plan['status']!='running':continue
                    try:self.tick(plan)
                    except Exception as exc:
                        plan.update(status='interrupted',error=type(exc).__name__+': '+str(exc));self.save(plan)
            time.sleep(.2)

    def tick(self,plan):
        from .subagents import TERMINAL
        from .runtime import workspace_digest
        m=self.manager;nodes=plan['nodes'];changed=False
        if m.parent_cancel is not None and m.parent_cancel.is_set():self.cancel(plan['id']);return
        for key,node in nodes.items():
            state=node['state']
            if state=='pending' and all(nodes[d]['state'] in ('merged','reference_ready') for d in node['depends_on']):
                if sum(v['data']['status'] not in TERMINAL for v in m.tasks.values())>=m.max_queue:continue
                node['state']='dispatching';self.save(plan)
                context='团队目标：'+plan['goal']+'\n依赖结果（待核实）：'+json.dumps({d:nodes[d].get('summary','') for d in node['depends_on']},ensure_ascii=False)
                node['agent_id']=m.spawn(node['task'],acceptance=node['acceptance'],mode=node['mode'],context=context)
                node.update(state='working',attempts=node['attempts']+1);changed=True
            elif state=='working':
                result=m.get(node['agent_id'])
                if result['status'] not in TERMINAL:continue
                node['summary']=result.get('summary','')
                if result['status']!='completed':node.update(state='blocked',error=result.get('error') or result['status'])
                elif node['mode']=='readonly':node['state']='reference_ready'
                else:
                    branch=m.tasks[node['agent_id']]['branch']
                    node['digest']=workspace_digest(branch.root)
                    node['state']='review_dispatching';self.save(plan)
                    task='独立验收以下任务与产物，不采信作者自评。不要扩展需求。执行有限检查，完成后 finish.summary 必须是 JSON：verdict=pass/fail/blocked/inconclusive，findings 数组，tests 数组，reason 字符串。\n任务：'+node['task']+'\n验收：'+node['acceptance']
                    node['review_id']=m.spawn(task,mode='isolated',purpose='verification',source_agent=node['agent_id'])
                    node['state']='reviewing'
                changed=True
            elif state=='reviewing':
                result=m.get(node['review_id'])
                if result['status'] not in TERMINAL:continue
                try:verdict=json.loads(result.get('summary',''))
                except ValueError:verdict={}
                node['review']=verdict
                branch=m.tasks[node['agent_id']]['branch']
                if result['status']!='completed' or verdict.get('verdict')!='pass' or verdict.get('findings') or not verdict.get('tests'):
                    node.update(state='blocked',error='独立验收未通过：'+str(verdict or result.get('error','')))
                elif workspace_digest(branch.root)!=node['digest']:node.update(state='blocked',error='验收期间作者副本已变化')
                else:
                    from .runtime import effective_policy
                    provider=getattr(m,'authority_provider',None)
                    if provider:provider().check('apply_agent_changes',writes=True)
                    node['state']='merging';self.save(plan)
                    # Check conflicts before entering the side-effecting merge.
                    from .isolation import inventory
                    current=inventory(branch.parent)
                    conflicts=[c['path'] for c in branch.manifest() if current.get(c['path'])!=branch.base.get(c['path'])]
                    if conflicts:node.update(state='blocked',error='父文件冲突：'+', '.join(conflicts))
                    else:node['merge']=m.apply(node['agent_id']);node['state']='merged'
                changed=True
        if any(n['state']=='blocked' for n in nodes.values()):plan['status']='blocked';changed=True
        elif all(n['state'] in ('merged','reference_ready') for n in nodes.values()):plan['status']='ready_for_final_review';changed=True
        if changed:self.save(plan)

    def revise(self,key,node_id,task,acceptance,reason,expected_revision):
        with self.lock:
            plan=self.plans[key];node=plan['nodes'][node_id]
            if plan['revision']!=expected_revision:raise ValueError('计划版本冲突')
            if node['state']!='blocked' or node['attempts']>=3:raise ValueError('仅能调整受阻节点，每节点最多三次尝试')
            if not reason.strip() or not task.strip() or not acceptance.strip():raise ValueError('必须提供修订理由、任务和验收条件')
            plan['history'].append({'node':node_id,'previous':dict(node),'reason':reason})
            node.update(task=task,acceptance=acceptance,state='pending',error='')
            plan.update(status='running',error='');self.save(plan)
            return self.get(key)

    def cancel(self,key):
        with self.lock:
            plan=self.plans[key]
            for n in plan['nodes'].values():
                for field in ('agent_id','review_id'):
                    if n.get(field):self.manager.cancel(n[field])
            plan['status']='cancelled';self.save(plan)

    def wait(self,key,timeout_s=30):
        deadline=time.monotonic()+min(60,max(0,timeout_s))
        while time.monotonic()<deadline:
            result=self.get(key)
            if self.manager.parent_cancel is not None and self.manager.parent_cancel.is_set():return result
            if result['status'] in FINAL:return result
            time.sleep(.2)
        return self.get(key)


def install(agent,manager):
    from .agent_tools import AgentTool,_obj
    def planner():
        m=manager()
        if not hasattr(m,'planner'):m.planner=TeamPlanner(m)
        return m.planner
    string={'type':'string','maxLength':8000}
    task=_obj({'id':{'type':'string','maxLength':80},'task':string,'acceptance':string,'mode':{'type':'string','enum':['isolated','readonly']},'depends_on':{'type':'array','items':{'type':'string'},'maxItems':24}},['id','task','acceptance'])
    definitions=[('plan_team','需要并行或有依赖的复杂任务时，先自行拆出任务图并提交。宿主自动调度、独立验收写入副本并冲突检查后合并；readonly 仅为待核实参考。最终仍由主 Agent 验收。',{'goal':string,'tasks':{'type':'array','items':task,'minItems':1,'maxItems':24}},['goal','tasks'],lambda **kw:planner().create(**kw)),
      ('get_team_plan','读取持久化计划与验收状态。',{'plan_id':string},['plan_id'],lambda plan_id:planner().get(plan_id)),
      ('wait_team_plan','宿主等待计划，不消耗模型轮询；可中止。',{'plan_id':string,'timeout_s':{'type':'number','minimum':0,'maximum':60}},['plan_id'],lambda plan_id,timeout_s=30:planner().wait(plan_id,timeout_s)),
      ('cancel_team_plan','取消计划及其运行中的任务，保留记录。',{'plan_id':string},['plan_id'],lambda plan_id:planner().cancel(plan_id)),
      ('revise_team_plan','根据具体失败修订受阻节点；禁止原样盲目重试。节点最多三次尝试，旧副作用不会重放。',{'plan_id':string,'node_id':string,'task':string,'acceptance':string,'reason':string,'expected_revision':{'type':'integer'}},['plan_id','node_id','task','acceptance','reason','expected_revision'],lambda plan_id,**kw:planner().revise(plan_id,**kw))]
    for name,description,fields,required,fn in definitions:
        agent.tools[name]=AgentTool(name,description,_obj(fields,required),lambda _fn=fn,**kw:json.dumps(_fn(**kw),ensure_ascii=False),destructive=name in ('plan_team','revise_team_plan'))
