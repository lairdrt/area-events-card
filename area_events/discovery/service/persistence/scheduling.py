"""Scheduling storage: the SchedulingStorage port. Reads the configuration (areas, settings) and keeps the durable crawl jobs in line with it; the policy (priorities, intervals) is
handed in by the caller and is a pure object."""
from ..contracts import job_key_for


class SqliteSchedulingStorage:
    def __init__(self, store, areas, settings, jobs, clock):
        self._store, self._areas, self._settings, self._jobs, self._clock = store, areas, settings, jobs, clock

    def is_paused(self):
        with self._store.read() as connection:
            return self._settings.all(connection).paused

    def synchronize(self, policy):
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
                priority = policy.priority(intensity)
                key = job_key_for(source_key)
                self._jobs.ensure(tx, key, source_id, now, priority=priority)
                tx.execute("UPDATE jobs SET priority=? WHERE job_key=? AND state <> 'leased'", (priority, key))
                wait = policy.interval(interval_s, intensity, settings.mode, quiet)
                due = max((last_success or 0) + wait, now)
                brought += tx.execute("UPDATE jobs SET next_run_at=?, updated_at=? WHERE job_key=? AND state <> 'leased' AND next_run_at > ?",
                                      (due, now, key, due)).rowcount
            self._settings.set(tx, "scheduler_seen_seq", max(latest, seen), now)
        return {"changed": True, "sources": len(eligible), "brought_forward": brought}

    def next_due(self):
        with self._store.read() as connection:
            row = connection.execute("""SELECT MIN(j.next_run_at) FROM jobs j WHERE j.state IN ('ready', 'done', 'failed')
                                        AND j.source_id IN (SELECT x.source_id FROM area_sources x JOIN areas a USING (area_id) WHERE a.enabled=1)""").fetchone()
        return row[0]
