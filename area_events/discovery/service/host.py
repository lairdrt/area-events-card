"""DiscoveryHost: the independently running Discovery Service process logic.

A host owns one background loop: recover expired leases, plan (the scheduler reacts to configuration changes), run due jobs (a bounded
number per cycle, so every source gets its turn), prune old data now and then, then wait. The wait is an Event wait with a bounded length
(the time to the next due job, between `busy_wait_s` and `idle_wait_s`): no busy polling, and `stop()` wakes it at once.

Nothing in the loop calls classification, geocoding, enrichment, validation or Home Assistant; a stalled or failing source costs at most
its own job (request and time budgets, lease expiry), and a failing cycle is counted, logged and retried after a growing pause.
`start()` and `stop()` are explicit; `stop()` lets the current job finish (it is bounded), then closes nothing it does not own: the
composition root closes the store after the host has stopped.
"""
import logging
import threading
from dataclasses import dataclass

log = logging.getLogger("area-events.discovery")


@dataclass(frozen=True)
class HostConfig:
    idle_wait_s: float = 5.0
    busy_wait_s: float = 0.2
    max_jobs_per_cycle: int = 3
    recover_every_s: int = 60
    prune_every_s: int = 86400
    error_wait_s: float = 5.0
    error_wait_cap_s: float = 300.0


class DiscoveryHost:
    def __init__(self, service, scheduler, clock, config: HostConfig = HostConfig()):
        self._service, self._scheduler, self._clock, self.config = service, scheduler, clock, config
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self._state = {"state": "stopped", "cycles": 0, "jobs_run": 0, "errors": 0, "last_cycle_at": None, "last_job": None, "last_error": None, "started_at": None}
        self._last_recover = self._last_prune = 0
        self._error_wait = config.error_wait_s

    # --- lifecycle ---------------------------------------------------------------------------------------------------

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return self
        self._stop.clear()
        with self._lock:
            self._state.update(state="running", started_at=int(self._clock()))
        self._thread = threading.Thread(target=self._loop, name="discovery-host", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout=60.0):
        """Ask the loop to finish its current job and exit; returns True when it has."""
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
            if thread.is_alive():
                return False
        with self._lock:
            self._state.update(state="stopped")
        return True

    def request_stop(self):
        """Signal a stop without waiting (for a signal handler or the API)."""
        self._stop.set()

    @property
    def stopping(self):
        return self._stop.is_set()

    def wait(self, timeout=None):
        """Block until the host has been told to stop (the serving process waits here)."""
        return self._stop.wait(timeout)

    def status(self):
        with self._lock:
            return dict(self._state)

    # --- one cycle (also called directly by tests and by `run`) -----------------------------------------------------

    def run_cycle(self):
        now = int(self._clock())
        report = {"jobs": 0, "outcomes": [], "plan": None}
        if now - self._last_recover >= self.config.recover_every_s:
            self._service.recover()
            self._last_recover = now
        if now - self._last_prune >= self.config.prune_every_s:
            if self._last_prune:
                self._service.prune()
            self._last_prune = now
        report["plan"] = self._scheduler.plan()
        if self._scheduler.paused():
            self._note(state="paused")
            return report
        self._note(state="running")
        for _ in range(self.config.max_jobs_per_cycle):
            if self._stop.is_set():
                break
            outcome = self._service.run_once()
            if outcome.outcome == "nothing_due":
                break
            report["jobs"] += 1
            report["outcomes"].append(outcome.outcome)
            self._note(jobs_run=1, last_job={"job": outcome.job_key, "outcome": outcome.outcome, "at": now, "error": outcome.error})
            if outcome.outcome in ("paused", "storage_unavailable"):
                break
        self._note(cycles=1, last_cycle_at=now)
        return report

    # --- internals ---------------------------------------------------------------------------------------------------

    def _note(self, **values):
        with self._lock:
            for key, value in values.items():
                if key in ("cycles", "jobs_run", "errors"):
                    self._state[key] += value
                else:
                    self._state[key] = value

    def _loop(self):
        while not self._stop.is_set():
            try:
                report = self.run_cycle()
                self._error_wait = self.config.error_wait_s
                if report["jobs"]:
                    wait = self.config.busy_wait_s
                else:
                    due = self._scheduler.next_due()
                    wait = self.config.idle_wait_s if due is None else min(self.config.idle_wait_s, max(due - self._clock(), self.config.busy_wait_s))
            except Exception as error:  # a cycle must never kill the host: count it, log it, wait longer
                log.exception("discovery cycle failed")
                self._note(errors=1, last_error=f"{type(error).__name__}: {str(error)[:200]}")
                wait = self._error_wait
                self._error_wait = min(self._error_wait * 2, self.config.error_wait_cap_s)
            self._stop.wait(wait)
        self._note(state="stopped")
