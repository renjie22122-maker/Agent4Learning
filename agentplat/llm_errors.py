"""Provider-independent error classification."""
from agentlab.providers import LLMError

class LLMCallError(LLMError):
    """真实调用失败。继承 LLMError，所以重试/熔断策略会自动生效。"""

    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(code, message, 0.0, retryable)


def _http_status_to_llm_error(status: int, body: str) -> LLMCallError:
    """把 HTTP 状态映射成平台认识的错误码。

    关键点：**retryable 的判定决定上层要不要重试**，判错会放大故障。
    429/5xx/超时是可重试的（上游暂时不行）；401/400/404 不可重试
    （key 错了、模型名拼错了 —— 重试一万次也没用，只会浪费时间）。
    """
    detail = body[:400].replace("\n", " ")
    if status == 429:
        return LLMCallError("429", f"上游限流：{detail}", retryable=True)
    if status in (500, 502, 503, 504):
        return LLMCallError("503", f"上游 {status}：{detail}", retryable=True)
    if status == 401:
        return LLMCallError("401", f"鉴权失败（API Key 不对或没权限）：{detail}", retryable=False)
    if status == 403:
        return LLMCallError("403", f"无权限（可能是模型未开通/余额不足）：{detail}", retryable=False)
    if status == 404:
        return LLMCallError("404", f"端点或模型不存在（检查 base_url 与模型名）：{detail}", retryable=False)
    if status == 400:
        return LLMCallError("400", f"请求被拒（参数/上下文超限）：{detail}", retryable=False)
    if status >= 500:
        return LLMCallError("503", f"上游 {status}：{detail}", retryable=True)
    return LLMCallError(str(status), f"上游返回 {status}：{detail}", retryable=False)


