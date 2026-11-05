"""Make browser teardown idempotent when stop and batch cleanup race."""
import threading
_setup_lock=threading.Lock()

def install():
    import tiktok_reply as tt
    cls=tt.TikTokLikeBot
    if getattr(cls,'_v62_safe_cleanup',False):return
    previous=cls.close
    previous_start=cls.start_browser
    def close(self):
        self._v62_close_requested=True
        with _setup_lock:
            lock=getattr(self,'_v62_cleanup_lock',None)
            if lock is None:
                lock=self._v62_cleanup_lock=threading.RLock()
        with lock:
            if getattr(self,'_v62_cleanup_done',False):return
            if getattr(self,'_v62_cleanup_in_progress',False):return
            self._v62_cleanup_in_progress=True
            try:
                result=previous(self)
                self._v62_cleanup_done=True
                return result
            finally:
                self._v62_cleanup_in_progress=False
    def start(self,*args,**kwargs):
        self._v62_cleanup_done=False
        self._v62_close_requested=False
        try:
            return previous_start(self,*args,**kwargs)
        finally:
            # Stop can precede assignment of the browser driver.
            self._v62_cleanup_done=False
            if getattr(self,'_v62_close_requested',False):
                close(self)
    cls.close=close
    cls.start_browser=start
    cls._v62_safe_cleanup=True


