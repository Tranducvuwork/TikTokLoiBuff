import json, os, threading, time, uuid
from pathlib import Path
ROOT=Path.home()/'.auto-like-cmt'
LOCK=threading.RLock()
PENDING={}
def identity(value): return str(value or '').strip().lstrip('@').casefold()
def flush():
    with LOCK:
        if not PENDING:return
        updates=dict(PENDING)
        data=json.loads((ROOT/'local_pool_data.json').read_text(encoding='utf-8-sig'))
        for account in data.get('accounts',[]):
            update=updates.get((str(account.get('id')),identity(account.get('username'))))
            if update:account['status']=update
        for name in ('local_pool_data.json','pool.json'):
            temp=ROOT/(name+'.autosave-'+str(uuid.uuid4())+'.tmp')
            temp.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
            os.replace(temp,ROOT/name)
        for key,status in updates.items():
            if PENDING.get(key)==status:PENDING.pop(key,None)
def install():
    from client.app.bridge import Bridge
    original=Bridge.__init__
    def initialize(self,*a,**k):
        original(self,*a,**k)
        def snapshot():
            return {(str(row.get('id')),identity(row.get('username'))):row.get('status','unknown')
                    for row in getattr(self,'_pool',{}).get('accounts',[])}
        previous=snapshot()
        def worker():
            nonlocal previous
            while True:
                time.sleep(.75)
                current=snapshot()
                with LOCK:
                    for key,status in current.items():
                        if key in previous and previous[key]!=status and status in ('live','die','unknown','banned'):
                            PENDING[key]=status
                previous=current
                try:flush()
                except (OSError,ValueError):pass
        threading.Thread(target=worker,daemon=True,name='account-status-autosave').start()
    Bridge.__init__=initialize
    import atexit
    atexit.register(flush)
