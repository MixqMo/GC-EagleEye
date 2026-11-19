#!/usr/bin/env python3
"""Ingesta OCDS GuateCompras: JSON record package y JSONL (también gzip).

--check-only examina registros sin necesitar PostgreSQL ni psycopg.
--dsn habilita la carga con snapshots completos e idempotencia.
Fuente: esquema construido a partir de cuatro reportes agregados; requiere validación real.
"""
from __future__ import annotations
import argparse
import contextlib
import datetime as dt
import gzip
import hashlib
import json
from pathlib import Path
import re
from decimal import Decimal, InvalidOperation
from collections import Counter
try:
    import simplejson as precise_json
except ImportError:
    precise_json = None


UTC = dt.timezone.utc

def canonical(obj):
    return (precise_json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(',', ':'), use_decimal=True)
            if precise_json else json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str))

def checksum(obj):
    return hashlib.sha256(canonical(obj).encode('utf-8')).hexdigest()

def file_checksum(path):
    h = hashlib.sha256()
    with open(path,'rb') as f:
        for block in iter(lambda:f.read(1024*1024), b''):h.update(block)
    return h.hexdigest()

def open_text(path):
    return gzip.open(path, 'rt', encoding='utf-8') if str(path).endswith('.gz') else open(path,'r',encoding='utf-8')

def infer_format(path):
    name = str(path).lower()
    if name.endswith('.jsonl.gz'): return 'jsonl.gz'
    if name.endswith('.json.gz'): return 'json.gz'
    if name.endswith('.jsonl') or name.endswith('.ndjson'):return 'jsonl'
    if name.endswith('.json'):return 'json'
    raise ValueError('Extensión no soportada: json, jsonl, json.gz, jsonl.gz')

def iter_records(path):
    fmt = infer_format(path)
    if fmt.startswith('jsonl'):
        with open_text(path) as src:
            for line_no, line in enumerate(src,1):
                if not line.strip():continue
                payload = json.loads(line)
                if isinstance(payload,dict) and isinstance(payload.get('records'),list):
                    yield from payload['records']
                elif isinstance(payload,list):yield from payload
                else:yield payload
        return
    try:
        import ijson
    except ImportError:
        # Ruta de respaldo SOLO para fixtures pequeños; un mes entero requiere ijson.
        if path.stat().st_size > 8_000_000:
            raise RuntimeError('Para JSON grande instale ijson (requirements.txt)')
        with open_text(path) as src:
            obj = json.load(src)
        if isinstance(obj, dict) and isinstance(obj.get('records'), list): yield from obj['records']
        elif isinstance(obj, list): yield from obj
        else: yield obj
        return
    # Los mensuales observados son paquetes: {uri, version, extensions, records:[...]}.
    with open_text(path) as f:
        header = f.read(65536)
    with open_text(path) as src:
        if re.search(r'"records"\s*:', header):
            yield from ijson.items(src,'records.item')
        elif header.lstrip().startswith('['):
            yield from ijson.items(src,'item')
        else:
            yield from ijson.items(src,'')

def time_or_none(value, issues, ocid, path):
    if value in (None, ''):return None
    try:
        if not isinstance(value,str):raise ValueError('fecha no es texto')
        t = dt.datetime.fromisoformat(value.replace('Z','+00:00'))
        if t.tzinfo is None:raise ValueError('fecha sin zona horaria')
        if not 1900<=t.year<=2200:raise ValueError('año fuera del rango práctico')
        return t
    except (ValueError, TypeError) as e:
        issues.append(dict(ocid=ocid,severity='warning',code='invalid_datetime',path=path,detail=str(e),value=value))
        return None

def money_or_none(value, issues, ocid, path):
    if value is None:return None
    try:
        d=Decimal(str(value))
        if not d.is_finite():raise InvalidOperation('decimal no finito')
        return d
    except (InvalidOperation, ValueError,TypeError) as e:
        issues.append(dict(ocid=ocid,severity='warning',code='invalid_decimal',path=path,detail=str(e),value=value))
        return None

