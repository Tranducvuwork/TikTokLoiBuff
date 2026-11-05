"""Wait for recovered proxies until the user explicitly stops the run."""
from contextvars import ContextVar
from functools import wraps
from time import monotonic

_deadline = ContextVar('proxy_wait_deadline', default=None)

def bounded_wait(log):
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            owner = _deadline.get() is None
            token = _deadline.set(float('inf')) if owner else None
            try:
                return fn(*args, **kwargs)
            except TimeoutError:
                if owner:
                    log('[proxy] Hết 120 giây chờ proxy khả dụng. Đã kết thúc lượt; kiểm tra proxy trước khi bắt đầu lại.')
                raise
            finally:
                if token is not None:
                    _deadline.reset(token)
        return wrapped
    return decorate

def wait(stop, seconds):
    if stop.is_set():
        return True
    deadline = _deadline.get()
    remaining = deadline - monotonic() if deadline is not None else seconds
    if remaining <= 0:
        stop.set()
        raise TimeoutError('Hết 120 giây chờ proxy khả dụng.')
    stopped = stop.wait(min(seconds, remaining))
    if not stopped and deadline is not None and monotonic() >= deadline:
        stop.set()
        raise TimeoutError('Hết 120 giây chờ proxy khả dụng.')
    return stopped
