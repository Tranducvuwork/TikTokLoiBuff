"""Exclude explicit pause intervals from the existing phase timers."""
import threading
import time


class ActiveRunClock:
    def __init__(self):
        self.intervals = []
        self.paused_at = None
        self.lock = threading.RLock()

    def pause(self, now=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            if self.paused_at is None:
                self.paused_at = now

    def resume(self, now=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            if self.paused_at is not None:
                self.intervals.append((self.paused_at, max(self.paused_at, now)))
                self.paused_at = None

    def active(self, start, elapsed):
        elapsed = max(0.0, float(elapsed or 0.0))
        start = float(start or 0.0)
        if start <= 0:
            return elapsed
        end = start + elapsed
        with self.lock:
            intervals = list(self.intervals)
            if self.paused_at is not None:
                intervals.append((self.paused_at, end))
        excluded = sum(max(0.0, min(end, right) - max(start, left))
                       for left, right in intervals)
        return max(0.0, elapsed - excluded)

    def adjust(self, timing, runner):
        timing = dict(timing)
        for key, start_name in (
            ('scout_elapsed', '_scout_started_at'),
            ('like_elapsed', '_like_started_at'),
            ('run_elapsed', '_timing_started_at'),
            ('total_elapsed', '_timing_started_at'),
        ):
            if key in timing:
                timing[key] = self.active(getattr(runner, start_name, 0), timing[key])
        with self.lock:
            timing['paused'] = self.paused_at is not None
        return timing


def install():
    from client.app.bridge import Bridge
    if getattr(Bridge, '_active_run_timing_v91', False):
        return
    start = Bridge.start_run
    pause, resume = Bridge.pause_run, Bridge.resume_run
    progress = Bridge.get_progress

    def accepted(result):
        return not (isinstance(result, dict) and result.get('ok') is False)

    def start_run(self, *args, **kwargs):
        result = start(self, *args, **kwargs)
        if accepted(result):
            self._active_run_clock_v91 = ActiveRunClock()
        return result

    def pause_run(self, *args, **kwargs):
        at = time.monotonic()
        result = pause(self, *args, **kwargs)
        clock = getattr(self, '_active_run_clock_v91', None)
        if clock is not None and accepted(result):
            clock.pause(at)
        return result

    def resume_run(self, *args, **kwargs):
        result = resume(self, *args, **kwargs)
        clock = getattr(self, '_active_run_clock_v91', None)
        if clock is not None and accepted(result):
            clock.resume()
        return result

    def get_progress(self, *args, **kwargs):
        result = dict(progress(self, *args, **kwargs))
        clock = getattr(self, '_active_run_clock_v91', None)
        if clock is None or not isinstance(result.get('timing'), dict):
            return result
        runners = [value for value in vars(self).values()
                   if hasattr(value, '_timing_started_at')]
        if runners:
            runner = max(runners, key=lambda value: getattr(value, '_timing_started_at', 0))
            result['timing'] = clock.adjust(result['timing'], runner)
        return result

    Bridge.start_run, Bridge.pause_run = start_run, pause_run
    Bridge.resume_run, Bridge.get_progress = resume_run, get_progress
    Bridge._active_run_timing_v91 = True
