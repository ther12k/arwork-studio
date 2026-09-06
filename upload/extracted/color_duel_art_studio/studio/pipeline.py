"""Deterministic raster -> vector paint + independent playable-region compiler.

No raster images are embedded into emitted SVG. Shared boundaries use identical
pixel-edge coordinates; independent path simplification is intentionally avoided.
SLIC provides image-aware *draft* regions, not semantic object detection.
"""
from __future__ import annotations
import hashlib, html, json, math, shutil, zipfile
from collections import defaultdict
from pathlib import Path
from typing import Callable
import numpy as np
import cv2
from PIL import Image, ImageOps, ImageFilter
from scipy import ndimage as ndi
from skimage.segmentation import slic
from rasterio.features import shapes, rasterize
from shapely import make_valid
from shapely.geometry import shape, Polygon, Point, box
from shapely.ops import unary_union, polylabel
from .models import BuildSettings

SCHEMA = 'color-duel-detailed-vector-1'
INK = '#29383E'

def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False), encoding='utf-8')

def read_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8'))

def checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def clean_image(data: bytes, destination: Path) -> dict:
    from io import BytesIO
    if not data or len(data) > 12 * 1024 * 1024:
        raise ValueError('Use a PNG, JPEG or WebP under 12 MB.')
    try:
        with Image.open(BytesIO(data)) as source:
            if source.format not in ('PNG', 'JPEG', 'WEBP'):
                raise ValueError('Only PNG, JPEG and WebP images are accepted. SVG is not an upload format.')
            if source.width * source.height > 32_000_000:
                raise ValueError('Image exceeds 32 megapixels.')
            if min(source.size) < 64:
                raise ValueError('Image must be at least 64 pixels in each dimension.')
            if getattr(source, 'is_animated', False):
                raise ValueError('Use a still image, not an animation.')
            im = ImageOps.exif_transpose(source).convert('RGBA')
            background = Image.new('RGBA', im.size, 'white')
            background.alpha_composite(im)
            rgb = background.convert('RGB')
            destination.parent.mkdir(parents=True, exist_ok=True)
            rgb.save(destination, format='PNG')  # Drops EXIF and executable metadata.
            return {'width': rgb.width, 'height': rgb.height, 'sha256': checksum(destination)}
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValueError('The file could not be decoded safely as a still image.') from exc

def polygon_parts(geom):
    if geom.geom_type == 'Polygon':
        if not geom.is_empty and geom.area > 0:
            yield geom
    elif geom.geom_type in ('MultiPolygon','GeometryCollection'):
        for child in geom.geoms:
            yield from polygon_parts(child)

def rings_of(p: Polygon) -> list:
    return [[[float(x),float(y)] for x,y in ring.coords] for ring in [p.exterior,*p.interiors]]

def number(n: float) -> str:
    return str(int(n)) if n == int(n) else f'{n:.4f}'.rstrip('0').rstrip('.')

def path_of(rings: list) -> str:
    paths = []
    for ring in rings:
        coords = ring[:-1] if ring[0] == ring[-1] else ring
        paths.append('M ' + ' L '.join(f'{number(x)},{number(y)}' for x,y in coords) + ' Z')
    return ' '.join(paths)

def region_polygon(r):
    return Polygon(r['rings'][0], r['rings'][1:])

def make_label(poly: Polygon, palette_id: int, mask=None, origin=(0,0)) -> dict:
    if mask is not None:
        dist = ndi.distance_transform_edt(np.pad(mask, 1))
        yy,xx = np.unravel_index(np.argmax(dist),dist.shape)
        px,py = origin[0]+xx-1+.5, origin[1]+yy-1+.5
        point = Point(px,py)
        if not poly.contains(point):
            point = poly.representative_point()
    else:
        point = polylabel(poly, tolerance=.7)
    radius = float(poly.boundary.distance(point))
    # Text rectangle must fit inside the inscribed circle; account for digit count.
    size = min(22., radius * 1.6 / math.sqrt((len(str(palette_id))*.65)**2 + 1))
    return {'x':round(point.x,4),'y':round(point.y,4),'fontSize':round(size,3),
            'minScreenPx':9,'clearance':round(radius,3)}

