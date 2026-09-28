import tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
from agentplat.knowledge import KnowledgeBase,snapshot
from agentplat import vector_knowledge as vectors


class FakeEmbedding:
    key='fixture-semantic-v1'
    def encode(self,texts,query=False):
        return [(i,np.asarray([1,0] if '猫' in t or '喵' in t else [0,1],dtype='<f4')) for i,t in enumerate(texts)]


class VectorKnowledgeTests(unittest.TestCase):
    def test_index_scope_incremental_revocation_and_model_version(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);kb=KnowledgeBase(root/'kb');doc=root/'cat.txt';doc.write_text('猫喜欢睡觉',encoding='utf-8');kb.import_file(doc)
            model=FakeEmbedding()
            self.assertEqual(vectors.build(kb,embedder=model)['indexed_now'],1)
            self.assertEqual(vectors.build(kb,embedder=model)['indexed_now'],0)
            hits=vectors.search(kb,'喵',embedder=model);self.assertEqual(len(hits),1)
            other=KnowledgeBase(root/'other');self.assertFalse(vectors.search(other,'喵',embedder=model))
            snapshot(kb.root,root/'copy');kb.remove(kb.list_documents()[0]['id'])
            self.assertFalse(vectors.search(kb,'喵',embedder=model))
            self.assertTrue(vectors.search(KnowledgeBase(root/'copy'),'喵',embedder=model))
            model.key='fixture-v2';self.assertFalse(vectors.search(KnowledgeBase(root/'copy'),'喵',embedder=model))

    def test_unavailable_model_explicitly_falls_back(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);kb=KnowledgeBase(root/'kb');doc=root/'cat.txt';doc.write_text('猫喜欢睡觉',encoding='utf-8');kb.import_file(doc)
            with patch.object(vectors,'config',return_value={'enabled':True,'model_path':str(root/'missing')}):
                result=kb.search('猫')
                self.assertTrue(result['hits']);self.assertEqual(result['method'],'lexical fallback');self.assertTrue(result['warning'])


if __name__=='__main__':unittest.main()
