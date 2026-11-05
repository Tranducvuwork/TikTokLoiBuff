import threading
import time

class ProxyEpoch:
    def __init__(self, rows, rotate, every=200, clock=time.monotonic):
        self.rows = list(rows)
        self.rotate = rotate
        self.every = every
        self.done = 0
        self.lock = threading.RLock()
        self.clock = clock
        self.next_rotation = self.clock() + 60

    def snapshot(self):
        with self.lock:
            import tiktok_reply as tt
            failed = any(tt._proxy_blocked(tt.parse_proxy(raw)) for raw in self.rows)
            if self.rotate and (failed or self.clock() >= self.next_rotation):
                rows = self.rotate()
                # An empty group means stop/unavailable: never reuse resting rows.
                self.rows = list(rows or [])
                self.next_rotation = self.clock() + 60
            return list(self.rows)

    def complete(self):
        with self.lock:
            self.done += 1
        return None
