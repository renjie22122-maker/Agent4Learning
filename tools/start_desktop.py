"""Desktop launcher: reuse authenticated service, otherwise start it hidden."""
import ctypes
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parents[1]
BASE = 'http://127.0.0.1:8800'
RUNTIME = ROOT / '.agent-runtime'


def access_url():
    try:
        record = json.loads((RUNTIME / 'desktop-access.json').read_text(encoding='utf-8'))
        token = record['token']
        request = urllib.request.Request(BASE + '/api/runtime', headers={'Cookie': 'agentlab_access=' + token})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=2) as response:
            state = json.load(response)
        if not state.get('version', '').startswith('runtime-'):
            return None
        return BASE + '/auth?token=' + urllib.parse.quote(token, safe='')
    except (OSError, ValueError, KeyError):
        return None


def launch(open_browser=True):
    # Serialize double clicks; never stop/restart an already running task.
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.ReleaseMutex.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    mutex = kernel.CreateMutexW(None, False, 'Local\\Agent4LearningDesktopLauncher8800')
    if not mutex:
        raise ctypes.WinError(ctypes.get_last_error())
    acquired = False
    try:
        acquired = kernel.WaitForSingleObject(mutex, 45000) in (0, 0x80)
        if not acquired:
            raise RuntimeError('另一个启动操作尚未结束，请稍后重试。')
        url = access_url()
        if not url:
            # A service outlives its caller. Do not persist the restricted
            # executor's intentionally dead network proxy into that service.
            for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY'):
                proxy = urllib.parse.urlsplit(os.environ.get(name, os.environ.get(name.lower(), '')))
                if proxy.hostname in ('127.0.0.1', 'localhost') and proxy.port == 9:
                    raise RuntimeError('当前启动环境带有受限代理 127.0.0.1:9。请通过桌面启动器或经授权的宿主环境启动；未修改系统代理。')
            with socket.socket() as probe:
                if probe.connect_ex(('127.0.0.1', 8800)) == 0:
                    raise RuntimeError('8800 端口已被占用，但无法验证为当前项目的服务。未中止任何进程。')
            RUNTIME.mkdir(exist_ok=True)
            python = Path(sys.executable).with_name('python.exe')
            with (RUNTIME / 'desktop-service.log').open('ab') as log:
                child = subprocess.Popen(
                    [str(python), '-u', '-X', 'utf8', str(ROOT / 'tools' / 'service_supervisor.py')],
                    cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                    creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
                    close_fds=True)
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                if child.poll() is not None:
                    raise RuntimeError('服务启动失败，请查看：' + str(RUNTIME / 'desktop-service.log'))
                url = access_url()
                if url:
                    break
                time.sleep(.4)
            if not url:
                raise RuntimeError('服务仍在初始化，请稍后再次双击入口。日志：' + str(RUNTIME / 'desktop-service.log'))
        if open_browser and not webbrowser.open(url):
            raise RuntimeError('服务已就绪，但系统默认浏览器无法打开。请检查默认浏览器设置。')
    finally:
        if acquired:
            kernel.ReleaseMutex(mutex)
        kernel.CloseHandle(mutex)


if __name__ == '__main__':
    try:
        launch(open_browser='--no-open' not in sys.argv)
    except Exception as exc:
        if '--no-open' in sys.argv:
            raise
        ctypes.windll.user32.MessageBoxW(None, str(exc), 'Agent4Learning 启动失败', 0x10)
        sys.exit(1)
