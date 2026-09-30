"""Print a non-destructive spill retention audit; no model calls or deletions."""
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agentplat.artifact_retention import audit_spills

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('workspace');p.add_argument('--references',nargs='+',required=True)
    p.add_argument('--inactive',action='store_true',help='Caller has checked that no run uses this workspace')
    p.add_argument('--min-age-days',type=float,default=30)
    args=p.parse_args()
    if args.min_age_days<0:p.error('min-age-days must be nonnegative')
    print(json.dumps(audit_spills(args.workspace,args.references,active=not args.inactive,min_age_days=args.min_age_days),ensure_ascii=False,indent=2))
