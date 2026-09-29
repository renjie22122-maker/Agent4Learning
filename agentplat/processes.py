"""可等待、取消、交互的进程执行器，统一处理超时和后代回收。"""
from __future__ import annotations
import os
import queue
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import time
import uuid


class WindowsJob:
    """先挂入 Job 再恢复进程；关闭 Job 时终止所有后代。"""
    def __init__(self, restricted=False):
        import ctypes as c
        from ctypes import wintypes as w
        class Basic(c.Structure):
            _fields_ = [('process_time', c.c_int64), ('job_time', c.c_int64),
                        ('flags', w.DWORD), ('min_ws', c.c_size_t), ('max_ws', c.c_size_t),
                        ('active', w.DWORD), ('affinity', c.c_size_t),
                        ('priority', w.DWORD), ('scheduling', w.DWORD)]
        class IO(c.Structure):
            _fields_ = [(name, c.c_uint64) for name in ('read_ops', 'write_ops', 'other_ops',
                                                      'read_bytes', 'write_bytes', 'other_bytes')]
        class Extended(c.Structure):
            _fields_ = [('basic', Basic), ('io', IO), ('process_memory', c.c_size_t),
                        ('job_memory', c.c_size_t), ('peak_process', c.c_size_t),
                        ('peak_job', c.c_size_t)]
        self.c = c
        self.k = c.WinDLL('kernel32', use_last_error=True)
        self.k.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]
        self.k.CreateJobObjectW.restype = w.HANDLE
        self.k.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
        self.k.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        self.k.CloseHandle.argtypes = [w.HANDLE]
        self.handle = self.k.CreateJobObjectW(None, None)
        if not self.handle:
            raise c.WinError(c.get_last_error())
        info = Extended()
        info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if restricted:
            info.basic.flags |= 0x8 | 0x200  # active-process and job-memory limits
            info.basic.active = 32
            info.job_memory = 512 * 1024 * 1024
        if not self.k.SetInformationJobObject(self.handle, 9, c.byref(info), c.sizeof(info)):
            self.close()
            raise c.WinError(c.get_last_error())

    def attach_resume(self, proc):
        if not self.k.AssignProcessToJobObject(self.handle, int(proc._handle)):
            raise self.c.WinError(self.c.get_last_error())
        nt = self.c.WinDLL('ntdll')
        nt.NtResumeProcess.argtypes = [self.c.c_void_p]
        nt.NtResumeProcess.restype = self.c.c_long
        if nt.NtResumeProcess(int(proc._handle)) != 0:
            raise RuntimeError('无法恢复已隔离进程')

    def close(self):
        if self.handle:
            self.k.CloseHandle(self.handle)
            self.handle = None


