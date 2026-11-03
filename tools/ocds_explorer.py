#!/usr/bin/env python3
"""OCDS record profiler. Streaming, standard library only.
Supports JSON arrays, wrapped arrays, JSONL and comma-separated JSON objects.
Fails rather than creating misleading empty reports.
"""
import argparse, collections, csv, json, os, pathlib, re, sys, time
CHUNK=1<<20
PUNCT=re.compile(rb'[{}"\\]')

def detect(path):
    with open(path,'rb') as f: head=f.read(65536).lstrip(b'\xef\xbb\xbf \n\t\r')
    if not head: raise ValueError('Archivo vacío')
    if head[:1]==b'[': return 0,'array'
    if head[:1]!=b'{': raise ValueError('Formato de inicio no reconocido: se esperaba { o [')
    # Pick the first key at root, without assuming the entire file is a JSON document.
    m=re.match(rb'\{\s*"((?:[^"\\]|\\.)*)"\s*:',head,re.S)
    if not m: raise ValueError('No se pudo leer la primera clave de la raíz JSON')
    key=json.loads(b'"'+m.group(1)+b'"')
    if key in ('ocid','compiledRelease','releases'): return 0,'objetos OCDS independientes'
    return 1,f'contenedor (primera clave: {key})'

def iter_records(path, expected_parent_depth, interval=2, limit=0):
    size=os.path.getsize(path); n=0; depth=0; collecting=False; in_string=False; escaped=False;slash_at=-10
    pieces=[]; start=0; read=0; begun=last=time.monotonic(); rejected=0
    with open(path,'rb') as f:
        while True:
            block=f.read(CHUNK)
            if not block:break
            seg_start=0 if collecting else None
            for m in PUNCT.finditer(block):
                pos=m.start(); b=block[pos]
                if in_string:
                    absolute=read+pos
                    if escaped and absolute != slash_at+1: escaped=False
                    if escaped: escaped=False
                    elif b==92: escaped=True; slash_at=absolute
                    elif b==34: in_string=False
                    continue
                if b==34:
                    in_string=True
                elif b==123:
                    if depth==expected_parent_depth and not collecting:
                        collecting=True; seg_start=pos; pieces=[]
                    depth+=1
                elif b==125:
                    depth-=1
                    if depth<0: raise ValueError('Llave de cierre inesperada')
                    if collecting and depth==expected_parent_depth:
                        pieces.append(block[seg_start:pos+1]); raw=b''.join(pieces)
                        try: item=json.loads(raw)
                        except (ValueError,UnicodeDecodeError) as e:
                            raise ValueError(f'JSON inválido en registro {n+1} (byte {read+pos}): {e}') from e
                        if isinstance(item,dict) and ('ocid' in item or isinstance(item.get('compiledRelease'),dict)):
                            n+=1;yield item,read+pos+1,size
                            if limit and n>=limit:return
                        else: rejected+=1
                        collecting=False;pieces=[];seg_start=None
            if collecting:pieces.append(block[seg_start:])
            read+=len(block)
            now=time.monotonic()
            if now-last>=interval:
                print(f'Leído {read/size*100:.1f}% | {n} procedimientos | {read/1048576:.1f}/{size/1048576:.1f} MiB | {now-begun:.0f} s',file=sys.stderr,flush=True);last=now
    if collecting: print('AVISO: último registro truncado',file=sys.stderr)
    print(f'Lectura finalizada | {n} procedimientos | {time.monotonic()-begun:.1f} s',file=sys.stderr,flush=True)
    if not n: raise ValueError(f'No se encontraron procedimientos OCDS; {rejected} objetos descartados. No se generaron reportes.')

def typename(x):
    if x is None:return 'null'
    if isinstance(x,bool):return 'boolean'
    if isinstance(x,int):return 'integer'
    if isinstance(x,float):return 'number'
    if isinstance(x,list):return 'array'
    if isinstance(x,dict):return 'object'
    return 'string'

def ex(x):
    if isinstance(x,dict):return '{'+str(len(x))+' claves}'
    if isinstance(x,list):return '['+str(len(x))+' elementos]'
    return json.dumps(x,ensure_ascii=False)[:140]

