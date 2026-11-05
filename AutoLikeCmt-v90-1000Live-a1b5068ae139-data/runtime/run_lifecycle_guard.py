"""Cancel the current run without restarting the pool window."""
import socket
import sys
import threading
import time


def _runners(bridge):
    return [value for value in vars(bridge).values()
            if isinstance(getattr(value, 'stop_event', None), threading.Event)]


def _owned_threads_and_sockets(runners, worker, excluded):
    """Inspect only stacks which belong to this run, never other Chrome/apps."""
    events = [runner.stop_event for runner in runners]
    threads = {thread.ident: thread for thread in threading.enumerate()}
    owned, sockets = set(), set()
    for ident, top in sys._current_frames().items():
        if ident in excluded:
            continue
        frames, frame = [], top
        belongs = worker is not None and ident == worker.ident
        while frame is not None:
            frames.append(frame)
            if frame.f_code.co_filename != __file__:
                owner = frame.f_locals.get('self')
                if any(owner is runner for runner in runners):
                    belongs = True
                event = getattr(owner, 'stop_event', None)
                if any(event is item for item in events):
                    belongs = True
                if any(frame.f_locals.get('stop_event') is item for item in events):
                    belongs = True
            frame = frame.f_back
        if not belongs:
            continue
        if ident in threads:
            owned.add(threads[ident])
        for frame in frames:
            for value in tuple(frame.f_locals.values()):
                if isinstance(value, socket.socket):
                    sockets.add(value)
                elif isinstance(value, socket.SocketIO):
                    sock = getattr(value, '_sock', None)
                    if isinstance(sock, socket.socket):
                        sockets.add(sock)
    return owned, sockets


def _interrupt(sockets):
    for sock in sockets:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            # Windows recv may stay blocked after shutdown alone. Close the
            # Python socket object too, invalidating the handle for its owner.
            sock.close()
            if sock.fileno() != -1:
                # makefile()/SocketIO can defer close while holding IO refs.
                sock._real_close()
        except OSError:
            pass


def install():
    from client.app.bridge import Bridge
    if getattr(Bridge, '_run_lifecycle_guard', False):
        return
    start, report, stop = Bridge.start_run, Bridge._run_and_report, Bridge.stop_run
    progress = Bridge.get_progress
    lock = threading.RLock()
    states = {}

    def state_for(bridge):
        with lock:
            return states.setdefault(bridge, dict(token=None, worker=None,
                starting=False, pending=False, error=None, threads=set()))

    def log(bridge, message):
        try:
            bridge._log(message)
        except Exception:
            pass

    def guarded_start(self, *args, **kwargs):
        state = state_for(self)
        with lock:
            if state['pending']:
                return {'ok': False, 'error': state['error'] or
                        'Đang ép dừng lượt cũ; chờ thu hồi xong rồi bắt đầu bài mới.'}
            if state['token'] is not None:
                return {'ok': False, 'error':
                        'Lượt cũ chưa kết thúc. Bấm Dừng lần nữa để ép dừng trước khi chạy bài mới.'}
            token = object()
            state.update(token=token, starting=True, worker=None, error=None)
        try:
            result = start(self, *args, **kwargs)
        except BaseException:
            with lock:
                state['starting'] = False
                if state['worker'] is None:
                    state['token'] = None
            raise
        with lock:
            state['starting'] = False
            if not isinstance(result, dict) or not result.get('ok'):
                if state['worker'] is None:
                    state['token'] = None
        return result

    def guarded_report(self, *args, **kwargs):
        state = state_for(self)
        with lock:
            token = state['token']
            state['worker'] = threading.current_thread()
        try:
            return report(self, *args, **kwargs)
        finally:
            with lock:
                if state['token'] is token and not state['pending']:
                    state.update(token=None, worker=None)

    def guarded_stop(self, force=False, *args, **kwargs):
        if not force:
            return stop(self, False, *args, **kwargs)
        state = state_for(self)
        with lock:
            if state['pending']:
                return {'ok': not bool(state['error']), 'forced': True,
                        'cleanup_pending': True, 'error': state['error']}
            if state['starting']:
                return {'ok': False, 'error': 'Đang khởi tạo lượt; bấm ép dừng lại sau giây lát.'}
            state.update(pending=True, error=None, threads=set())
            runners = _runners(self)
            worker = state['worker']
        if worker is not None and worker.is_alive() and not runners:
            with lock:
                state.update(pending=False)
            return {'ok': False, 'error': 'Chưa xác định được worker của lượt cũ; giữ nguyên pool.'}
        for runner in runners:
            runner.stop_event.set()
            pause = getattr(runner, 'pause_event', None)
            if pause is not None:
                pause.set()
        cleanup_error = []

        def finish():
            try:
                result = stop(self, True, *args, **kwargs)
                if isinstance(result, dict) and result.get('ok') is False:
                    cleanup_error.append(result.get('error') or 'Lỗi thu hồi lượt cũ.')
            except Exception as error:
                cleanup_error.append('Không thu hồi được lượt cũ: ' + type(error).__name__)

        cleanup = threading.Thread(target=finish, daemon=True, name='force-stop-cleanup')

        def wait_cleanup():
            announced = time.monotonic()
            while True:
                excluded = {threading.get_ident(), cleanup.ident}
                active, sockets = _owned_threads_and_sockets(runners, worker, excluded)
                with lock:
                    state['threads'].update(active)
                    remaining = any(thread.is_alive() for thread in state['threads'])
                for runner in runners:
                    runner.stop_event.set()
                _interrupt(sockets)
                if not cleanup.is_alive() and not remaining:
                    break
                if time.monotonic() - announced >= 15:
                    log(self, '[dừng] Đang chờ tác vụ cũ thoát; cửa sổ pool vẫn giữ mở.')
                    announced = time.monotonic()
                time.sleep(.1)
            if cleanup_error:
                with lock:
                    state['error'] = cleanup_error[0]
                log(self, '[dừng] ' + cleanup_error[0])
                return
            try:
                import local_account_results
                local_account_results.flush()
            except Exception as error:
                log(self, '[dừng] Chưa lưu được kết quả: ' + type(error).__name__)
            with lock:
                state.update(token=None, worker=None, pending=False, error=None, threads=set())
            log(self, '[dừng] Đã thu hồi lượt cũ. Có thể nhập bài mới và bấm Bắt đầu.')

        # Discover sockets before cleanup starts closing Chrome and waiting on locks.
        active, sockets = _owned_threads_and_sockets(runners, worker, {threading.get_ident()})
        with lock:
            state['threads'].update(active)
        _interrupt(sockets)
        cleanup.start()
        threading.Thread(target=wait_cleanup, daemon=True, name='force-stop-monitor').start()
        return {'ok': True, 'forced': True, 'cleanup_pending': True}

    def current_progress(self, *args, **kwargs):
        result = dict(progress(self, *args, **kwargs))
        state = state_for(self)
        with lock:
            result['cleanup_pending'] = state['pending']
            result['cleanup_error'] = state['error']
            if state['pending']:
                result['running'] = True
            elif state['token'] is None:
                result['running'] = False
        return result

    Bridge.start_run = guarded_start
    Bridge._run_and_report = guarded_report
    Bridge.stop_run = guarded_stop
    Bridge.recover_stuck_run = lambda self: guarded_stop(self, True)
    Bridge.get_progress = current_progress
    Bridge._run_lifecycle_guard = True
