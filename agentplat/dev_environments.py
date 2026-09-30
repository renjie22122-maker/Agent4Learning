"""Host-owned, versioned environment plans. Every execution needs fresh approval.

This manages provisioning, not containment: probes and installers execute with
host user privileges. Registering an environment never changes shell policy.
"""
import hashlib,json,os,re,shutil,sqlite3,sys,time
from contextlib import contextmanager
from pathlib import Path,PurePosixPath
from urllib.parse import urlsplit
from .filesystem_contract import check_entry,walk_files

BASE=Path(__file__).resolve().parents[1]/'.agent-runtime'/'environments'


def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def alive(pid):
    if os.name=='nt':
        import ctypes
        from ctypes import wintypes
        api=ctypes.WinDLL('kernel32',use_last_error=True)
        api.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD];api.OpenProcess.restype=wintypes.HANDLE
        handle=api.OpenProcess(0x1000,False,pid)
        if not handle:
            if ctypes.get_last_error()==87:return False
            raise PermissionError('Cannot inspect provisioning process')
        api.GetExitCodeProcess.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)]
        api.CloseHandle.argtypes=[wintypes.HANDLE]
        try:
            code=wintypes.DWORD()
            if not api.GetExitCodeProcess(handle,ctypes.byref(code)):raise PermissionError('Cannot inspect process status')
            return code.value==259
        finally:api.CloseHandle(handle)
    try:os.kill(pid,0)
    except ProcessLookupError:return False
    return True


def fingerprints(folder):
    return {str(p.relative_to(folder)):hashlib.sha256(p.read_bytes()).hexdigest()
            for p in walk_files(folder,ignored={'__pycache__'})}


def summary(record):
    receipt=record['receipt']
    return {**record,'receipt':{**{k:v for k,v in receipt.items() if k!='files'},
                               'fingerprinted_files':len(receipt.get('files',{}))}}


def validate(spec):
    required={'name','version','kind','runtime','version_args','smoke'}
    if not required<=spec.keys():raise ValueError('方案需要 name/version/kind/runtime/version_args/smoke')
    if set(spec)-required-{'url','sha256','interpreter','packages','archive'}:raise ValueError('未知环境方案字段')
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,60}',spec['name']) or not isinstance(spec['version'],str) or not 1<=len(spec['version'])<=80:
        raise ValueError('环境名称或版本无效')
    if spec['kind'] not in ('portable','venv','existing'):raise ValueError('支持 portable、venv、existing')
    for args in [spec['version_args'],*spec['smoke']]:
        if not isinstance(args,list) or not args or any(not isinstance(a,str) or '\x00' in a for a in args):raise ValueError('探针必须为非空 argv 数组')
    if not 1<=len(spec['smoke'])<=4:raise ValueError('需要 1–4 个最小功能探针')
    if len(json.dumps(spec,ensure_ascii=False))>3000:raise ValueError('方案过长，请保留最小探针')
    runtime=Path(spec['runtime'])
    if spec['kind']=='existing':
        if not runtime.is_absolute():raise ValueError('已有环境必须指定绝对可执行文件路径')
    elif runtime.is_absolute() or '..' in runtime.parts or ':' in spec['runtime'] or '\\' in spec['runtime']:
        raise ValueError('受管理环境 runtime 必须为相对路径，使用 / 分隔')
    if spec['kind']=='portable':
        url=urlsplit(spec.get('url',''))
        if url.scheme!='https' or not url.hostname or url.username or url.password or url.fragment:
            raise ValueError('归档需 HTTPS 来源，不允许 URL 凭据')
        if not re.fullmatch('[0-9a-fA-F]{64}',spec.get('sha256','')):raise ValueError('必须提供归档 SHA256')
        if spec.get('archive') not in ('zip','tar'):raise ValueError('显式选择 zip 或 tar')
    if spec['kind']=='venv':
        if not Path(spec.get('interpreter','')).is_absolute():raise ValueError('需已知 Python 解释器绝对路径')
        if spec['runtime']!=('Scripts/python.exe' if os.name=='nt' else 'bin/python'):raise ValueError('venv runtime 路径不正确')
        if len(spec.get('packages',[]))>30 or any(not re.fullmatch(r'[A-Za-z0-9_.-]+==[A-Za-z0-9_.+!-]+',p) for p in spec.get('packages',[])):
            raise ValueError('包必须为固定 name==version；不支持安装脚本或未固定版本')
    return spec


