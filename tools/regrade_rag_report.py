"""Recheck existing RAG artifacts without rerunning models or rewriting history."""
import argparse
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agentplat.benchmark import grade, GRADER_VERSION


def regrade(report_path, output):
    report_path=Path(report_path).resolve();output=Path(output).resolve()
    rows=json.loads(report_path.read_text(encoding='utf-8'))['results']
    results=[]
    for row in rows:
        if row['task'] not in ('rag_policy','rag_conflict','rag_large'):continue
        folder=report_path.parent/f"{row['task']}-{row['repeat']}"
        # Missing archives are unknown, not a new failed task run.
        databases=list((folder/'private-knowledge').glob('*/index.sqlite3'))
        if not databases or not (folder/'workspace'/'result.json').is_file():
            passed=None;reason='Archived artifact or knowledge database unavailable'
        else:
            with patch.dict(os.environ,{'AGENTLAB_KB_DIR':str(folder/'private-knowledge')}):
                passed,reason=grade(row['task'],folder/'workspace',folder/'unused-grader')
        results.append(dict(task=row['task'],repeat=row['repeat'],original_passed=row.get('passed'),
                            regraded_passed=passed,reason=reason))
    result=dict(kind='grader_correction',source_report=str(report_path),grader_version=GRADER_VERSION,
                model_calls=0,results=results,note='Existing artifacts only; this is not another independent model trial.')
    with output.open('x',encoding='utf-8') as f:json.dump(result,f,ensure_ascii=False,indent=2)
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('report');parser.add_argument('output')
    args=parser.parse_args();print(json.dumps(regrade(args.report,args.output),ensure_ascii=False,indent=2))
