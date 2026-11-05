from pathlib import Path
import json,os,shutil,hashlib
def install():
    root=Path.home()/'.auto-like-cmt';root.mkdir(parents=True,exist_ok=True)
    seed=json.loads(Path(__file__).with_name('bundled-pool.json').read_text(encoding='utf-8'))
    path=root/'local_pool_data.json'
    data=json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else seed
    def raw(row):return str(row.get('raw') or row.get('address') or '') if isinstance(row,dict) else str(row)
    known={raw(row).strip() for row in data.get('proxies',[])}
    try: deleted=set(json.loads((root/'deleted-proxy-keys.json').read_text(encoding='utf-8')))
    except (OSError,ValueError): deleted=set()
    additions=[dict(row) for row in seed.get('proxies',[]) if raw(row).strip() not in known and hashlib.sha256(raw(row).strip().encode('utf-8')).hexdigest() not in deleted]
    next_added_id=max([int(row.get('id',0)) for row in data.get('proxies',[]) if isinstance(row,dict)] or [0])+1
    for row in additions:
        row['id']=next_added_id
        next_added_id+=1
    data['proxies']=data.get('proxies',[])+additions
    converted=False
    ids={str(row.get('id')) for row in data['proxies'] if isinstance(row,dict)}
    next_id=max([int(value) for value in ids if value.isdigit()] or [0])+1
    normalized=[]
    for row in data['proxies']:
        if isinstance(row,dict):
            normalized.append(row)
        else:
            converted=True
            normalized.append({'id':next_id,'address':row,'raw':row,'proxy_type':'http','status':'unknown','state':'unknown','note':'','test_status':'unknown'})
            next_id+=1
    data['proxies']=normalized
    acc_changed=False
    existing_accs=list(data.get('accounts',[]))
    seed_accs=[dict(row) for row in seed.get('accounts',[]) if isinstance(row,dict) and str(row.get('id','')).isdigit()]
    seed_ids={int(row['id']) for row in seed_accs}
    max_seed_id=max(seed_ids) if seed_ids else 11456
    pruned_accs=[a for a in existing_accs if isinstance(a,dict) and str(a.get('id','')).isdigit() and (int(a['id']) in seed_ids or int(a['id'])>max_seed_id)]
    if len(pruned_accs)!=len(existing_accs):
        existing_accs=pruned_accs
        data['accounts']=existing_accs
        acc_changed=True
    by_id={int(a['id']):a for a in existing_accs if isinstance(a,dict) and str(a.get('id','')).isdigit()}
    new_hot,new_mid=[],[]
    for row in seed_accs:
        aid=int(row['id'])
        if aid in by_id:
            acc=by_id[aid]
            for k in ('password','mail_pass','totp_secret','login_user','username','cookie'):
                if row.get(k) and not acc.get(k):
                    acc[k]=row[k];acc_changed=True
            if aid>=9313 and row.get('username') and acc.get('username')!=row.get('username'):
                acc['username']=row['username'];acc_changed=True
            if row.get('email') and '***' not in str(row.get('email')) and (not acc.get('email') or '***' in str(acc.get('email'))):
                acc['email']=row['email'];acc_changed=True
        else:
            acc_changed=True
            if aid>=11365:new_hot.append(dict(row))
            else:new_mid.append(dict(row))
    if new_hot or new_mid:
        data['accounts']=new_hot+new_mid+existing_accs
    if additions or converted or acc_changed or not path.exists():
        for name in ['local_pool_data.json','pool.json']:
            p=root/name; backup=root/(name+'.before-v61-proxy-package.bak')
            if p.exists() and not backup.exists():shutil.copy2(p,backup)
            temp=root/(name+'.proxy-package.tmp');temp.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8');os.replace(temp,p)