def profile(path,mode,depth,limit,interval):
    stats=collections.defaultdict(lambda:{'types':collections.Counter(),'records':0,'occurrences':0,'examples':[],'lengths':collections.Counter(),'values':collections.Counter()})
    count=0; ids=[]
    def walk(v,path,seen,level):
        if level>80:return
        s=stats[path];s['types'][typename(v)]+=1;s['occurrences']+=1
        if path not in seen:s['records']+=1;seen.add(path)
        if len(s['examples'])<3:
            z=ex(v)
            if z not in s['examples']:s['examples'].append(z)
        if isinstance(v,(list,tuple)):
            s['lengths'][len(v)]+=1
            for elem in v:walk(elem,path+'[]',seen,level+1)
        elif isinstance(v,dict):
            for k,elem in v.items():walk(elem,path+'.'+k,seen,level+1)
        else:
            z=ex(v)
            if z in s['values'] or len(s['values'])<25:s['values'][z]+=1
    for obj,_,_ in iter_records(path,depth,interval,limit):
        count+=1;walk(obj,'$',set(),0)
        if len(ids)<8:ids.append(obj.get('ocid',obj.get('compiledRelease',{}).get('ocid','?')))
    paths=[]
    for p,s in sorted(stats.items()):
        paths.append({'path':p,'types':dict(s['types']),'records':s['records'],'presence_pct':round(100*s['records']/count,2),'occurrences':s['occurrences'],'examples':s['examples'],'array_lengths':dict(s['lengths'].most_common(12)),'top_values':s['values'].most_common(6)})
    if not any(x['path']=='$.compiledRelease.tender.id' for x in paths):
        raise ValueError('Los registros no incluyen compiledRelease.tender.id. Revise si la estructura del archivo es la esperada; no se generaron reportes.')
    return {'source':str(path),'input_mode':mode,'file_bytes':os.path.getsize(path),'records_analyzed':count,'paths':paths,'sample_ids':ids}
HTML='''<!doctype html><html lang="es"><meta charset="utf-8"><title>OCDS Explorer</title><style>body{font:14px system-ui;margin:24px;background:#111827;color:#f4f4f4}input{padding:12px;width:90%;color:inherit;background:#263244;border:1px solid #536071}table{border-collapse:collapse;width:100%}td,th{padding:8px;border-bottom:1px solid #394354;text-align:left;vertical-align:top}code{color:#8ed3fd}pre{white-space:pre-wrap}summary{cursor:pointer}</style><h1>OCDS Explorer</h1><p id="summary"></p><input id="q" placeholder="Filtrar rutas JSON"><table><thead><tr><th>Ruta</th><th>Tipos</th><th>Presencia</th><th>Apariciones</th><th>Detalles</th></tr></thead><tbody id="tbody"></tbody></table><script id="payload" type="application/json">__PAYLOAD__</script><script>const d=JSON.parse(document.getElementById('payload').textContent);document.getElementById('summary').textContent=d.records_analyzed+' procedimientos · '+d.paths.length+' rutas · '+d.input_mode;const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));function draw(){let q=document.getElementById('q').value.toLowerCase();document.getElementById('tbody').innerHTML=d.paths.filter(x=>x.path.toLowerCase().includes(q)).map(x=>'<tr><td><code>'+esc(x.path)+'</code></td><td>'+esc(JSON.stringify(x.types))+'</td><td>'+x.presence_pct+'%</td><td>'+x.occurrences+'</td><td><details><summary>Ver datos</summary><pre>'+esc(JSON.stringify({examples:x.examples,array_lengths:x.array_lengths,top_values:x.top_values},null,2))+'</pre></details></td></tr>').join('')}document.getElementById('q').oninput=draw;draw()</script></html>'''

def main():
    a=argparse.ArgumentParser();a.add_argument('file');a.add_argument('--out',default='reporte');a.add_argument('--limit',type=int,default=0);a.add_argument('--progress-every',type=float,default=2);args=a.parse_args()
    try:
        depth,mode=detect(args.file);print(f'Formato detectado: {mode}',flush=True)
        d=profile(args.file,mode,depth,args.limit,args.progress_every)
        target=pathlib.Path(args.out);target.parent.mkdir(parents=True,exist_ok=True)
        target.with_suffix('.json').write_text(json.dumps(d,ensure_ascii=False,indent=2),encoding='utf8')
        with target.with_suffix('.csv').open('w',newline='',encoding='utf8') as f:
            w=csv.writer(f);w.writerow(['ruta','tipos','registros','presencia_%','apariciones','ejemplos','longitudes','valores'])
            for x in d['paths']:w.writerow([x['path'],json.dumps(x['types']),x['records'],x['presence_pct'],x['occurrences'],' | '.join(x['examples']),json.dumps(x['array_lengths']),json.dumps(x['top_values'],ensure_ascii=False)])
        target.with_suffix('.html').write_text(HTML.replace('__PAYLOAD__',json.dumps(d,ensure_ascii=False).replace('<','\\u003c')),encoding='utf8')
        print(f'OK: {d["records_analyzed"]} procedimientos, {len(d["paths"])} rutas JSON. Reportes: {target}.html/.csv/.json')
    except Exception as e:
        print('ERROR:',e,file=sys.stderr);sys.exit(1)
if __name__=='__main__':main()