class Environments:
    def __init__(self,root=None):self.root=Path(root or BASE).absolute()

    @contextmanager
    def db(self):
        self.root.mkdir(parents=True,exist_ok=True);check_entry(self.root)
        registry=self.root/'registry.sqlite3'
        if registry.exists():check_entry(registry)
        db=sqlite3.connect(registry,timeout=15)
        try:
            db.row_factory=sqlite3.Row
            db.execute('CREATE TABLE IF NOT EXISTS environments(id TEXT PRIMARY KEY,plan TEXT,state TEXT,pid INTEGER,receipt TEXT,updated REAL)')
            yield db
            db.commit()
        except BaseException:
            db.rollback();raise
        finally:db.close()

    def create(self,workspace,spec):
        validate(spec)
        plan=dict(schema_version=1,workspace=str(Path(workspace).resolve()),spec=spec)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            same=[]
            for row in db.execute('SELECT id,plan,state FROM environments ORDER BY updated DESC'):
                old=json.loads(row['plan'])
                if old.get('workspace')==plan['workspace'] and old.get('spec')==spec:same.append(row)
            reusable=next((r for r in same if r['state']!='retired'),None)
            if reusable:key=reusable['id']
            else:
                if same:
                    import uuid
                    plan['generation']=uuid.uuid4().hex
                key=digest(plan)
                db.execute('INSERT INTO environments VALUES(?,?,?,?,?,?)',(key,json.dumps(plan,ensure_ascii=False),'planned',None,'{}',time.time()))
        return self.get(key)

    def get(self,key):
        if not re.fullmatch('[0-9a-f]{64}',key):raise ValueError('无效环境 ID')
        with self.db() as db:row=db.execute('SELECT * FROM environments WHERE id=?',(key,)).fetchone()
        if not row:raise ValueError('环境不存在')
        result=dict(row);result['plan']=json.loads(result['plan']);result['receipt']=json.loads(result['receipt'])
        if digest(result['plan'])!=key:raise ValueError('环境方案被修改')
        result['directory']=str(self.root/'managed'/key)
        result['execution_boundary']='host provisioning only; ordinary sandbox policy unchanged'
        return result

    def list(self,workspace):
        with self.db() as db:keys=[r[0] for r in db.execute('SELECT id FROM environments ORDER BY updated DESC')]
        return [r for r in map(self.get,keys) if r['plan']['workspace']==str(Path(workspace).resolve())]

    def _state(self,key,state,receipt):
        with self.db() as db:db.execute('UPDATE environments SET state=?,pid=NULL,receipt=?,updated=? WHERE id=?',(state,json.dumps(receipt,ensure_ascii=False),time.time(),key))

    def operate(self,key,action,expected_hash):
        if key!=expected_hash:raise ValueError('批准的方案哈希不匹配')
        row=self.get(key);spec=row['plan']['spec'];folder=Path(row['directory'])
        if action not in ('prepare','verify','retire'):raise ValueError('未知操作')
        # Transactional claim prevents two callers preparing/cleaning the same runtime.
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            state,pid,current_receipt=db.execute('SELECT state,pid,receipt FROM environments WHERE id=?',(key,)).fetchone()
            row['receipt']=json.loads(current_receipt)
            if pid:
                if alive(pid):raise RuntimeError('环境仍有操作在运行')
            if action=='prepare' and state!='planned':raise RuntimeError('不得重复安装；现有或中断环境请先 verify 核对，必要时 retire 后创建新方案')
            if action=='verify' and state in ('planned','retired'):raise RuntimeError('尚未准备或已停用，不能验证复用')
            db.execute('UPDATE environments SET state=?,pid=?,updated=? WHERE id=?',('preparing' if action=='prepare' else action,os.getpid(),time.time(),key))
        receipt={'action':action,'started_at':time.time(),'plan_hash':key,'checks':[]}
        try:
            if folder.parent.exists():check_entry(folder.parent)
            if action=='retire':
                # Existing host installations are never moved or removed.
                if spec['kind']!='existing' and folder.exists():
                    folder.resolve().relative_to((self.root/'managed').resolve())
                    list(walk_files(folder))
                    trash=self.root/'retired';trash.mkdir(exist_ok=True);check_entry(trash)
                    destination=trash/(key+'-'+str(time.time_ns()))
                    folder.rename(destination);receipt['quarantine']=str(destination)
                receipt['note']='停用记录；受管理目录移至回收区，未永久删除；已有宿主环境未改动'
                self._state(key,'retired',receipt);return self.get(key)
            if action=='prepare':
                if spec['kind']!='existing':
                    folder.parent.mkdir(parents=True,exist_ok=True);check_entry(folder.parent)
                    folder.mkdir(exist_ok=False)
                if spec['kind']=='portable':self._archive(spec,folder)
                elif spec['kind']=='venv':
                    self._run([spec['interpreter'],'-m','venv',str(folder)],folder,receipt)
                    if spec.get('packages'):
                        self._run([str(folder/spec['runtime']),'-m','pip','--isolated','install','--disable-pip-version-check',
                                   '--only-binary=:all:','--index-url','https://pypi.org/simple',*spec['packages']],folder,receipt)
                    receipt['resolved_packages']=self._run([str(folder/spec['runtime']),'-m','pip','freeze','--all'],folder,receipt)
            runtime=Path(spec['runtime']) if spec['kind']=='existing' else folder/spec['runtime']
            if not runtime.is_file():raise RuntimeError('运行时文件缺失；未判定宿主安装损坏')
            check_entry(runtime)
            previous=row['receipt']
            if action=='verify' and previous.get('files') and fingerprints(folder)!=previous['files']:
                raise RuntimeError('受管理环境内容已改变，拒绝自动执行或复用；请核对并准备新的固定版本方案')
            if action=='verify' and previous.get('runtime_sha256') and hashlib.sha256(runtime.read_bytes()).hexdigest()!=previous['runtime_sha256']:
                raise RuntimeError('运行时文件已改变，拒绝自动复用')
            root=Path(row['plan']['workspace']) if spec['kind']=='existing' else folder
            probe=self._run([str(runtime),*spec['version_args']],root,receipt)
            if not re.search(r'(?<![A-Za-z0-9.])'+re.escape(spec['version'])+r'(?![A-Za-z0-9.])',probe):
                raise RuntimeError('实际版本输出不匹配方案版本')
            import tempfile
            with tempfile.TemporaryDirectory(prefix='env-smoke-',dir=self.root) as td:
                for command in spec['smoke']:
                    argv=[a.replace('{runtime}',str(runtime)).replace('{env}',str(folder)).replace('{scratch}',td) for a in command]
                    self._run(argv,Path(td),receipt)
            receipt.update(runtime=str(runtime),runtime_sha256=hashlib.sha256(runtime.read_bytes()).hexdigest(),verified_at=time.time(),
                           note='仅证明所列版本及最小探针；复用前仍需 verify，不授予后续宿主权限')
            if spec['kind']!='existing':receipt['files']=fingerprints(folder)
            self._state(key,'ready',receipt)
        except BaseException as exc:
            for field in ('files','runtime_sha256','runtime'):
                if field in row['receipt']:receipt[field]=row['receipt'][field]
            receipt.update(error=type(exc).__name__+': '+str(exc)[:1200],note='可能已有部分副作用；不能自动重新安装')
            self._state(key,'needs_inspection',receipt)
            raise
        return self.get(key)

    def _run(self,argv,cwd,receipt):
        from .processes import ProcessSupervisor
        from .execution_environment import task_environment
        supervisor=ProcessSupervisor()
        try:
            key=supervisor.start(argv,cwd,shell=False,timeout_s=180,env=task_environment(),interactive=False)
            result=supervisor.wait(key,.2)
            while result['status']=='running':result=supervisor.wait(key,.2)
            receipt['checks'].append(dict(argv=argv,status=result['status'],exit_code=result.get('exit_code'),output=result.get('output','')[-3000:]))
            if result['status']!='exited' or result['exit_code']!=0:raise RuntimeError('环境命令失败或超时；请查看检查记录')
            return result.get('output','')
        finally:supervisor.close()

    def _archive(self,spec,folder):
        import urllib.request,zipfile,tarfile,stat
        archive=folder/'download.archive'
        with urllib.request.urlopen(spec['url'],timeout=60) as response,archive.open('xb') as stream:
            if urlsplit(response.url).scheme!='https':raise ValueError('拒绝非 HTTPS 重定向')
            total=0;checksum=hashlib.sha256()
            while True:
                block=response.read(1024*1024)
                if not block:break
                total+=len(block)
                if total>1_000_000_000:raise ValueError('归档超过 1 GB')
                checksum.update(block);stream.write(block)
        if checksum.hexdigest()!=spec['sha256'].lower():raise ValueError('下载归档 SHA256 不匹配')
        def path(name):
            p=PurePosixPath(name)
            if p.is_absolute() or '..' in p.parts or '\\' in name or ':' in name:raise ValueError('归档路径越界')
            if any(part.endswith((' ','.')) or re.fullmatch(r'(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?',part) for part in p.parts):
                raise ValueError('归档包含不安全的 Windows 路径组件')
            target=folder.joinpath(*p.parts)
            target.resolve().relative_to(folder.resolve())
            return target
        if spec['archive']=='zip':
            with zipfile.ZipFile(archive) as bundle:
                entries=bundle.infolist()
                if len(entries)>100000 or sum(e.file_size for e in entries)>2_000_000_000:raise ValueError('展开归档超过限制')
                for entry in entries:
                    if stat.S_ISLNK(entry.external_attr>>16):raise ValueError('归档链接不允许')
                    target=path(entry.filename)
                    if entry.is_dir():target.mkdir(parents=True,exist_ok=True);continue
                    target.parent.mkdir(parents=True,exist_ok=True)
                    with bundle.open(entry) as source,target.open('xb') as out:shutil.copyfileobj(source,out)
                    if os.name!='nt':target.chmod((entry.external_attr>>16)&0o755 or 0o644)
        else:
            with tarfile.open(archive) as bundle:
                count=0;size=0
                for entry in bundle:
                    count+=1;size+=entry.size
                    if count>100000 or size>2_000_000_000:raise ValueError('展开归档超过限制')
                    target=path(entry.name)
                    if entry.isdir():target.mkdir(parents=True,exist_ok=True);continue
                    if not entry.isfile():raise ValueError('归档链接或特殊文件不允许')
                    target.parent.mkdir(parents=True,exist_ok=True)
                    with bundle.extractfile(entry) as source,target.open('xb') as out:shutil.copyfileobj(source,out)
                    if os.name!='nt':target.chmod(entry.mode&0o755)


