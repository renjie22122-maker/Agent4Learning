"""Desktop service supervisor. Restart exited processes, never kill a slow task."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / '.agent-runtime'


def restart_delay(exits, now):
    recent = [t for t in exits if now - t < 600]
    if len(recent) >= 5:
        return None
    return (2, 5, 15, 30, 60)[len(recent)]


def supervise(command=None, runtime=RUNTIME, port=8800):
    runtime.mkdir(parents=True, exist_ok=True)
    # Lifetime OS lock is released on crash; a stale PID file is never trusted.
    lock = open(runtime / 'supervisor.lock', 'a+b')
    lock.seek(0); lock.write(b'0'); lock.flush(); lock.seek(0)
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        return 0
    def status(**data):
        record = dict(supervisor_pid=os.getpid(), updated_at=time.time(), **data)
        temp = runtime / 'supervisor-status.tmp'
        temp.write_text(json.dumps(record), encoding='utf-8')
        temp.replace(runtime / 'supervisor-status.json')
    exits = []
    child = None
    try:
        while True:
            with socket.socket() as probe:
                if probe.connect_ex(('127.0.0.1', port)) == 0:
                    status(state='blocked', reason='port_in_use')
                    return 1
            log_path = runtime / ('service-' + time.strftime('%Y%m%d-%H%M%S') + '-' + str(time.time_ns()) + '.log')
            with log_path.open('ab') as log:
                child = subprocess.Popen(command or [sys.executable, '-u', '-X', 'utf8', '-m',
                    'agentplat.demo', '--host', '127.0.0.1', '--port', str(port), '--corpus', '5000', '--unlimited'],
                    cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                status(state='running', service_pid=child.pid, log=str(log_path))
                code = child.wait()
            if code == 0:
                status(state='stopped', reason='clean_exit', exit_code=code)
                return 0
            now = time.monotonic()
            delay = restart_delay(exits, now)
            exits.append(now)
            if delay is None:
                status(state='blocked', reason='repeated_crashes', exit_code=code)
                return 1
            status(state='backoff', delay_s=delay, exit_code=code)
            time.sleep(delay)
    finally:
        lock.close()


if __name__ == '__main__':
    raise SystemExit(supervise())
