import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from studio.pipeline import load_bundle,validate_bundle,checksum
p=argparse.ArgumentParser();p.add_argument('folder',type=Path);args=p.parse_args()
b=load_bundle(args.folder);r=validate_bundle(b)
for name,h in b['manifest'].get('checksums',{}).items():
    if checksum(args.folder/name)!=h:r['errors'].append('Checksum mismatch: '+name)
r['passed']=not r['errors'];print(json.dumps(r,indent=2));sys.exit(0 if r['passed'] else 1)
