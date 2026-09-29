"""Host-selected risk policy; model prose cannot lower review requirements."""
from dataclasses import dataclass
from pathlib import PurePath


@dataclass(frozen=True)
class ReviewDecision:
    level: str
    reason: str


def decide(profile, changed, *, unknown_changes=False, source_used=False):
    if profile not in ('strict', 'balanced'):
        raise ValueError('review_profile must be strict or balanced')
    paths = list(changed)
    if not paths and not unknown_changes:
        return ReviewDecision('sources', '只读回答：核对来源，不启动代码验收')
    if profile == 'strict':
        return ReviewDecision('independent', '宿主选择严格独立验收')
    if source_used or unknown_changes:
        return ReviewDecision('independent', '来源依赖或未识别的工作区改动')
    # Only known non-executable document formats can avoid an independent code review.
    # Unknown extensions, source code, config, and security paths remain independent.
    if all(PurePath(p).suffix.lower() in ('.md', '.txt', '.csv') for p in paths):
        return ReviewDecision('evidence', '仅数据/文档变更，保留现有证据与需求检查')
    return ReviewDecision('independent', '代码、配置或未知产物需要独立验收')
