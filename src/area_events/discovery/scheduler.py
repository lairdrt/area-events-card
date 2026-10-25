"""DiscoveryScheduler: turns the persisted configuration (areas, their sources, mode, intensity, pause) into durable crawl jobs.

It reads only the discovery database, so it needs no card, no Home Assistant and no downstream component. Planning is cheap and
bounded: a read decides whether the control configuration changed since the scheduler last looked (the highest `area` or `settings`
change sequence it has seen is kept in `settings.scheduler_seen_seq`); only then does one short write transaction create the jobs of
newly covered sources and bring forward the next run of sources whose pacing became faster.

One crawl job per *source*: sources shared by overlapping areas, or by many cards, are crawled once.

Pacing (the wait after a job completes), never below `min_interval_s` and never faster than HostPolicy and robots.txt allow, because
those apply when the page is fetched:
    manual mode      the global intensity: low x2, normal x1, high x0.5 of the source's base interval
    automatic mode   the strongest intensity among the enabled areas that use the source; a source that has produced no change for
                     2 runs in a row is asked for half as often, for 4 runs a quarter as often, and any change restores the base
"""
from dataclasses import dataclass

from ..persistence.ports import PacingDecision, SchedulingStorage

@dataclass(frozen=True)
class SchedulerConfig:
    min_interval_s: int = 300
    max_interval_s: int = 86400
    low_factor: float = 2.0
    normal_factor: float = 1.0
    high_factor: float = 0.5
    quiet_after_runs: int = 2
    quieter_after_runs: int = 4
    priority_low: int = 30
    priority_normal: int = 50
    priority_high: int = 70


class AreaPacing:
    """Pacing from the configuration: intensity of the areas (or the global manual intensity), and quiet-run slowing in automatic mode. A pure policy: Persistence hands it the
    facts (`PacingFacts`) and applies its answer; it never touches the database. It is both the PacingPolicy of a finished run and the SchedulingPolicy of planning."""

    def __init__(self, config: SchedulerConfig = SchedulerConfig()):
        self.config = config

    def priority(self, intensity):
        return {"low": self.config.priority_low, "normal": self.config.priority_normal, "high": self.config.priority_high}[intensity]

    def interval(self, base_interval_s, intensity, mode, quiet_runs):
        c = self.config
        factor = {"low": c.low_factor, "normal": c.normal_factor, "high": c.high_factor}[intensity]
        if mode == "automatic":
            if quiet_runs >= c.quieter_after_runs:
                factor *= 4
            elif quiet_runs >= c.quiet_after_runs:
                factor *= 2
        return int(min(max(base_interval_s * factor, c.min_interval_s), c.max_interval_s))

    def next_run(self, facts):
        quiet = 0 if facts.changed else facts.quiet_runs + 1
        intensity = (facts.strongest or "normal") if facts.mode == "automatic" else facts.intensity
        return PacingDecision(facts.now + self.interval(facts.interval_s, intensity, facts.mode, quiet), min(quiet, 100))


class DiscoveryScheduler:
    """Keeps the durable crawl jobs in line with the configuration. The configuration and the jobs live in Persistence (the SchedulingStorage port); this object supplies the policy."""

    def __init__(self, storage: SchedulingStorage, pacing: AreaPacing):
        self._storage, self.pacing = storage, pacing

    def paused(self):
        return self._storage.is_paused()

    def plan(self):
        """Bring jobs in line with the configuration if it changed. Returns {'changed': bool, 'sources': n, 'brought_forward': n}."""
        return self._storage.synchronize(self.pacing)

    def next_due(self):
        """When the earliest job of an eligible source is due (epoch seconds), or None when nothing is scheduled."""
        return self._storage.next_due()