class ProcessSupervisor:
    def __init__(self, max_active=4, max_output_bytes=8_000_000):
        self.max_active = max_active
        self.max_output_bytes = max_output_bytes
        self.tasks = {}
        self.lock = threading.RLock()

    def start(self, command, cwd, *, timeout_s=30, shell=False, env=None, cleanup_command=None, native_workspace=None, native_network='deny'):
        if os.name == 'nt' and (shell or native_workspace is not None):
            from .windows_command import prepare
            command = prepare(command)
        if not 0 < timeout_s <= 3600:
            raise ValueError('进程超时必须在 (0, 3600] 秒')
        with self.lock:
            if sum(t['status'] == 'running' for t in self.tasks.values()) >= self.max_active:
                raise RuntimeError('执行槽位已满')
            task_id = uuid.uuid4().hex
            directory = tempfile.TemporaryDirectory(prefix='agent-process-')
            path = Path(directory.name) / 'output.bin'
            stream = path.open('w+b')
            job = WindowsJob(restricted=native_workspace is not None) if os.name == 'nt' else None
            proc = None
            try:
                if native_workspace is not None:
                    from .windows_sandbox import NativeProcess
                    proc = NativeProcess(command, native_workspace, cwd, stream, native_network)
                else:
                    proc = subprocess.Popen(command, cwd=str(cwd), shell=shell,
                        stdin=subprocess.PIPE, stdout=stream, stderr=stream, env=env,
                        bufsize=0,
                        start_new_session=os.name != 'nt',
                        creationflags=0x4 if job else 0)
                if job:
                    job.attach_resume(proc)
            except BaseException:
                if proc:
                    proc.kill()
                    proc.wait(timeout=5)
                if job:
                    job.close()
                if proc and hasattr(proc, "close"):
                    proc.close()
                stream.close()
                directory.cleanup()
                raise
            task = dict(proc=proc, job=job, directory=directory, stream=stream, path=path,
                        started=time.monotonic(), timeout=timeout_s, status='running',
                        exit_code=None, reason='', done=threading.Event(), cleanup=cleanup_command,
                        input_queue=queue.Queue(maxsize=4), input_writer=False)
            self.tasks[task_id] = task
            threading.Thread(target=self._watch, args=(task_id,), daemon=True).start()
            return task_id

    def _kill(self, task):
        if task['job']:
            task['job'].close()
        elif os.name != 'nt':
            try:
                os.killpg(task['proc'].pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif task['proc'].poll() is None:
            task['proc'].kill()

    def _watch(self, task_id):
        task = self.tasks[task_id]
        try:
            while task['proc'].poll() is None:
                reason = ('timeout' if time.monotonic() - task['started'] >= task['timeout']
                          else 'output_limit' if task['path'].stat().st_size > self.max_output_bytes
                          else '')
                if reason:
                    with self.lock:
                        task['reason'] = task['reason'] or reason
                        self._kill(task)
                    break
                time.sleep(.03)
            task['proc'].wait(timeout=5)
            # 即便 shell 正常返回，也不允许后代脱离此次工具调用继续运行。
            with self.lock:
                self._kill(task)
                if task['cleanup']:
                    cleanup = subprocess.run(task['cleanup'], capture_output=True, timeout=10)
                    if cleanup.returncode and b'No such container' not in cleanup.stderr:
                        raise RuntimeError('沙箱容器清理未确认：' + cleanup.stderr.decode('utf-8', 'replace')[-300:])
                if hasattr(task['proc'], 'close'):
                    task['proc'].close()
                task['exit_code'] = task['proc'].returncode
                task['status'] = task['reason'] or 'exited'
        except Exception as exc:
            task['status'] = 'cleanup_failed'
            task['reason'] = repr(exc)
        finally:
            if task['proc'].stdin:
                task['proc'].stdin.close()
            task['done'].set()

    def poll(self, task_id, cursor=0, max_bytes=24000):
        with self.lock:
            task = self.tasks[task_id]
            with task['path'].open('rb') as f:
                f.seek(max(0, cursor))
                raw = f.read(max(1, min(max_bytes, 200000)))
                next_cursor = f.tell()
            return dict(task_id=task_id, status=task['status'], exit_code=task['exit_code'],
                        output=raw.decode('utf-8', 'replace'), cursor=next_cursor,
                        reason=task['reason'], pid=task['proc'].pid)

    def wait(self, task_id, timeout_s=1, cursor=0):
        self.tasks[task_id]['done'].wait(max(0, min(timeout_s, 60)))
        return self.poll(task_id, cursor)

    def write(self, task_id, text):
        if len(text.encode()) > 4096:
            raise ValueError('单次输入过大')
        with self.lock:
            task = self.tasks[task_id]
            if task['status'] != 'running':
                raise RuntimeError('进程已结束')
            task['input_queue'].put_nowait(text.encode('utf-8'))
            if not task['input_writer']:
                task['input_writer'] = True
                def writer():
                    while task['proc'].poll() is None:
                        try:
                            raw = task['input_queue'].get(timeout=.1)
                        except queue.Empty:
                            continue
                        try:
                            while raw:
                                n = task['proc'].stdin.write(raw)
                                if not n:
                                    return
                                raw = raw[n:]
                        except (OSError, ValueError):
                            return
                threading.Thread(target=writer, daemon=True).start()
        return {'queued': True}

    def cancel(self, task_id):
        with self.lock:
            task = self.tasks[task_id]
            if task['status'] == 'running':
                task['reason'] = 'cancelled'
                self._kill(task)
        return self.wait(task_id, 15)

    def release(self, task_id):
        with self.lock:
            task = self.tasks[task_id]
            if not task['done'].is_set():
                raise RuntimeError('运行中的进程不能释放')
            task['stream'].close()
            task['directory'].cleanup()
            del self.tasks[task_id]

    def close(self):
        for task_id in list(self.tasks):
            self.cancel(task_id)
            self.release(task_id)
