"""Host credentials are not inherited by task shell processes."""
import os

SAFE = frozenset({'SYSTEMROOT','WINDIR','COMSPEC','PATH','PATHEXT','TEMP','TMP',
                 'LANG','LC_ALL','TZ','NUMBER_OF_PROCESSORS','PROCESSOR_ARCHITECTURE',
                 # Normal profile/toolchain locations are not credentials. Removing
                 # these breaks expanduser(), configuration discovery and DLL lookup.
                 'HOME','USERPROFILE','HOMEDRIVE','HOMEPATH','APPDATA','LOCALAPPDATA',
                 'PROGRAMDATA','PROGRAMFILES','PROGRAMFILES(X86)','COMMONPROGRAMFILES',
                 'XDG_CONFIG_HOME','XDG_CACHE_HOME','XDG_DATA_HOME',
                 'VIRTUAL_ENV','CONDA_PREFIX','CONDA_DEFAULT_ENV','CONDA_SHLVL',
                 'CONDA_EXE','CONDA_PYTHON_EXE','JAVA_HOME','DOTNET_ROOT',
                 'CARGO_HOME','RUSTUP_HOME','NVM_HOME','NVM_SYMLINK'})


def task_environment(environment=None):
    source = os.environ if environment is None else environment
    result = {k:v for k,v in source.items() if k.upper() in SAFE}
    result.update(PYTHONIOENCODING='utf-8', PYTHONUTF8='1', PYTHONUNBUFFERED='1', PYTHONNOUSERSITE='1')
    return result


def describe(workspace):
    """Host-provided identity, not an assertion that dependencies work."""
    import sys
    return dict(ordinary_execution=getattr(workspace,'execution_mode','unknown'),
                host_python=sys.executable, shell='cmd.exe' if os.name=='nt' else '/bin/sh',
                note='普通命令保持原执行后端；原生沙箱 Python 是不含第三方包的独立运行时。'
                     'request_execution 仅将获批的那条命令交给宿主，后续 run_shell 不会自动切换。'
                     '宿主保留用户目录和工具链位置变量，过滤密钥及 Python 注入变量。'
                     '沙箱缺包或被拒绝不能证明宿主缺包或安装损坏；应先申请必要的只读宿主探测，避免直接重装或新建环境。')