def pack_region(poly, rid, pid, object_id='unassigned', label=None):
    rings = rings_of(poly)
    return {'id':rid,'paletteId':int(pid),'objectId':object_id,'d':path_of(rings),
            'fillRule':'evenodd','rings':rings,'bbox':list(map(float,poly.bounds)),
            'area':float(poly.area),'label':label or make_label(poly,pid)}

def merge_tiny(labels: np.ndarray, rgb: np.ndarray, minimum: int) -> np.ndarray:
    """Merge only adjacent tiny components; never bridge unrelated pieces."""
    labels = labels.astype(np.int32).copy()
    for _ in range(3):
        counts = np.bincount(labels.ravel())
        tiny = np.where((counts > 0) & (counts < minimum))[0]
        tiny = tiny[tiny != 0]
        if not len(tiny): break
        slices = ndi.find_objects(labels)
        for rid in tiny:
            sl = slices[rid-1] if rid-1 < len(slices) else None
            if sl is None: continue
            y,x = sl
            y0,y1=max(0,y.start-1),min(labels.shape[0],y.stop+1)
            x0,x1=max(0,x.start-1),min(labels.shape[1],x.stop+1)
            crop=labels[y0:y1,x0:x1]; pixels=rgb[y0:y1,x0:x1]
            own = crop==rid
            edge = ndi.binary_dilation(own, structure=ndi.generate_binary_structure(2,1)) & ~own
            neighbors = np.unique(crop[edge]); neighbors=neighbors[neighbors!=0]
            if not len(neighbors): continue
            mean=pixels[own].mean(0)
            target=min(neighbors, key=lambda n: float(np.sum((pixels[(crop==n)&edge].mean(0)-mean)**2)))
            crop[own]=target
    return labels

def palette_for_regions(means: np.ndarray, requested: int):
    from skimage.color import rgb2lab, lab2rgb
    labs = rgb2lab(np.asarray(means,dtype=float).reshape(-1,1,3)/255).reshape(-1,3).astype(np.float32)
    count = min(requested,len(labs),len(np.unique(np.round(labs,1),axis=0)))
    count = max(1,count)
    cv2.setRNGSeed(23)
    _, indexes, centers = cv2.kmeans(labs,count,None,
        (cv2.TERM_CRITERIA_EPS+cv2.TERM_CRITERIA_MAX_ITER,80,.1),1,cv2.KMEANS_PP_CENTERS)
    order=np.argsort(centers[:,0],kind='stable')
    inverse=np.empty(count,dtype=int); inverse[order]=np.arange(count)
    colors=np.clip(lab2rgb(centers[order].reshape(-1,1,3)).reshape(-1,3)*255,0,255).astype(np.uint8)
    palette=[]
    for i,c in enumerate(colors,1):
        hx='#'+''.join(f'{int(v):02X}' for v in c)
        palette.append({'id':i,'number':i,'name':f'Tone {i:02d}','hex':hx,
            'paint':{'type':'linearGradient','stops':[{'offset':0,'color':hx},{'offset':1,'color':hx}]}})
    return palette, inverse[indexes.ravel()]+1

def trace_paint(rgb: np.ndarray, settings: BuildSettings, artwork_id: str):
    im=Image.fromarray(cv2.bilateralFilter(rgb,5,22,2))
    quant=im.quantize(colors=settings.paint_colors,method=Image.Quantize.MEDIANCUT,dither=Image.Dither.NONE).filter(ImageFilter.ModeFilter(5))
    ids=np.asarray(quant,dtype=np.uint8)
    table=np.array(quant.getpalette(),dtype=np.uint8).reshape(-1,3)
    grouped=defaultdict(list); n_shapes=0
    for geom,val in shapes(ids,connectivity=4):
        p=shape(geom); n_shapes+=1
        # Paint only: polygons can be small. They are not counted as player taps.
        rings=rings_of(p)
        hx='#'+''.join(f'{int(v):02X}' for v in table[int(val)])
        grouped[hx].append(path_of(rings))
    paint_paths=[{'fill':hx,'d':' '.join(ds)} for hx,ds in sorted(grouped.items())]
    gray=cv2.cvtColor(rgb,cv2.COLOR_RGB2GRAY)
    dark=(gray<settings.ink_threshold).astype(np.uint8)
    ink=[]
    if dark.any():
        parts=[]
        for geom,_ in shapes(dark,mask=dark.astype(bool),connectivity=4):
            poly=shape(geom)
            if poly.area >= 2: parts.append(path_of(rings_of(poly)))
        if parts: ink=[{'fill':INK,'d':' '.join(parts)}]
    h,w=rgb.shape[:2]
    return {'schemaVersion':1,'artworkId':artwork_id,'viewBox':[0,0,w,h],
        'paths':paint_paths,'inkPaths':ink,'sourceColorShapeCount':n_shapes}, table[ids]

