"""Limit live browser sessions, including sessions retained between accounts."""
import threading

class Budget:
    def __init__(self, limit, stop_event):
        self.pool = threading.BoundedSemaphore(max(1, limit))
        # Scouts and pool share the same physical browser budget.
        self.scout = self.pool
        self.stop = stop_event
        self.bots = set()
        self.lock = threading.RLock()
    def close_all(self):
        with self.lock:
            bots = list(self.bots)
        for bot in bots:
            bot.close()

def limited_factory(factory, budget, scout=False):
    slots = budget.scout if scout else budget.pool
    class LimitedBot(factory):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._delivery_slot = False
            self._delivery_lock = threading.RLock()
            with budget.lock:
                budget.bots.add(self)
        def start_browser(self, *args, **kwargs):
            with self._delivery_lock:
                # A relogin/retry must close the previous session before opening another.
                if self._delivery_slot or getattr(self, 'driver', None) is not None:
                    self.close()
                while not slots.acquire(timeout=0.2):
                    if budget.stop.is_set():
                        raise RuntimeError('Đã dừng trước khi mở Chrome.')
                self._delivery_slot = True
                try:
                    if budget.stop.is_set():
                        raise RuntimeError('Đã dừng trước khi mở Chrome.')
                    return super().start_browser(*args, **kwargs)
                except BaseException:
                    self.close()
                    raise
        def close(self):
            with self._delivery_lock:
                try:
                    return super().close()
                finally:
                    if self._delivery_slot:
                        self._delivery_slot = False
                        slots.release()
    return LimitedBot

def install():
    from client.app.runner import Runner
    import tiktok_reply as tt
    if getattr(Runner, '_delivery_browser_limit_v55', False):
        return
    tt.recommend_thread_count = lambda requested, total: (min(max(1, int(requested)), total), '')
    original_run = Runner.run
    original_one = Runner._run_one
    def run(self, accounts, proxies, opts, *args, **kwargs):
        self._close_reused_bots()
        budget = Budget(1 if opts.debug else max(1, int(opts.threads)), self.stop_event)
        self._delivery_budget = budget
        try:
            return original_run(self, accounts, proxies, opts, *args, **kwargs)
        finally:
            budget.close_all()
            self._delivery_budget = None
    def run_one(self, *args, **kwargs):
        budget = getattr(self, '_delivery_budget', None)
        if budget is None:
            return original_one(self, *args, **kwargs)
        values = list(args)
        values[5] = limited_factory(values[5], budget, bool(kwargs.get('scan_only') or kwargs.get('scout_mode')))
        return original_one(self, *values, **kwargs)
    Runner.run = run
    Runner._run_one = run_one
    Runner._delivery_browser_limit_v55 = True
