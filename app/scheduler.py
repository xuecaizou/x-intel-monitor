"""定时调度：优先 APScheduler，不可用时降级为 threading 循环"""
import datetime
import threading
import time


class FetchScheduler:
    def __init__(self, run_fetch):
        self.run_fetch = run_fetch
        self.sched = None
        self.interval = 15
        self._last_tick = None

    def start(self, interval_minutes):
        self.interval = int(interval_minutes)
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            self.sched = BackgroundScheduler(daemon=True)
            self.sched.add_job(self._tick, "interval", minutes=self.interval, id="fetch",
                               next_run_time=datetime.datetime.now())
            self.sched.start()
        except Exception as e:
            print("[scheduler] APScheduler 不可用，降级 threading 循环:", e)
            threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        self._tick()
        while True:
            time.sleep(self.interval * 60)
            self._tick()

    def _tick(self):
        self._last_tick = datetime.datetime.now()
        try:
            self.run_fetch()
        except Exception as e:
            print("[scheduler] 抓取失败:", e)

    def reschedule(self, interval_minutes):
        """配置变更后重设调度周期（取各来源频率的最小值，保证 5 分钟来源可达）"""
        self.interval = max(1, int(interval_minutes))
        if self.sched is not None:
            try:
                self.sched.reschedule_job("fetch", minutes=self.interval)
            except Exception as e:
                print("[scheduler] 重设周期失败:", e)

    def next_run_in(self):
        if self.sched is not None:
            try:
                job = self.sched.get_job("fetch")
                if job and job.next_run_time is not None:
                    delta = job.next_run_time - datetime.datetime.now(job.next_run_time.tzinfo)
                    return max(0, int(delta.total_seconds()))
            except Exception:
                return None
            return None
        if self._last_tick is not None:
            remain = self.interval * 60 - (datetime.datetime.now() - self._last_tick).total_seconds()
            return max(0, int(remain))
        return None


_instance = None


def start(run_fetch, interval_minutes):
    global _instance
    _instance = FetchScheduler(run_fetch)
    _instance.start(interval_minutes)
    return _instance


def instance():
    return _instance