def svg_open(w,h):
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}">'

def svg_paint(paint):
    return '<g fill-rule="evenodd">'+''.join(f'<path fill="{p["fill"]}" d="{p["d"]}"/>' for p in paint['paths'])+'</g>'

def svg_ink(paint):
    return '<g fill-rule="evenodd">'+''.join(f'<path fill="{p["fill"]}" d="{p["d"]}"/>' for p in paint['inkPaths'])+'</g>'

def validate_bundle(bundle: dict, roundtrip=True) -> dict:
    m,g,p,paint=bundle['manifest'],bundle['geometry'],bundle['palette'],bundle['paint']
    errors=[]; warnings=[]; regs=g['regions']+g.get('decorations',[])
    w,h=map(int,g['viewBox'][2:]); ids=set(); pids={a['id'] for a in p}
    polys=[]; outside_labels=[]
    if m['id']!=g['artworkId'] or m['id']!=paint['artworkId']: errors.append('Artwork identity mismatch.')
    if m['version']!=g['artworkVersion']: errors.append('Artwork version mismatch.')
    if len(pids)!=len(p): errors.append('Duplicate palette IDs.')
    if m['regionCount']!=len(g['regions']): errors.append('Incorrect region count.')
    for r in regs:
        if r['id'] in ids: errors.append('Duplicate region ID.')
        ids.add(r['id']); poly=region_polygon(r); polys.append(poly)
        if r['paletteId'] not in pids: errors.append('Unknown palette group.')
        if not poly.is_valid or poly.area<=0: errors.append('Invalid polygon: '+r['id'])
        if not box(0,0,w,h).covers(poly): errors.append('Out of bounds: '+r['id'])
        if abs(poly.area-r['area'])>.01: errors.append('Area mismatch: '+r['id'])
        if path_of(r['rings']) != r['d']: errors.append('Path/rings mismatch: '+r['id'])
        pt=Point(r['label']['x'],r['label']['y'])
        if not poly.contains(pt): outside_labels.append(r['id'])
        if not r['d'].endswith(' Z'): errors.append('Open path: '+r['id'])
    if outside_labels: errors.append('Labels outside region interiors: '+','.join(outside_labels[:5]))
    # Pixel centers and area checks are supplemented by a real polygon union.
    combined=unary_union(polys)
    total_area=sum(poly.area for poly in polys)
    overlap=max(0,total_area-combined.area)
    missing=max(0,w*h-combined.area)
    if missing>.01 or overlap>.01: errors.append('Region partition has missing or overlapping area.')
    empty_pixels=None
    if roundtrip:
        back=rasterize(((poly,1) for poly in polys),out_shape=(h,w),fill=0,dtype=np.uint8)
        empty_pixels=int((back==0).sum())
        if empty_pixels: errors.append('Raster roundtrip left uncovered pixels.')
    small=[r['id'] for r in g['regions'] if 2*r['label']['clearance'] * 360/w * 8 < 24]
    if small: warnings.append(f'{len(small)} regions have less than a 24px inscribed target at 8x zoom on a 360px-wide canvas; inspect or merge them.')
    if paint.get('sourceColorShapeCount',0)>15000: warnings.append('Detailed vector painting is heavy. Cache/rasterize its static layer in the game and test real devices.')
    warnings.append('Automatic regions are drafts, not guaranteed to follow semantic object boundaries. Human visual review is required.')
    fixed=sum(r['area'] for r in g.get('decorations',[]))
    return {'passed':not errors,'errors':errors,'warnings':warnings,'playableRegions':len(g['regions']),
            'fixedRegions':len(g.get('decorations',[])),'paletteGroups':len(p),'paintPaths':len(paint['paths']),
            'paintSubshapes':paint.get('sourceColorShapeCount'), 'area':total_area,
            'canvasArea':w*h,'overlapArea':overlap,'missingArea':missing,
            'roundtripEmptyPixels':empty_pixels,'smallTargetCount':len(small),
            'precoloredAreaPercent':round(100*fixed/(w*h),3),'humanReviewed':False,
            'checkScope':'IDs, palette references, versions, closed paths, polygon validity, bounds, label interiors, partition union, overlap, raster coverage. Not semantic/artistic quality.'}

