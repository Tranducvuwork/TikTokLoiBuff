"""Save check callbacks to the local pool and update the in-memory account list."""
import inspect, json, os, threading, time, uuid, re
from pathlib import Path
ROOT=Path.home()/'.auto-like-cmt'
LOCK=threading.RLock()
PENDING={}
BRIDGES=[]
VALID={'live','die','unknown','banned'}

def rows(value):
    if isinstance(value,dict):
        if 'id' in value:return [value]
        return rows(value.get('results',value.get('accounts',[])))
    if isinstance(value,(list,tuple)):return [r for r in value if isinstance(r,dict)]
    return []

def apply_results(bridge,results):
    updates=[dict(r) for r in rows(results) if r.get('id') is not None and r.get('status') in VALID]
    with LOCK:
        for result in updates:
            id=str(result['id'])
            # Preserve cookie from a successful login if a later status-only report follows.
            previous=PENDING.get(id,{})
            previous.update(result); PENDING[id]=previous
            for owner in ([bridge] if bridge is not None else []) + BRIDGES:
                if owner is None:continue
                for account in getattr(owner,'_pool',{}).get('accounts',[]):
                    if str(account.get('id'))==id:
                        account['status']=result['status']; account.update({k:result[k] for k in ('_check_change_proxy','_check_refresh_cookie') if k in result})
                        if result.get('cookie'):
                            account['cookie']=result['cookie']
                            if result['status']=='live':
                                account['_check_refresh_cookie']=False
                                account['_check_change_proxy']=False
                        break
    return {'ok':True,'updated':len(updates)}

def flush():
    with LOCK:
        if not PENDING:return
        updates=dict(PENDING)
        data=json.loads((ROOT/'local_pool_data.json').read_text(encoding='utf-8-sig'))
        for account in data.get('accounts',[]):
            result=updates.get(str(account.get('id')))
            if not result:continue
            account['status']=result['status']; account.update({k:result[k] for k in ('_check_change_proxy','_check_refresh_cookie') if k in result})
            if result.get('cookie'):
                account['cookie']=result['cookie']
                if result['status']=='live':
                    account['_check_refresh_cookie']=False
                    account['_check_change_proxy']=False
        for name in ('local_pool_data.json','pool.json'):
            temp=ROOT/(name+'.results-'+str(uuid.uuid4())+'.tmp')
            temp.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
            os.replace(temp,ROOT/name)
        for id in updates:PENDING.pop(id,None)

def install():
    import admin_check_accounts as checker
    original=checker.check_all; signature=inspect.signature(original)
    def check_all(*a,**k):
        bound=signature.bind(*a,**k)
        previous=bound.arguments.get('progress_fn')
        previous_log=bound.arguments.get('log_fn')
        accounts=bound.arguments.get('accounts',[])
        by_name={}
        for account in accounts:
            for field in ('username','login_user'):
                name=str(account.get(field) or '').strip().lstrip('@').casefold()
                if name: by_name.setdefault(name,set()).add(str(account['id']))
        failed=set()
        def tracked_log(message):
            match=re.search(r'\[check\]\s+@?([\w.]+)\s+API\s+proxy:',str(message))
            with LOCK:
                if match: failed.update(by_name.get(match[1].casefold(),set()))
                summary=re.search(r'→\s*acc(\d+)\s+LIVE\b',str(message))
            if previous_log: return previous_log(message)
        bound.arguments['log_fn']=tracked_log
        def progress(done,total,result):
            apply_results(None,result)
            if previous:return previous(done,total,result)
        bound.arguments['progress_fn']=progress
        return original(*bound.args,**bound.kwargs)
    checker.check_all=check_all
    from client.app.bridge import Bridge
    original_init=Bridge.__init__
    def initialize(self,*a,**k):
        original_init(self,*a,**k)
        BRIDGES.append(self)
        self.lic.report_account_results=lambda results,*args,**kwargs:apply_results(self,results)
    Bridge.__init__=initialize
    def writer():
        while True:
            time.sleep(.5)
            try:flush()
            except (OSError,ValueError):pass
    threading.Thread(target=writer,daemon=True,name='save-check-results').start()
    import atexit
    atexit.register(flush)
