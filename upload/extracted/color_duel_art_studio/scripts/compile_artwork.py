"""CLI for compiling approved local master artwork; no AI or network required."""
import argparse,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from studio.pipeline import compile_image,make_export
from studio.models import BuildSettings
p=argparse.ArgumentParser()
p.add_argument('image',type=Path);p.add_argument('output',type=Path)
p.add_argument('--id',default='new-artwork');p.add_argument('--title',default='New artwork')
p.add_argument('--version',default='0.1.0');p.add_argument('--regions',type=int,default=650)
p.add_argument('--palette',type=int,default=32);p.add_argument('--tones',type=int,default=80)
p.add_argument('--edge',type=int,default=1024)
a=p.parse_args()
result=compile_image(a.image,a.output,artwork_id=a.id,version=a.version,title=a.title,
    settings=BuildSettings(target_regions=a.regions,palette_colors=a.palette,paint_colors=a.tones,max_edge=a.edge),
    progress=lambda pct,msg:print(f'{pct:4.0%} {msg}',flush=True))
(a.output.parent/(a.output.name+'.zip')).write_bytes(make_export(a.output,True))
print(f"{result['manifest']['regionCount']} playable regions. Inspect validation.json and review before publishing.")
