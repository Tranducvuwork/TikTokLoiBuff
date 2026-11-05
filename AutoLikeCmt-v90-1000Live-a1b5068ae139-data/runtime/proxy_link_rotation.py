from proxy_wait_timeout import bounded_wait, wait as proxy_wait
import json
from types import SimpleNamespace
from proxy_cooldown_60 import Rotation

def _replace_opts(opts, **overrides):
    d = dict(vars(opts))
    d.update(overrides)
    return SimpleNamespace(**d)

def install():
    from client.app.runner import Runner,RunStats
    import machine_proxy_config as cfg
    if getattr(Runner,'_delivery_link_rotation_60',False):return
    previous=Runner.run
    def run(self,accounts,proxies,opts,done_fn=None):
        data=json.loads(cfg.POOL_PATH.read_text(encoding='utf-8-sig'))
        rows=[str((x.get('raw') or x.get('address') or '') if isinstance(x,dict) else x).strip() for x in data.get('proxies',[])]
        rows=list(dict.fromkeys(x for x in rows if x))
        count=cfg.get_config()['count']
        if count<1:raise ValueError(f'Nhập số proxy từ 1 đến {len(rows)}.')
        if count>len(rows):count=len(rows)
        rotation=Rotation(rows,count,history=cfg.POOL_PATH.parent/'proxy-rest-60.json')
        previous_limit=getattr(self,'_run_proxy_limit',None)
        self._run_proxy_limit=count
        acquire,release=self._proxy_acquire,self._proxy_release
        aggregate=RunStats(target=opts.target_count)
        targets=list(opts.target_texts or [])
        links=[str(x).strip() for x in opts.links if str(x).strip()]
        self.stop_event.clear()
        @bounded_wait(self._log)
        def current_group(desired=1,stop_event=None):
            stop=stop_event or self.stop_event
            announced=False
            while not stop.is_set():
                import tiktok_reply as tt
                excluded=[]
                for raw in rows:
                    parsed=tt.parse_proxy(raw)
                    if not parsed or tt._proxy_blocked(parsed):excluded.append(raw)
                group=rotation.take(excluded)
                if group is not None:
                    import tiktok_reply as tt
                    usable = []
                    for raw in group:
                        parsed = tt.parse_proxy(raw)
                        if parsed and not tt._proxy_blocked(parsed):
                            usable.append(raw)
                    if usable:
                        return group
                    # The scheduler would reject this group and reacquire it
                    # forever because normal release deliberately keeps it.
                    rotation.release()
                    self._log('[proxy] Nhóm hiện tại không có proxy khả dụng; trả nhóm và chọn nhóm khác trong pool.')
                    continue
                if not announced:
                    self._log('[proxy] Chờ proxy lỗi phục hồi; proxy tốt không nghỉ bắt buộc.')
                    announced=True
                if proxy_wait(stop, 1):break
            return []
        old_rotate=getattr(self,'_proxy_rotate200',None)
        def rotate200():
            rotation.release()
            group=current_group()
            self._log(f'[proxy] Đổi sang nhóm {len(group)} proxy, giữ cache CID; acc đang chạy không bị ngắt.')
            return group
        self._proxy_rotate200=rotate200
        self._proxy_acquire=current_group
        # Keep the same group through scout and every account batch of this link.
        self._proxy_release=lambda *args,**kwargs:None
        try:
            for index,link in enumerate(links):
                if self.stop_event.is_set() or aggregate.reached_target():break
                group=current_group()
                if not group:break
                self._log(f'[proxy] Link {index+1}/{len(links)} dùng {len(group)} proxy theo số nhập; tự xoay sau mỗi 60 giây.')
                target=targets[min(index,len(targets)-1)] if targets else opts.target_text
                remaining=max(0,opts.target_count-aggregate.ok) if opts.target_count else 0
                single=_replace_opts(opts,links=[link],target_text=target,target_texts=[target],target_count=remaining)
                try:
                    stats=previous(self,accounts,rows,single,done_fn=None)
                    aggregate.ok+=stats.ok
                    aggregate.done+=stats.done
                    aggregate.acc_ok+=stats.acc_ok
                    aggregate.stopped_at_target=aggregate.stopped_at_target or stats.stopped_at_target
                    aggregate.per_link.update(stats.per_link)
                finally:
                    rotation.release()
                    self._log('[proxy] Đã trả nhóm; proxy tốt dùng tiếp ngay, chỉ proxy lỗi mới tạm nghỉ.')
                self.stats=aggregate
            return aggregate
        finally:
            rotation.release()
            self._proxy_acquire,self._proxy_release=acquire,release
            self._proxy_rotate200=old_rotate
            if previous_limit is None:
                del self._run_proxy_limit
            else:
                self._run_proxy_limit=previous_limit
            self.stats=aggregate
            if done_fn:done_fn()
    Runner.run=run
    Runner._delivery_link_rotation_60=True
