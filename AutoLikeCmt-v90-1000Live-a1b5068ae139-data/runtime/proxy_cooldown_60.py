import json,threading,time,os

class Rotation:
    def __init__(self, rows, count, clock=time.time, history=None):
        self.rows=list(dict.fromkeys(rows))
        self.count=count
        self.clock=clock
        self.history=history
        self.used={}
        self.active=[]
        self.lock=threading.RLock()
        if history and history.exists():
            self.used=json.loads(history.read_text(encoding='utf-8'))
    def take(self, excluded=()):
        with self.lock:
            now=self.clock()
            blocked=set(excluded)
            ready=[r for r in self.rows if r not in blocked]
            self.active=[r for r in self.active if r in ready]
            if not ready:return None
            ready.sort(key=lambda r:self.used.get(r,0))
            self.active += [r for r in ready if r not in self.active][:max(0,self.count-len(self.active))]
            return list(self.active)
    def release(self):
        with self.lock:
            for row in self.active:self.used[row]=self.clock()
            self.active=[]
            if self.history:
                temp=self.history.with_suffix('.tmp')
                temp.write_text(json.dumps(self.used),encoding='utf-8')
                os.replace(temp,self.history)

def install():
    from client.app.runner import Runner
    import machine_proxy_config as cfg
    if getattr(Runner,'_delivery_rotation_60',False):return
    previous=Runner.run
    def run(self, accounts, proxies, opts, *args, **kwargs):
        data=json.loads(cfg.POOL_PATH.read_text(encoding='utf-8-sig'))
        rows=[str((x.get('raw') or x.get('address') or '') if isinstance(x,dict) else x).strip() for x in data.get('proxies',[])]
        rows=list(dict.fromkeys(x for x in rows if x))
        count=cfg.get_config()['count']
        if count>len(rows):raise ValueError(f'Chỉ có {len(rows)} proxy; hãy giảm số proxy cho máy này.')
        rotation=Rotation(rows,count,history=cfg.POOL_PATH.parent/'proxy-rest-60.json')
        acquire,release=self._proxy_acquire,self._proxy_release
        def next_group(desired=1,stop_event=None):
            stop=stop_event or self.stop_event
            announced=False
            while not stop.is_set():
                group=rotation.take()
                if group is not None:
                    self._log(f'[proxy] Nhận {len(group)} proxy theo số nhập; nhóm đã dùng nghỉ đủ 60 giây.')
                    return group
                if not announced:
                    self._log('[proxy] Chờ nhóm proxy nghỉ đủ 60 giây; không mở dư Chrome.')
                    announced=True
                stop.wait(1)
            return []
        self._proxy_acquire=next_group
        self._proxy_release=lambda *values,**options:rotation.release()
        try:return previous(self,accounts,rows,opts,*args,**kwargs)
        finally:
            rotation.release()
            self._proxy_acquire,self._proxy_release=acquire,release
    Runner.run=run
    Runner._delivery_rotation_60=True
