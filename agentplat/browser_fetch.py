"""Pinned transport for the Node browser worker. No arbitrary headers or methods."""
import base64
import json
from pathlib import Path
import sys
from .sources import SourceStore
from .web_policy import policy,allowed

if __name__ == '__main__':
    store = SourceStore(Path(sys.argv[1])); store.policy_provider = policy
    result = store.fetch(sys.argv[2], max_bytes=2_000_000, timeout_s=10)
    raw = (Path(sys.argv[1])/result['path']).read_bytes()
    print(json.dumps({'body':base64.b64encode(raw).decode(), 'content_type':result['content_type']}))