def install(agent):
    from .agent_tools import AgentTool,_obj
    from .approvals import check_authority,execute
    import subprocess
    store=Environments()
    def plan(spec):
        check_authority(agent)
        return summary(store.create(agent.ws.root,spec))
    def inspect(executables):
        if not 1<=len(executables)<=12 or any(not re.fullmatch(r'[A-Za-z0-9_.+-]{1,60}',n) for n in executables):
            raise ValueError('仅接受 1–12 个可执行程序名，不接受路径或命令')
        return dict(host_path_hints={n:shutil.which(n) for n in executables},host_python=sys.executable,
                    ordinary_backend=agent.ws.execution_mode,
                    note='仅为宿主 PATH 元数据，不保证版本、功能或沙箱可见性。登记已有环境后需授权运行版本与功能探针；先复用，勿直接重装。')
    def operate(environment_id,action,reason,timeout_s=900):
        check_authority(agent);row=store.get(environment_id)
        if row['plan']['workspace']!=str(agent.ws.root.resolve()):raise PermissionError('环境属于另一项目')
        from .human_input import request_command
        helper=Path(__file__).resolve().parents[1]/'tools'/'manage_environment.py'
        argv=[sys.executable,str(helper),'--root',str(store.root),'--id',environment_id,'--hash',environment_id,'--action',action]
        note=reason+'\n环境操作：'+action+'\n'+json.dumps(row['plan'],ensure_ascii=False)+'\n安装及探针在宿主执行，可能联网；不修改后续沙箱权限。停用仅回收受管理目录，不删除宿主已有安装。'
        answer=request_command(agent,subprocess.list2cmdline(argv),note,timeout_s,argv=argv)
        if answer.get('status')!='approved':return dict(executed=False,decision=answer)
        result=execute(agent,answer['request_id'])
        return dict(executed=True,result=result,environment=summary(store.get(environment_id)))
    def wrap(fn):return lambda **args:json.dumps(fn(**args),ensure_ascii=False)
    agent.tools['inspect_development_environment']=AgentTool('inspect_development_environment','Read host toolchain location hints without executing programs. Missing PATH entries do not imply damage. Inspect before proposing an environment installation.',_obj({'executables':{'type':'array','items':{'type':'string'},'minItems':1,'maxItems':12}},['executables']),wrap(inspect))
    agent.tools['list_development_environments']=AgentTool('list_development_environments','List registered environments and observed states for this project. Ready means prior probes passed; it does not grant sandbox access.',_obj({},[]),wrap(lambda:[summary(r) for r in store.list(agent.ws.root)]))
    agent.tools['plan_development_environment']=AgentTool('plan_development_environment','Propose an immutable versioned environment plan without installing. Inspect reusable environments first. Supports an existing executable, pinned Python venv, or HTTPS ZIP/TAR with SHA256. Specify version arguments and minimal functional probes as argv arrays; placeholders: {runtime}/{env}/{scratch}.',
        _obj({'spec':{'type':'object','properties':{'name':{'type':'string'},'version':{'type':'string'},'kind':{'type':'string','enum':['existing','venv','portable']},'runtime':{'type':'string'},'version_args':{'type':'array','items':{'type':'string'}},'smoke':{'type':'array','items':{'type':'array','items':{'type':'string'}}},'interpreter':{'type':'string'},'packages':{'type':'array','items':{'type':'string'}},'url':{'type':'string'},'sha256':{'type':'string'},'archive':{'type':'string','enum':['zip','tar']}},'required':['name','version','kind','runtime','version_args','smoke'],'additionalProperties':False}},['spec']),wrap(plan),True)
    agent.tools['prepare_development_environment']=AgentTool('prepare_development_environment','Request single-use host approval for an existing plan: prepare once, verify interrupted state or before reuse, or retire managed files to recoverable quarantine. Existing host installations are never removed. Each operation needs fresh approval; never blindly reinstall.',
        _obj({'environment_id':{'type':'string'},'action':{'type':'string','enum':['prepare','verify','retire']},'reason':{'type':'string'},'timeout_s':{'type':'number','minimum':1,'maximum':3600}},['environment_id','action','reason']),wrap(operate),True)
