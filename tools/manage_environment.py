"""Host-only execution target for an exact, user-approved environment plan."""
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agentplat.dev_environments import Environments,summary

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',required=True);p.add_argument('--id',required=True)
    p.add_argument('--hash',required=True);p.add_argument('--action',choices=['prepare','verify','retire'],required=True)
    args=p.parse_args()
    print(json.dumps(summary(Environments(args.root).operate(args.id,args.action,args.hash)),ensure_ascii=False,indent=2))
