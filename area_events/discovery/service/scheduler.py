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

from .areas import AreaRepository, SettingsRepository


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


class FixedPacing:
    """The 2A behaviour: a source is asked for again after its own interval."""

    def after_run(self, tx, source_id, interval_s, now, changed):
        return now + int(interval_s)


class AreaPacing:
    """Pacing from the configuration: intensity of the areas (or the global manual intensity), and quiet-run slowing in automatic mode."""

    def __init__(self, areas: AreaRepository, settings: SettingsRepository, config: SchedulerConfig = SchedulerConfig()):
        self._areas, self._settings, self.config = areas, settings, config

    def interval(self, base_interval_s, intensity, mode, quiet_runs):
        c = self.config
        factor = {"low": c.low_factor, "normal": c.normal_factor, "high": c.high_factor}[intensity]
        if mode == "automatic":
            if quiet_runs >= c.quieter_after_runs:
                factor *= 4
            elif quiet_runs >= c.quiet_after_runs:
                factor *= 2
        return int(min(max(base_interval_s * factor, c.min_interval_s), c.max_interval_s))

    def after_run(self, tx, source_id, interval_s, now, changed):
        quiet = 0 if changed else int(tx.execute("SELECT quiet_runs FROM sources WHERE source_id=?", (source_id,)).fetchone()[0]) + 1
        tx.execute("UPDATE sources SET quiet_runs=? WHERE source_id=?", (min(quiet, 100), source_id))
        settings = self._settings.all(tx)
        intensity = settings.intensity
        if settings.mode == "automatic":
            rows = [r for r in self._areas.eligible_sources(tx) if r[0] == source_id]
            intensity = rows[0][5] if rows else "normal"
        return now + self.interval(interval_s, intensity, settings.mode, quiet)


class DiscoveryScheduler:
    def __init__(self, store, areas: AreaRepository, settings: SettingsRepository, jobs, changes, clock, pacing: AreaPacing):
        self._store, self._areas, self._settings, self._jobs, self._changes = store, areas, settings, jobs, changes
        self._clock, self.pacing = clock, pacing

    def paused(self):
        with self._store.read() as connection:
            return self._settings.all(connection).paused

    def plan(self):
        """Bring jobs in line with the configuration if it changed. Returns {'changed': bool, 'sources': n, 'brought_forward': n}."""
        now = int(self._clock())
        with self._store.read() as connection:
            seen = int(self._settings.get(connection, "scheduler_seen_seq"))
            latest = connection.execute("SELECT COALESCE(MAX(seq), 0) FROM changes WHERE entity IN ('area', 'settings')").fetchone()[0]
            eligible = self._areas.eligible_sources(connection)
            missing = connection.execute("""SELECT COUNT(*) FROM sources s WHERE s.enabled=1 AND s.source_id IN (SELECT source_id FROM area_sources)
                                            AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.source_id=s.source_id)""").fetchone()[0]
        if latest <= seen and not missing:
            return {"changed": False, "sources": len(eligible), "brought_forward": 0}
        brought = 0
        with self._store.write() as tx:
            settings = self._settings.all(tx)
            for source_id, source_key, interval_s, last_success, quiet, strongest in self._areas.eligible_sources(tx):
                intensity = settings.intensity if settings.mode == "manual" else strongest
                priority = {"low": self.pacing.config.priority_low, "normal": self.pacing.config.priority_normal, "high": self.pacing.config.priority_high}[intensity]
                key = f"crawl_source:{source_key}"
                self._jobs.ensure(tx, key, source_id, now, priority=priority)
                tx.execute("UPDATE jobs SET priority=? WHERE job_key=? AND state <> 'leased'", (priority, key))
                wait = self.pacing.interval(interval_s, intensity, settings.mode, quiet)
                due = max((last_success or 0) + wait, now)
                brought += tx.execute("UPDATE jobs SET next_run_at=?, updated_at=? WHERE job_key=? AND state <> 'leased' AND next_run_at > ?",
                                      (due, now, key, due)).rowcount
            self._settings.set(tx, "scheduler_seen_seq", max(latest, seen), now)
        return {"changed": True, "sources": len(eligible), "brought_forward": brought}

    def next_due(self):
        """When the earliest job of an eligible source is due (epoch seconds), or None when nothing is scheduled."""
        with self._store.read() as connection:
            row = connection.execute("""SELECT MIN(j.next_run_at) FROM jobs j WHERE j.state IN ('ready', 'done', 'failed')
                                        AND j.source_id IN (SELECT x.source_id FROM area_sources x JOIN areas a USING (area_id) WHERE a.enabled=1)""").fetchone()
        return row[0]