def emit_bundle(folder: Path, bundle: dict, previews=True) -> dict:
    folder.mkdir(parents=True,exist_ok=True)
    m,g,p,paint=bundle['manifest'],bundle['geometry'],bundle['palette'],bundle['paint']
    m['regionCount']=len(g['regions']); m['paletteCount']=len(p)
    w,h=map(int,g['viewBox'][2:]); vb=svg_open(w,h)
    write_json(folder/'regions.json',g); write_json(folder/'palette.json',p); write_json(folder/'paint.json',paint)
    m['contentHash']=hashlib.sha256(b''.join((folder/f).read_bytes() for f in ['regions.json','palette.json','paint.json'])).hexdigest()
    final=vb+svg_paint(paint)+svg_ink(paint)+'</svg>'
    (folder/'colored.svg').write_text(final)
    outlines='<g fill="none" fill-rule="evenodd" stroke="'+INK+'" stroke-width="0.65" stroke-linejoin="round">'+''.join(f'<path d="{r["d"]}"/>' for r in g['regions'])+'</g>'
    (folder/'linework.svg').write_text(vb+outlines+svg_ink(paint)+'</svg>')
    (folder/'ink.svg').write_text(vb+svg_ink(paint)+'</svg>')
    def numbered(selected=False):
        select=g['regions'][0]['paletteId'] if g['regions'] else 1
        defs='<defs><pattern id="sel" width="8" height="8" patternUnits="userSpaceOnUse"><rect width="8" height="8" fill="#F0F3F6"/><path d="M0 0H4V4H0Z M4 4H8V8H4Z" fill="#CBD5DD"/></pattern></defs>'
        masks='<g stroke="'+INK+'" stroke-width=".65" fill-rule="evenodd">'+''.join(f'<path d="{r["d"]}" fill="'+('url(#sel)' if selected and r['paletteId']==select else 'white')+'"/>' for r in g['regions'])+'</g>'
        labels='<g font-family="sans-serif" text-anchor="middle" dominant-baseline="central" fill="'+INK+'">'+''.join(f'<text x="{r["label"]["x"]}" y="{r["label"]["y"]}" font-size="{r["label"]["fontSize"]}">{r["paletteId"]}</text>' for r in g['regions'])+'</g>'
        return vb+defs+svg_paint(paint)+masks+svg_ink(paint)+labels+'</svg>'
    (folder/'numbered.svg').write_text(numbered())
    (folder/'selected-preview.svg').write_text(numbered(True))
    qa=validate_bundle(bundle)
    if not qa['passed']: raise ValueError('Asset validation failed: '+'; '.join(qa['errors'][:5]))
    m['qa']={'status':'draft-needs-human-review','passedGeometryChecks':True,'humanReviewed':False}
    write_json(folder/'validation.json',qa)
    if previews:
        import cairosvg
        from io import BytesIO
        raw=cairosvg.svg2png(bytestring=final.encode(),output_width=640,output_height=round(640*h/w))
        img=Image.open(BytesIO(raw)).convert('RGB'); img.save(folder/'thumbnail.webp',quality=88)
        img.save(folder/'colored-preview.png')
        cairosvg.svg2png(bytestring=numbered().encode(),write_to=str(folder/'numbered-preview.png'),output_width=640,output_height=round(640*h/w))
    m['checksums']={f:checksum(folder/f) for f in ['regions.json','palette.json','paint.json','colored.svg','numbered.svg','linework.svg','ink.svg']}
    write_json(folder/'artwork.json',m)
    return qa

