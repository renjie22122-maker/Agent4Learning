"""Bounded recovery without replaying tools or hiding ambiguous outcomes."""
import hashlib
import json
import time


def call_with_recovery(call, *, cancel=None, on_attempt=lambda:None, on_retry=lambda *a:None, delays=(1,3)):
    for attempt in range(len(delays)+1):
        if cancel is not None and cancel.is_set():raise InterruptedError('请求已取消')
        on_attempt()
        try:return call()
        except Exception as exc:
            if (attempt >= len(delays) or not getattr(exc,'retryable',False)
                    or str(getattr(exc,'code','')) not in ('429','503')):
                raise
            delay=delays[attempt]
            on_retry(attempt+1,delay,str(getattr(exc,'code','')))
            if cancel is not None:
                if cancel.wait(delay):raise InterruptedError('重试等待已取消')
            else:time.sleep(delay)


class FailureCircuit:
    """Detect interleaved repeated failures on unchanged files, not just streaks."""
    def __init__(self, threshold=6):
        self.threshold=threshold
        self.counts={}

    def observe(self, name, args, output, ok, digest):
        identity=(name,json.dumps(args,sort_keys=True,ensure_ascii=False))
        if ok:
            self.counts={key:value for key,value in self.counts.items() if key[:2]!=identity}
            return False
        key=(*identity,digest,hashlib.sha256(output.encode('utf-8')).hexdigest())
        self.counts[key]=self.counts.get(key,0)+1
        if len(self.counts)>128:
            oldest=next(iter(self.counts));self.counts.pop(oldest)
        return self.counts.get(key,0)>=self.threshold
