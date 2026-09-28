"""执行后端配置。工作区副本、进程回收与 OS 沙箱是不同能力。"""
import os
import shutil
import json
from pathlib import Path

POLICY_PATH = Path(__file__).resolve().parents[1] / '.agent-runtime' / 'execution-policy.json'


def saved_policy():
    return json.loads(POLICY_PATH.read_text(encoding='utf-8')) if POLICY_PATH.exists() else {}


def native_network_policy():
    policy = os.environ.get('AGENTLAB_NATIVE_NETWORK', saved_policy().get('native_network', 'deny'))
    if policy not in {'host', 'deny'}:
        raise ValueError('AGENTLAB_NATIVE_NETWORK 必须为 host 或 deny')
    return policy


def execution_mode():
    mode = os.environ.get('AGENTLAB_EXECUTION_MODE', saved_policy().get('mode', 'local')).strip().lower()
    if mode not in {'local', 'native', 'docker', 'disabled'}:
        raise ValueError('AGENTLAB_EXECUTION_MODE 必须为 local、native、docker 或 disabled')
    return mode


def execution_status(mode=None):
    mode = mode or execution_mode()
    docker = bool(shutil.which('docker'))
    labels = {
        'native': ('Windows AppContainer：文件隔离；网络遵循宿主' if native_network_policy() == 'host' else 'Windows AppContainer：文件隔离；严格网络预检') if os.name == 'nt' else '原生 Windows 沙箱不可用',
        'local': '本机执行：进程受监督，无 OS 沙箱',
        'docker': 'Docker：执行时检查服务与镜像' if docker else 'Docker 未安装：已选择的容器执行不可用',
        'disabled': '命令执行已禁用',
    }
    return {
        'execution_mode': mode, 'execution_label': labels[mode],
        'docker_cli_available': docker,
        'shell_available': (None if os.name == 'nt' else False) if mode == 'native' else True if mode == 'local' else None if mode == 'docker' and docker else False,
        'sandbox_ready': None if (mode == 'native' and os.name == 'nt') or (mode == 'docker' and docker) else False,
        'native_supported': os.name == 'nt',
        'network_policy': native_network_policy() if mode == 'native' else 'deny' if mode == 'docker' else 'host',
        'native_network_check': 'strict mode refuses untrusted command unless OS denial is observed',
        'sandbox_check': 'AppContainer creation checked on execution' if mode == 'native' else 'daemon and image checked before each execution' if mode == 'docker' else 'no OS sandbox',
        'local_is_sandboxed': False,
        'process_tree_supervised': True,
    }
