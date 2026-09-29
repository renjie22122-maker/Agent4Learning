"""Durable host-supplied logical operation IDs; unknown outcomes never replay."""
import json
import sqlite3
from pathlib import Path
from dataclasses import asdict
from contextlib import contextmanager


class OperationLedger:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        try:
            with db:
                yield db
        finally:
            db.close()

    def execute(self, identity, arguments, operation):
        from .tools import ToolError, ToolResult
        self.path.parent.mkdir(parents=True, exist_ok=True)
        key=json.dumps(identity,sort_keys=True); payload=json.dumps(arguments,sort_keys=True)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY,args TEXT,status TEXT,result TEXT)')
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT args,status,result FROM operations WHERE id=?',(key,)).fetchone()
            if row:
                if row[0]!=payload: raise ToolError('OPERATION_CONFLICT','同一逻辑操作 ID 不能改变参数')
                if row[1]!='completed': raise ToolError('OUTCOME_UNKNOWN','此逻辑操作结果未知；需先核对外部状态，不能自动重放')
                return ToolResult(**json.loads(row[2]))
            db.execute('INSERT INTO operations VALUES(?,?,?,?)',(key,payload,'started',''))
        # Record intent before side effects. A crash or timeout leaves started.
        result=operation()
        with self.connect() as db:
            db.execute('UPDATE operations SET status=?,result=? WHERE id=?',('completed',json.dumps(asdict(result)),key))
        return result

    def save_reference(self, reference, text):
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS results (ref TEXT PRIMARY KEY, text TEXT)')
            db.execute('INSERT OR REPLACE INTO results VALUES (?,?)', (reference,text))

    def read_reference(self, reference):
        if not self.path.exists():raise KeyError(reference)
        with self.connect() as db:
            exists=db.execute("SELECT 1 FROM sqlite_master WHERE name='results'").fetchone()
            row=db.execute('SELECT text FROM results WHERE ref=?',(reference,)).fetchone() if exists else None
        if row is None:raise KeyError(reference)
        return row[0]