def compile_image(source: Path, output: Path, *, artwork_id: str, version: str, title: str,
                  settings: BuildSettings, provenance: dict|None=None, progress: Callable = lambda *_:None) -> dict:
    progress(.04,'Preparing approved master')
    im=Image.open(source).convert('RGB'); original=im.size
    im.thumbnail((settings.max_edge,settings.max_edge),Image.Resampling.LANCZOS)
    rgb=np.array(im); h,w=rgb.shape[:2]
    progress(.12,'Finding image-aware draft regions')
    labels=slic(rgb,n_segments=settings.target_regions,compactness=settings.compactness,
                sigma=.8,start_label=1,enforce_connectivity=True,min_size_factor=.25,channel_axis=-1)
    labels=merge_tiny(labels,rgb,settings.min_region_pixels)
    progress(.28,'Building closed, non-overlapping region polygons')
    polys=[]; means=[]; masks=[]; origins=[]
    for geom,_ in shapes(labels.astype(np.int32),connectivity=4):
        poly=shape(geom)
        for poly in polygon_parts(make_valid(poly)):
            x0,y0,x1,y1=map(int,poly.bounds)
            local=rasterize([(poly,1)],out_shape=(y1-y0,x1-x0),transform=__import__('affine').Affine.translation(x0,y0),dtype=np.uint8).astype(bool)
            if not local.any(): continue
            polys.append(poly); means.append(rgb[y0:y1,x0:x1][local].mean(0)); masks.append(local);origins.append((x0,y0))
    progress(.38,'Grouping palette colors and positioning labels')
    palette,assignments=palette_for_regions(np.array(means),settings.palette_colors)
    regions=[]; decorations=[]
    for i,(poly,pid,mask,origin) in enumerate(zip(polys,assignments,masks,origins),1):
        label=make_label(poly,int(pid),mask,origin)
        reg=pack_region(poly,f'r-{i:05d}',int(pid),label=label)
        if label['clearance']<settings.min_label_radius or label['fontSize']<3.5:
            decorations.append(reg)
        else: regions.append(reg)
    if not regions: raise ValueError('No playable regions. Lower the label radius or region count.')
    progress(.48,'Tracing the detailed vector paint layer')
    paint,_=trace_paint(rgb,settings,artwork_id)
    geometry={'schemaVersion':1,'artworkId':artwork_id,'artworkVersion':version,
        'viewBox':[0,0,w,h],'fillRule':'evenodd','stroke':INK,'strokeWidth':.65,
        'regions':regions,'decorations':decorations,'detailPaths':[]}
    manifest={'schemaVersion':1,'format':SCHEMA,'id':artwork_id,'version':version,'title':title,
        'description':'Image-aware vector-region draft. Review boundaries and targets before publication.',
        'category':'Studio','viewBox':[0,0,w,h], 'difficulty':'unrated','difficultyValidatedByPlaytest':False,
        'regionCount':len(regions),'paletteCount':len(palette),'objectGroups':[],
        'assets':{'regions':'regions.json','palette':'palette.json','paint':'paint.json','coloredSvg':'colored.svg',
            'numberedSvg':'numbered.svg','lineworkSvg':'linework.svg','inkSvg':'ink.svg','thumbnail':'thumbnail.webp','sourceMaster':'source-master.png'},
        'rendering':{'model':'vector-underpainting-with-region-masks','fillRule':'evenodd','decorationsArePrecolored':True,'labelMinScreenPx':9,'zoomRecommended':8},
        'generation':{'settings':settings.model_dump(),'sourcePixels':list(original),'workingPixels':[w,h],
            'algorithm':'SLIC + adjacent-small-region merge + exact pixel-edge polygonization; bilateral-smoothed median-cut vector underpainting with 5px mode cleanup'},
        'provenance':provenance or {'source':'User-supplied image; rights not independently verified'}}
    bundle={'manifest':manifest,'geometry':geometry,'palette':palette,'paint':paint}
    output.mkdir(parents=True,exist_ok=True)
    shutil.copy2(source,output/'source-master.png')
    progress(.70,'Checking topology, labels and runtime exports')
    qa=emit_bundle(output,bundle)
    write_json(output/'build-settings.json',settings.model_dump())
    progress(1,'Draft bundle ready for review')
    return {'manifest':manifest,'validation':qa}

def load_bundle(folder):
    return {'manifest':read_json(folder/'artwork.json'),'geometry':read_json(folder/'regions.json'),
        'palette':read_json(folder/'palette.json'),'paint':read_json(folder/'paint.json')}