def flat_money(obj, issues, ocid, path):
    val=obj.get('value') or {}
    if not isinstance(val,dict):val={}
    return money_or_none(val.get('amount'),issues,ocid,path+'.value.amount'),val.get('currency')

def issue(issues, ocid, code, path, detail='', value=None, severity='warning'):
    issues.append(dict(ocid=ocid,severity=severity,code=code,path=path,detail=detail,value=value))

def array(obj, key):
    v=obj.get(key) if isinstance(obj,dict) else None
    return v if isinstance(v,list) else []

def valid_source_id(obj):
    return obj.get('id') if isinstance(obj,dict) and isinstance(obj.get('id'),str) and obj.get('id') else None

def investigate(record):
    """Verificaciones de estructura/referencias posibles sin asumir que el dato ausente equivale a cero."""
    problems=[]
    if not isinstance(record,dict):
        issue(problems,None,'bad_record','$','Se esperaba objeto',record,'error');return problems
    ocid=record.get('ocid')
    compiled=record.get('compiledRelease')
    if not ocid or not isinstance(ocid,str):issue(problems,ocid,'missing_ocid','$.ocid','OCID obligatorio','', 'error')
    if not isinstance(compiled,dict):
        issue(problems,ocid,'missing_compiled','$.compiledRelease','Versión compilada ausente','', 'error');return problems
    if compiled.get('ocid') != ocid:issue(problems,ocid,'ocid_mismatch','$.compiledRelease.ocid', 'OCID difiere entre contenedor y compiledRelease',compiled.get('ocid'))
    entities={}
    for name,path in [('bids','$.compiledRelease.bids.details'),('awards','$.compiledRelease.awards'),('contracts','$.compiledRelease.contracts'),('items','$.compiledRelease.tender.items')]:
        if name=='bids':values=array(compiled.get('bids') or {},'details')
        elif name=='items':values=array(compiled.get('tender') or {},'items')
        else:values=array(compiled,name)
        names=[]
        for v in values:
            if not isinstance(v,dict):issue(problems,ocid,'bad_child',path,'Se esperaba objeto',v);continue
            k=valid_source_id(v)
            if k is None:issue(problems,ocid,'missing_child_id',path+'[].id','Identificador ausente')
            else:names.append(k)
        repeated=[k for k,n in Counter(names).items() if n>1]
        for k in repeated:issue(problems,ocid,'duplicate_child_id',path,'Identificador repetido dentro de un OCID',k)
        entities[name]=set(names)
    for contract in array(compiled,'contracts'):
        if not isinstance(contract,dict):continue
        ref=contract.get('awardID')
        if ref and ref not in entities.get('awards',set()):issue(problems,ocid,'unresolved_award', '$.compiledRelease.contracts[].awardID', 'Referencia a adjudicación no hallada en compiledRelease.awards',ref)
        start=time_or_none((contract.get('period') or {}).get('startDate'),problems,ocid,'$.compiledRelease.contracts[].period.startDate')
        end=time_or_none((contract.get('period') or {}).get('endDate'),problems,ocid,'$.compiledRelease.contracts[].period.endDate')
        time_or_none(contract.get('dateSigned'),problems,ocid,'$.compiledRelease.contracts[].dateSigned')
        if start and end and end<start:issue(problems,ocid,'invalid_period','$.compiledRelease.contracts[].period','Fin anterior a inicio')
    tender=compiled.get('tender') or {}
    start=time_or_none((tender.get('tenderPeriod') or {}).get('startDate'),problems,ocid,'$.compiledRelease.tender.tenderPeriod.startDate')
    end=time_or_none((tender.get('tenderPeriod') or {}).get('endDate'),problems,ocid,'$.compiledRelease.tender.tenderPeriod.endDate')
    if start and end and end<start:issue(problems,ocid,'invalid_period','$.compiledRelease.tender.tenderPeriod','Fin anterior a inicio')
    if tender.get('numberOfTenderers') is not None and len(array(compiled.get('bids') or {},'details')):
        declared=tender.get('numberOfTenderers')
        actual=len({x.get('id') for b in array(compiled.get('bids') or {},'details') for x in array(b,'tenderers') if isinstance(x,dict) and x.get('id')})
        if isinstance(declared,int) and declared != actual:
            issue(problems,ocid,'tenderer_count_mismatch','$.compiledRelease.tender.numberOfTenderers',f'Declarados={declared}; distintos en ofertas={actual}',declared,'info')
    for p in array(compiled,'parties'):
        if not isinstance(p,dict):continue
        c=array(p,'additionalContactPoints')
        canonical_contacts=[canonical(x) for x in c]
        if len(set(canonical_contacts)) <len(c):issue(problems,ocid,'duplicate_contacts','$.compiledRelease.parties[].additionalContactPoints','Puntos de contacto duplicados')
    for field, entries in [('tenderers',array(tender,'tenderers'))]:
        ids=[x.get('id') for x in entries if isinstance(x,dict)]
        if len(ids)>len(set(ids)):issue(problems,ocid,'duplicate_tenderer','$.compiledRelease.tender.tenderers','Oferentes repetidos')
    return problems


