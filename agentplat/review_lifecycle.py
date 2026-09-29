"""Completion policy and review checks, separate from model/tool execution."""
import json


class ReviewLifecycle:
    def _review_status_text(self):
        from .independent_review import status
        info = status(self)
        progress = info.get('verification_progress') or {}
        return ('验收任务：' + str(info.get('agent_id', '尚未启动')) +
                '\n状态：' + str(info['status']) + '；已用 ' + str(info.get('elapsed_seconds',0)) + ' 秒' +
                '\n阶段：' + str(progress.get('stage','准备验收')) +
                '\n检查计划：' + '；'.join(progress.get('checks',[])) +
                ('\n受阻原因：' + progress['blockers'] if progress.get('blockers') else '') +
                '\n' + info['note'])

    def _review_finish(self, args: dict) -> "ReflectionVerdict":
        """在**接受**完成声明之前做一次核对。见 `reflection.py`。"""
        from .reflection import ReflectionRequest, ReflectionVerdict
        if self.children:
            planner=getattr(self.children,'planner',None)
            if planner and any(p['status'] not in ('ready_for_final_review','cancelled') for p in planner.plans.values()):
                return ReflectionVerdict(False,'团队计划尚未完成：运行中用 wait_team_plan；受阻或中断先修订，或明确 cancel_team_plan 并如实说明未完成项。','团队计划待完成',False)
            if hasattr(self.children, 'coordination') and self.children.coordination.pending('root'):
                return ReflectionVerdict(False, '有尚未处理的团队消息，请在下一步骤读取后再结束。', '团队消息待处理', False)
            from .subagents import TERMINAL
            if any(t['data']['status'] not in TERMINAL and t['data'].get('purpose') != 'verification'
                   for t in self.children.tasks.values()):
                return ReflectionVerdict(False, '仍有子任务未结束，请等待或取消后核对结果。', '子任务未收尾',
                                         self._finish_rejects >= 2)
        if any(t['status'] == 'running' for t in self.ws.processes.tasks.values()):
            return ReflectionVerdict(False, '仍有命令在运行，请等待或取消并检查退出状态。', '命令未收尾',
                                     self._finish_rejects >= 2)

        if getattr(self, 'verification_task', False):
            from .independent_review import validate_verdict
            return validate_verdict(self, args)
        from .runtime import workspace_digest
        self._verified = self.evidence.valid(self.ws.scope)
        if workspace_digest(self.ws.scope) != self._initial_digest and not self._files_touched:
            self._files_touched.append("[工作区内容发生变化，含 shell 改动]")
        if self.reflector is None:
            return ReflectionVerdict.ok()
        review_summary = str(args.get('summary', '') or '')
        human_answered = False
        question_ids = set()
        for event in self.session.events:
            if event.kind == 'followup/user':
                human_answered = False
                question_ids.clear()
            if event.kind == 'human/requested' and event.data.get('request_type') == 'question':
                question_ids.add(event.data.get('question_id'))
            if event.kind == 'human/answered' and event.data.get('status') == 'answered' and event.data.get('question_id') in question_ids:
                human_answered = True
        if human_answered:
            review_summary += '\n宿主已记录：request_user_input 收到真实用户答复；当前调用 finish。'
        req = ReflectionRequest(
            task=getattr(self, '_acceptance_task', self._task_text),
            summary=review_summary,
            files_changed=str(args.get("files_changed", "") or ""),
            verified=self._verified,
            files_touched=list(self._files_touched),
            failed_verifies=self._failed_verifies,
            rejects=self._finish_rejects,
        )
        verdict = self.reflector.review(req)
        if verdict.allow and self._files_touched and getattr(self, 'independent_review_required', False):
            from .independent_review import check
            return check(self)
        return verdict
