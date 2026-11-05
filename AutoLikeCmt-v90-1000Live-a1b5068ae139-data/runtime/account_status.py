import threading

def failure_status(reason):
    if reason == 'stopped':
        return None
    return 'unknown'

def install():
    import remember_activation
    remember_activation.install()
    import local_activation_v61
    local_activation_v61.install()
    import bundled_proxy_pool
    bundled_proxy_pool.install()
    import random_account_order
    random_account_order.install()
    import browser_cleanup_v62
    browser_cleanup_v62.install()
    import crash_trace_v61
    crash_trace_v61.install()
    from client.app.runner import Runner
    original_run = Runner.run
    original_relogin = Runner._try_relogin
    original_record = Runner._record_result

    def run(self, *args, **kwargs):
        self._login_status_map = {}
        self._login_status_lock = threading.RLock()
        return original_run(self, *args, **kwargs)

    def record(self, account_id, username, status):
        with self._login_status_lock:
            if account_id in self._login_status_map:
                status = self._login_status_map[account_id]
                if status is None:
                    return
        return original_record(self, account_id, username, status)

    def relogin(self, bot, acc, name):
        result = original_relogin(self, bot, acc, name)
        account_id = acc.get('id')
        if result:
            with self._login_status_lock:
                self._login_status_map.pop(account_id, None)
            return result
        reason = getattr(bot, 'last_login_failure', '')
        if self.stop_event.is_set():
            reason = 'stopped'
        status = failure_status(reason)
        with self._login_status_lock:
            self._login_status_map[account_id] = status
        if status is not None and account_id is not None:
            acc['status'] = status
            self._record_result(account_id, acc.get('username') or name, status)
            # Persist the failed login even if the runner's report callback is absent.
            import local_account_results as store
            store.apply_results(None, {'id': account_id, 'status': status, '_check_change_proxy': True, '_check_refresh_cookie': True})
            self._log(f'[account] {name}: cập nhật {status.upper()} (lỗi login: {reason or "chưa xác định"}).')
        return result

    Runner.run = run
    Runner._record_result = record
    Runner._try_relogin = relogin