def org_details(p):
    det=p.get('details') or {}
    if not isinstance(det,dict):det={}
    def v(name,key):return (det.get(name) or {}).get(key) if isinstance(det.get(name),dict) else None
    addr=p.get('address') or {}
    ident=p.get('identifier') or {}
    return (p.get('id'),p.get('name'), ident.get('scheme'),ident.get('id'),
            addr.get('countryName'),addr.get('region'),addr.get('locality'),addr.get('streetAddress'),
            v('type','id'),v('type','description'),v('entityType','id'),v('entityType','description'),
            v('level','id'),v('level','description'),v('legalEntityTypeDetail','id'),v('legalEntityTypeDetail','description'))

ORG_COLUMNS=('org_key','name','id_scheme','id_value','address_country','address_region','address_locality','address_street',
    'organization_type_id','organization_type_desc','organization_entity_type_id','organization_entity_type_desc',
    'organization_level_id','organization_level_desc','legal_entity_type_id','legal_entity_type_desc')

def upsert_org(cur,p):
    key=valid_source_id(p)
    if not key:return None
    vals=org_details(p)
    cols=','.join(ORG_COLUMNS)
    placeholders=','.join(['%s']*len(ORG_COLUMNS))
    updates=','.join(f'{x}=COALESCE(EXCLUDED.{x},gci.organizations.{x})' for x in ORG_COLUMNS[1:])
    cur.execute(f'INSERT INTO gci.organizations ({cols}) VALUES ({placeholders}) ON CONFLICT (org_key) DO UPDATE SET {updates},last_seen_at=now()',vals)
    ident=p.get('identifier') or {}
    if isinstance(ident,dict) and ident.get('scheme') and ident.get('id'):
        cur.execute('INSERT INTO gci.organization_identifiers (org_key,scheme,identifier) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING',(key,ident['scheme'],str(ident['id'])))
    return key

def ensure_org(cur,ref):
    if not isinstance(ref,dict):return None
    return upsert_org(cur,ref)

