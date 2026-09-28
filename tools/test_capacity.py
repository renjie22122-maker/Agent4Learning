from pathlib import Path
import sys, unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentplat.llmconfig import LLMConfig
from agentplat import model_capacity as capacity


class CapacityTests(unittest.TestCase):
    def test_metadata_model_endpoint_and_manual(self):
        cfg = LLMConfig(base_url='https://example.test/v1', model='a')
        with patch.dict(capacity._cache, {capacity._key(cfg):(0, {'a':1048576, 'b':128000})}, clear=True):
            self.assertEqual(cfg.resolved_context_window(),1048576)
            self.assertEqual(cfg.resolved_context_window('b'),128000)
            self.assertEqual(cfg.resolved_context_window('unknown'),64000)
            cfg.context_window=50000
            self.assertEqual(cfg.resolved_context_window('b'),50000)
            cfg.context_window=0; cfg.base_url='https://other.test/v1'
            self.assertEqual(cfg.resolved_context_window(),64000)

    def test_metadata_validation_and_official_fallback(self):
        data=capacity.parse({'data':[{'id':'a','context_window':1048576},{'id':'b','context_length':128000},{'id':'bad','context_window':-1},{'id':'flag','context_window':True}]})
        self.assertEqual(data,{'a':1048576,'b':128000})
        cfg=LLMConfig(base_url='https://api.deepseek.com',model='deepseek-flash')
        self.assertEqual(cfg.resolved_context_window(),1000000)
        cfg.base_url='https://api.deepseek.com.fake.test'
        self.assertEqual(cfg.resolved_context_window(),64000)


if __name__=='__main__': unittest.main(verbosity=2)