def edit_bundle(source: Path, output: Path, request, version: str):
    bundle=load_bundle(source); g=bundle['geometry']; m=bundle['manifest']
    regs={r['id']:r for r in g['regions']}; chosen=[regs.get(rid) for rid in dict.fromkeys(request.region_ids)]
    if any(r is None for r in chosen): raise ValueError('Select existing playable regions from the current revision.')
    valid_palette={p['id'] for p in bundle['palette']}
    pid=request.palette_id or chosen[0]['paletteId']
    if pid not in valid_palette: raise ValueError('Unknown palette group.')
    if request.action=='merge':
        if len(chosen)<2: raise ValueError('Select two or more adjacent regions.')
        union=unary_union([region_polygon(r) for r in chosen])
        if union.geom_type!='Polygon' or not union.is_valid: raise ValueError('Merge only edge-adjacent regions; disconnected pieces cannot be one tap target.')
        used=set(request.region_ids)
        g['regions']=[r for r in g['regions'] if r['id'] not in used]
        merged_id='r-m-'+hashlib.sha256(('|'.join(sorted(used))+version).encode()).hexdigest()[:12]
        g['regions'].append(pack_region(union,merged_id,pid,chosen[0]['objectId']))
    elif request.action=='group':
        for r in chosen: r['objectId']=request.group
    elif request.action=='palette':
        for r in chosen:
            r['paletteId']=pid
            r['label']=make_label(region_polygon(r),pid)
    elif request.action=='label':
        if len(chosen)!=1 or request.x is None or request.y is None: raise ValueError('Select one region and a label position.')
        poly=region_polygon(chosen[0]); point=Point(request.x,request.y)
        if not poly.contains(point): raise ValueError('Label anchor must be strictly inside the region.')
        radius=poly.boundary.distance(point); digits=len(str(chosen[0]['paletteId']))
        size=min(22.,radius*1.6/math.sqrt((digits*.65)**2+1))
        if size<3.5: raise ValueError('Too close to the edge to fit the number. Choose a wider interior area.')
        chosen[0]['label']={'x':request.x,'y':request.y,'fontSize':round(size,3),'clearance':round(radius,3),'minScreenPx':9}
    elif request.action=='decorate':
        if len(chosen)>=len(g['regions']): raise ValueError('Keep at least one playable region.')
        remove=set(request.region_ids)
        g['regions']=[r for r in g['regions'] if r['id'] not in remove]
        g['decorations'].extend(chosen)
    groups=defaultdict(list)
    for r in g['regions']:
        if r['objectId']!='unassigned': groups[r['objectId']].append(r['id'])
    m['objectGroups']=[{'id':key,'title':key.replace('-',' ').title(),'regionIds':ids} for key,ids in sorted(groups.items())]
    m['version']=version; g['artworkVersion']=version
    m.pop('review',None)
    m['provenance']['lastEdit']=request.action
    output.mkdir(parents=True,exist_ok=True)
    for f in ['source-master.png','build-settings.json']:
        if (source/f).is_file(): shutil.copy2(source/f,output/f)
    qa=emit_bundle(output,bundle)
    return {'manifest':m,'validation':qa}

def make_export(folder: Path, include_authoring=False):
    m=read_json(folder/'artwork.json')
    names=['artwork.json','regions.json','palette.json','paint.json','colored.svg','numbered.svg',
           'linework.svg','ink.svg','selected-preview.svg','thumbnail.webp','validation.json']
    # sourceMaster is optional for game rendering, but keep paths honest in the runtime package.
    if include_authoring: names+=['source-master.png','build-settings.json','colored-preview.png','numbered-preview.png']
    from io import BytesIO
    output=BytesIO()
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as z:
        root=f'artworks/{m["id"]}/'
        export_m=json.loads(json.dumps(m))
        if not include_authoring: export_m['assets'].pop('sourceMaster',None)
        for name in names:
            if name=='artwork.json': z.writestr(root+name,json.dumps(export_m,indent=2))
            elif (folder/name).is_file(): z.write(folder/name,root+name)
        z.writestr('catalog-entry.json',json.dumps({'id':m['id'],'title':m['title'],'manifest':root+'artwork.json','format':m['format'],'status':m['qa']['status']},indent=2))
        z.writestr('IMPORT.md','Load artwork.json and its regions/palette/paint files with the detailed-vector adapter. This is a draft until reviewed. Preserve viewBox, even-odd holes, contentHash and version. Do not stretch a full painting into each individual region. Do not use numbered.svg as hit-test metadata. Validation does not establish copyright clearance.\n')
    return output.getvalue()
