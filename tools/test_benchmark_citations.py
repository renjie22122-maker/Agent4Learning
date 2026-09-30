"""Positive and negative controls for scoped, source-bound RAG grading."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentplat.benchmark import TASKS, grade
from agentplat.knowledge import KnowledgeBase, database_root


class CitationTests(unittest.TestCase):
    def test_scopes_versions_and_original_source(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {'AGENTLAB_KB_DIR':td}):
            root=Path(td)/'workspace';root.mkdir()
            kb=KnowledgeBase(database_root(root))
            citations={};documents={}
            for name,content in TASKS['rag_policy']['knowledge'].items():
                path=Path(td)/name;path.write_text(content,encoding='utf-8')
                documents[name]=kb.import_file(path)['document_id']
                with kb.connect() as db:
                    citations[name]=db.execute('SELECT id FROM chunks WHERE document_id=?',(documents[name],)).fetchone()[0]
            good=citations['current.txt'];old=citations['old.txt']
            def check(citation,expected,amount=680):
                (root/'result.json').write_text(json.dumps(dict(hotel_limit=amount,currency='CNY',source=citation)),encoding='utf-8')
                self.assertEqual(grade('rag_policy',root,root/'grader')[0],expected,citation)
            for citation in ('kb:'+good,'kb:project:'+good):check(citation,True)
            for citation in ('kb:public:'+good,'kb:session:'+good,'project:'+good,'kb:project:missing','kb:project:'+old):
                check(citation,False)
            check('kb:project:'+good,False,400)
            kb.remove(documents['current.txt']);check('kb:project:'+good,False)
            # An active document with the right name but different contents isn't evidence.
            path=Path(td)/'current.txt';path.write_text('Forged policy: 680 CNY',encoding='utf-8')
            doc=kb.import_file(path)
            with kb.connect() as db:
                forged=db.execute('SELECT id FROM chunks WHERE document_id=?',(doc['document_id'],)).fetchone()[0]
            check('kb:project:'+forged,False)


if __name__=='__main__':unittest.main()
