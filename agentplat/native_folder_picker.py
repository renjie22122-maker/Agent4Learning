"""Show a native multi-folder dialog only following an authenticated user click."""
import json,os,subprocess,threading
from pathlib import Path
_slot=threading.Lock()

def select_folders():
    if os.name!='nt':raise ValueError('此系统请使用路径输入或网页中的目录浏览')
    if not _slot.acquire(blocking=False):raise ValueError('文件夹选择窗口已经打开，请完成或关闭它')
    try:
        source=Path(__file__).with_name('folder_picker.cs').read_text(encoding='utf-8')
        command="[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; Add-Type -TypeDefinition @'\n"+source+"\n'@; ConvertTo-Json -InputObject @([AgentFolderPicker]::Select()) -Compress"
        result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-STA','-Command',command],capture_output=True,timeout=300,creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:raise RuntimeError('无法打开系统文件夹选择窗口')
        paths=json.loads(result.stdout.decode('utf-8-sig').strip())
        if not isinstance(paths,list) or any(not isinstance(p,str) for p in paths):raise ValueError('选择结果无效')
        if len(paths)>16:raise ValueError('最多选择 16 个文件夹')
        return paths
    except subprocess.TimeoutExpired:raise ValueError('选择窗口已超时，请重新打开') from None
    finally:_slot.release()
