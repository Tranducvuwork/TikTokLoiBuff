"""Shuffle a copy of the selected run list; prioritize high-trust EU-TTP2 accounts."""
import random, threading


def _trust_tier(account: dict) -> int:
    if not isinstance(account, dict):
        return 0
    cookie = str(account.get('cookie') or '').lower()
    if 'eu-ttp2' in cookie or 'no1a' in cookie or 'us-eastred' in cookie:
        return 0
    if bool(str(account.get('email') or '').strip()):
        return 1
    return 2


def arrange(accounts, ids, shuffled):
    selected = list(accounts)
    if ids:
        wanted = {str(value) for value in ids}
        selected = [account for account in selected if str(account.get('id')) in wanted]
    tier0 = [acc for acc in selected if _trust_tier(acc) == 0]
    tier1 = [acc for acc in selected if _trust_tier(acc) == 1]
    tier2 = [acc for acc in selected if _trust_tier(acc) == 2]
    if shuffled:
        rng = random.SystemRandom()
        rng.shuffle(tier0)
        rng.shuffle(tier1)
        rng.shuffle(tier2)
    return tier0 + tier1 + tier2


def install():
    from client.app.bridge import Bridge
    if getattr(Bridge, '_random_order_installed', False):
        return
    original_start, original_report = Bridge.start_run, Bridge._run_and_report
    lock = threading.RLock()
    def start(self, options):
        cleaned = dict(options)
        choice = (cleaned.pop('random_accounts', False) is True, cleaned.pop('account_ids', None))
        with lock:
            if getattr(self, '_random_order_choice', None) is not None:
                # A start can succeed without reaching report (or be cancelled).
                # Only discard its snapshot once the actual worker has stopped.
                thread = getattr(self, '_runner_thread', None)
                stale = (hasattr(self, '_runner_thread')
                         and not getattr(self, '_random_order_starting', False)
                         and (thread is None or not thread.is_alive()))
                if not stale:
                    return {'ok': False, 'error': 'Đang có lượt chạy; hãy dừng trước khi bắt đầu lượt mới.'}
                self._random_order_choice = None
            self._random_order_choice = choice
            self._random_order_starting = True
        try:
            result = original_start(self, cleaned)
        except Exception:
            with lock:
                self._random_order_choice = None
            raise
        finally:
            with lock:
                self._random_order_starting = False
        if not isinstance(result, dict) or not result.get('ok'):
            with lock:
                self._random_order_choice = None
        return result
    def report(self, accounts, proxies, opts):
        choice = getattr(self, '_random_order_choice', None) or (False, None)
        try:
            return original_report(self, arrange(accounts, choice[1], choice[0]), proxies, opts)
        finally:
            with lock:
                self._random_order_choice = None
    Bridge.start_run, Bridge._run_and_report = start, report
    Bridge._random_order_installed = True