def add_contacts(cur,p):
    org=valid_source_id(p)
    if not org:return
    contacts=[]
    if isinstance(p.get('contactPoint'),dict):contacts.append(('primary',p['contactPoint']))
    contacts.extend(('additional',v) for v in array(p,'additionalContactPoints') if isinstance(v,dict))
    for kind,c in contacts:
        cur.execute('''INSERT INTO gci.organization_contacts (org_key,contact_kind,contact_hash,contact_name,email,telephone,fax_number)
                   VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
                    (org,kind,checksum(c),c.get('name'),c.get('email'),c.get('telephone'),c.get('faxNumber')))

def insert_role(cur,pid,key,role):
    if key and role:
        cur.execute('INSERT INTO gci.process_organization_roles (process_id,org_key,role_code) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING',(pid,key,role))

def convert_ts(v,issues,ocid,path):return time_or_none(v,issues,ocid,path)

def insert_current(cur,pid,record,issues):
    ocid=record['ocid']; c=record['compiledRelease']; t=c.get('tender') or {}
    parties=array(c,'parties')
    for p in parties:
        if not isinstance(p,dict):continue
        upsert_org(cur,p); add_contacts(cur,p)
        for m in array(p,'memberOf'):
            if not isinstance(m,dict) or not m.get('id'):continue
            parent=ensure_org(cur,m)
            if parent and p.get('id') and parent!=p['id']:
                cur.execute('INSERT INTO gci.organization_memberships (child_org_key,parent_org_key) VALUES (%s,%s) ON CONFLICT DO NOTHING',(p['id'],parent))
    buyer=ensure_org(cur,c.get('buyer'))
    procurer=ensure_org(cur,t.get('procuringEntity'))
    period=t.get('tenderPeriod') or {}
    vals=(t.get('id'),t.get('title'),t.get('status'),t.get('statusDetails'),t.get('procurementMethod'),
          t.get('procurementMethodDetails'),t.get('mainProcurementCategory'),t.get('numberOfTenderers'),
          convert_ts(t.get('datePublished'),issues,ocid,'$.compiledRelease.tender.datePublished'),
          convert_ts(period.get('startDate'),issues,ocid,'$.compiledRelease.tender.tenderPeriod.startDate'),
          convert_ts(period.get('endDate'),issues,ocid,'$.compiledRelease.tender.tenderPeriod.endDate'),
          buyer,procurer,c.get('language'),c.get('initiationType'),(c.get('dataSegmentation') or {}).get('id'),pid)
    cur.execute('''UPDATE gci.procurement_processes SET
        tender_source_id=%s,title=%s,status=%s,status_details=%s,procurement_method=%s,procurement_method_details=%s,
        main_procurement_category=%s,number_of_tenderers=%s,tender_published_at=%s,tender_start_at=%s,tender_end_at=%s,
        buyer_org_key=%s,procuring_org_key=%s,language=%s,initiation_type=%s,segmentation_id=%s,updated_at=now()
        WHERE process_id=%s''',vals)
    # Eliminar solo proyección relacional actual. process_snapshots permanece inmutable.
    for table in ('documents','contracts','awards','bids','tender_items','process_categories','release_references','process_organization_roles'):
        cur.execute(f'DELETE FROM gci.{table} WHERE process_id=%s',(pid,))
    insert_role(cur,pid,buyer,'buyer')
    insert_role(cur,pid,procurer,'procuringEntity')
    for p in parties:
        if not isinstance(p,dict):continue
        for r in array(p,'roles'):insert_role(cur,pid,p.get('id'),r)
    for k,kind in [('additionalProcurementCategories','additional_procurement'),('submissionMethod','submission_method')]:
        for v in array(t,k):
            if isinstance(v,str):cur.execute('INSERT INTO gci.process_categories VALUES (%s,%s,%s) ON CONFLICT DO NOTHING',(pid,kind,v))
    for b in array(c.get('bids') or {},'details'):
        if not isinstance(b,dict) or not valid_source_id(b):continue
        amount,currency=flat_money(b,issues,ocid,'$.compiledRelease.bids.details[]')
        cur.execute('''INSERT INTO gci.bids (process_id,bid_source_id,bid_date,status,amount,currency)
            VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (process_id,bid_source_id) DO UPDATE SET
            bid_date=EXCLUDED.bid_date,status=EXCLUDED.status,amount=EXCLUDED.amount,currency=EXCLUDED.currency
            RETURNING bid_pk''',(pid,b['id'],convert_ts(b.get('date'),issues,ocid,'$.compiledRelease.bids.details[].date'),b.get('status'),amount,currency))
        bid_pk=cur.fetchone()[0]
        for tenderer in array(b,'tenderers'):
            key=ensure_org(cur,tenderer)
            if key:
                cur.execute('INSERT INTO gci.bid_tenderers VALUES (%s,%s) ON CONFLICT DO NOTHING',(bid_pk,key))
                insert_role(cur,pid,key,'tenderer')
    for a in array(c,'awards'):
        if not isinstance(a,dict) or not valid_source_id(a):continue
        amount,currency=flat_money(a,issues,ocid,'$.compiledRelease.awards[]')
        cur.execute('''INSERT INTO gci.awards (process_id,award_source_id,award_date,title,status,status_details,amount,currency)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (process_id,award_source_id) DO UPDATE SET
            award_date=EXCLUDED.award_date,title=EXCLUDED.title,status=EXCLUDED.status,status_details=EXCLUDED.status_details,
            amount=EXCLUDED.amount,currency=EXCLUDED.currency RETURNING award_pk''',
            (pid,a['id'],convert_ts(a.get('date'),issues,ocid,'$.compiledRelease.awards[].date'),a.get('title'),a.get('status'),a.get('statusDetails'),amount,currency))
        award_pk=cur.fetchone()[0]
        for supplier in array(a,'suppliers'):
            key=ensure_org(cur,supplier)
            if key:
                cur.execute('INSERT INTO gci.award_suppliers VALUES (%s,%s) ON CONFLICT DO NOTHING',(award_pk,key))
                insert_role(cur,pid,key,'supplier')
    for tenderer in array(t,'tenderers'):
        insert_role(cur,pid,ensure_org(cur,tenderer),'tenderer')
    for con in array(c,'contracts'):
        if not isinstance(con,dict) or not valid_source_id(con):continue
        amount,currency=flat_money(con,issues,ocid,'$.compiledRelease.contracts[]')
        ref=con.get('awardID'); ap=None
        if ref:
            cur.execute('SELECT award_pk FROM gci.awards WHERE process_id=%s AND award_source_id=%s',(pid,ref))
            found=cur.fetchone();ap=found[0] if found else None
        dur=con.get('period') or {}
        cur.execute('''INSERT INTO gci.contracts
            (process_id,contract_source_id,contract_number,award_source_id,award_pk,signed_at,period_start_at,
             period_end_at,title,status,status_details,amount,currency)
             VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (process_id,contract_source_id)
             DO UPDATE SET title=EXCLUDED.title,award_source_id=EXCLUDED.award_source_id RETURNING contract_pk''',
             (pid,con['id'],con.get('contractNumber'),ref,ap,
              convert_ts(con.get('dateSigned'),issues,ocid,'$.compiledRelease.contracts[].dateSigned'),
              convert_ts(dur.get('startDate'),issues,ocid,'$.compiledRelease.contracts[].period.startDate'),
              convert_ts(dur.get('endDate'),issues,ocid,'$.compiledRelease.contracts[].period.endDate'),
              con.get('title'),con.get('status'),con.get('statusDetails'),amount,currency))
        contract_pk=cur.fetchone()[0]
        put_docs(cur,pid,ocid,'contract',con['id'],contract_pk,array(con,'documents'),issues)
    for item in array(t,'items'):
        if not isinstance(item,dict) or not valid_source_id(item):continue
        q=money_or_none(item.get('quantity'),issues,ocid,'$.compiledRelease.tender.items[].quantity')
        cl=item.get('classification') or {}; unit=item.get('unit') or {}
        cur.execute('''INSERT INTO gci.tender_items
           (process_id,item_source_id,description,quantity,unit_name,classification_scheme,classification_id)
           VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (process_id,item_source_id)
           DO UPDATE SET description=EXCLUDED.description,quantity=EXCLUDED.quantity RETURNING item_pk''',
           (pid,item['id'],item.get('description'),q,unit.get('name'),cl.get('scheme'),cl.get('id')))
        item_pk=cur.fetchone()[0]
        for a in array(item,'attributes'):
            if isinstance(a,dict) and valid_source_id(a):
                cur.execute('''INSERT INTO gci.tender_item_attributes (item_pk,attribute_source_id,name,value)
                VALUES (%s,%s,%s,%s) ON CONFLICT (item_pk,attribute_source_id) DO UPDATE SET
                name=EXCLUDED.name,value=EXCLUDED.value''',(item_pk,a['id'],a.get('name'),a.get('value')))
    put_docs(cur,pid,ocid,'tender',t.get('id') or ocid,None,array(t,'documents'),issues)
    for rel in array(record,'releases'):
        if not isinstance(rel,dict) or not rel.get('url'):continue
        tags=[x for x in array(rel,'tag') if isinstance(x,str)]
        cur.execute('''INSERT INTO gci.release_references (process_id,release_url,release_date,tags)
            VALUES (%s,%s,%s,%s) ON CONFLICT (process_id,release_url) DO UPDATE
            SET release_date=EXCLUDED.release_date,tags=EXCLUDED.tags''',
            (pid,rel['url'],convert_ts(rel.get('date'),issues,ocid,'$.releases[].date'),tags))


def put_docs(cur,pid,ocid,scope,owner,contract_pk,docs,issues):
    for d in docs:
        if not isinstance(d,dict) or not valid_source_id(d):continue
        cur.execute('''INSERT INTO gci.documents
          (process_id,scope,owner_source_id,contract_pk,document_source_id,title,document_type,document_type_details,published_at,mime_type,language,url)
          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (process_id,scope,owner_source_id,document_source_id)
          DO UPDATE SET title=EXCLUDED.title,url=EXCLUDED.url,published_at=EXCLUDED.published_at''',
          (pid,scope,owner,contract_pk,d['id'],d.get('title'),d.get('documentType'),d.get('documentTypeDetails'),
           convert_ts(d.get('datePublished'),issues,ocid,'$.compiledRelease.'+('tender' if scope=='tender' else 'contracts[]')+'.documents[].datePublished'),
           d.get('format'),d.get('language'),d.get('url')))


def log_issues(cur,run_id,snapshot_id,problems):
    from psycopg.types.json import Jsonb
    for p in problems:
        cur.execute('''INSERT INTO gci.data_quality_issues
          (run_id,snapshot_id,ocid,severity,issue_code,json_path,detail,source_value)
          VALUES (%s,%s,%s,%s,%s,%s,%s,%s)''',
          (run_id,snapshot_id,p.get('ocid'),p['severity'],p['code'],p.get('path'),p.get('detail'),Jsonb(p.get('value'))))


def import_record(conn,record,run_id,ordinal,detected):
    from psycopg.types.json import Jsonb
    problems=investigate(record)
    if any(x['severity']=='error' for x in problems):
        with conn.transaction():
            with conn.cursor() as cur:log_issues(cur,run_id,None,problems)
        return False,False,problems
    ocid=record['ocid']; compiled=record['compiledRelease']
    published=time_or_none(compiled.get('publishedDate'),problems,ocid,'$.compiledRelease.publishedDate')
    compiled_date=time_or_none(compiled.get('date'),problems,ocid,'$.compiledRelease.date')
    effective=published or compiled_date
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute('INSERT INTO gci.procurement_processes (ocid) VALUES (%s) ON CONFLICT(ocid) DO NOTHING',(ocid,))
            cur.execute('SELECT process_id,current_published_at,current_snapshot_id FROM gci.procurement_processes WHERE ocid=%s FOR UPDATE',(ocid,))
            pid,previous,prior_snapshot=cur.fetchone()
            source_hash=checksum(record)
            cur.execute('''INSERT INTO gci.process_snapshots
                (process_id,record_hash,compiled_release_id,compiled_release_date,published_at,raw_record)
                VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(process_id,record_hash) DO NOTHING
                RETURNING snapshot_id''',
                (pid,source_hash,compiled.get('id'),compiled_date,published,Jsonb(record)))
            r=cur.fetchone();is_new=bool(r)
            if r:sid=r[0]
            else:
                cur.execute('SELECT snapshot_id FROM gci.process_snapshots WHERE process_id=%s AND record_hash=%s',(pid,source_hash))
                sid=cur.fetchone()[0]
            cur.execute('INSERT INTO gci.snapshot_ingestions (snapshot_id,run_id,record_ordinal) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING',(sid,run_id,ordinal))
            promote = prior_snapshot is None or (effective is not None and (previous is None or effective>previous))
            if (prior_snapshot and effective == previous and sid !=prior_snapshot):
                issue(problems,ocid,'same_timestamp_conflict','$.compiledRelease.publishedDate','Dos snapshots diferentes con igual marca temporal')
            if promote:
                insert_current(cur,pid,record,problems)
                cur.execute('UPDATE gci.procurement_processes SET current_published_at=%s,current_snapshot_id=%s WHERE process_id=%s',(effective,sid,pid))
            log_issues(cur,run_id,sid,problems)
    return is_new,promote,problems


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('file',type=Path,help='Ruta a mensual .json, anual .jsonl, ambos pueden ser .gz')
    g=ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--dsn',help='Cadena de conexión postgres, por ejemplo: postgresql:///gci')
    g.add_argument('--check-only',action='store_true',help='Solo verifica JSON/JSONL; no escribe en DB')
    ap.add_argument('--limit',type=int,default=0,help='Máximo de registros (0=sin límite)')
    ap.add_argument('--source-uri',help='URL de fuente original (por defecto ruta del archivo)')
    ap.add_argument('--period',help='AAAA-MM para el archivo mensual')
    ap.add_argument('--issues',type=Path,help='Escribir incidencias JSONL (sin datos personales salvo los valores problemáticos)')
    args=ap.parse_args()
    fmt=infer_format(args.file)
    run_id=None;seen=Counter();issue_counts=Counter();last_errors=[]
    if args.dsn:
        try:import psycopg
        except ImportError as e:raise SystemExit('Instale psycopg[binary] desde requirements.txt') from e
        if precise_json is None:raise SystemExit('Instale simplejson para serializar Decimal como número JSON sin convertirlo a float')
        from psycopg.types.json import set_json_dumps
        conn=psycopg.connect(args.dsn,autocommit=False)
        set_json_dumps(lambda obj: precise_json.dumps(obj,use_decimal=True,ensure_ascii=False), conn)
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute('''INSERT INTO gci.ingest_runs (source_uri,source_path,source_period,source_format,source_sha256)
                    VALUES (%s,%s,%s,%s,%s) RETURNING run_id''',
                    (args.source_uri or args.file.resolve().as_uri(),str(args.file.resolve()),args.period,fmt,file_checksum(args.file)))
                run_id=cur.fetchone()[0]
    else:conn=None
    try:
        log_file=args.issues.open('w',encoding='utf-8') if args.issues else None
        with contextlib.ExitStack() as stack:
            if log_file:stack.enter_context(log_file)
            for ix,record in enumerate(iter_records(args.file),1):
                if args.limit and ix>args.limit:break
                seen['read']+=1
                if conn:
                    try:
                        fresh,promoted,problems=import_record(conn,record,run_id,ix,seen)
                        seen['snapshot_new']+=int(fresh);seen['current_updates']+=int(promoted)
                        seen['invalid']+=int(any(p['severity']=='error' for p in problems))
                    except Exception as e:
                        # Guardar incidencia de ejecución sin perder los registros ya comprometidos.
                        conn.rollback()
                        seen['invalid']+=1
                        problems=[dict(ocid=record.get('ocid') if isinstance(record,dict) else None,
                                severity='error',code='db_import_failed',path='$',detail=str(e),value=None)]
                        with conn.transaction():
                            with conn.cursor() as cur:log_issues(cur,run_id,None,problems)
                else:problems=investigate(record);seen['invalid']+=int(any(p['severity']=='error' for p in problems))
                for x in problems:
                    issue_counts[x['code']]+=1
                    if len(last_errors)<12:last_errors.append({k:v for k,v in x.items() if k!='value'})
                    if log_file:log_file.write(canonical(x)+'\n')
                if conn and ix%1000==0:print(f'Procesados {ix:,}...',flush=True)
    finally:
        if conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute('''UPDATE gci.ingest_runs SET state='finished',finished_at=now(),records_read=%s,
                      snapshots_inserted=%s,current_updates=%s,invalid_records=%s WHERE run_id=%s''',
                      (seen['read'],seen['snapshot_new'],seen['current_updates'],seen['invalid'],run_id))
            conn.close()
    print(json.dumps(dict(processed=seen['read'],valid=seen['read']-seen['invalid'],new_snapshots=seen['snapshot_new'],
         promoted=seen['current_updates'],issue_counts=dict(issue_counts),first_issues=last_errors,run_id=run_id),ensure_ascii=False,indent=2))

if __name__=='__main__':main()
