"""Repositories for the control plane: discovery areas, their sources, and the shared discovery settings.

Same rules as the data repositories: explicit SQL, writes take the caller's transaction, reads take a read connection, no state
between calls. An area names a place to cover (center, radius, unit); it is never geocoded here (coordinates are validated by the
control service), and nothing in it constrains which source evidence is stored.
"""
from typing import Optional

from ..contracts.models import INTENSITIES, MODES, UNITS, AreaRecord, SettingsRecord  # noqa: F401


class AreaRepository:
    def create(self, tx, area_key, request_id, name, latitude, longitude, radius, unit, enabled, intensity, now):
        return tx.execute("""INSERT INTO areas (area_key, request_id, name, latitude, longitude, radius, unit, enabled, intensity, revision, created_at, updated_at)
                             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?) RETURNING area_id""",
                          (area_key, request_id, name, latitude, longitude, radius, unit, 1 if enabled else 0, intensity, now, now)).fetchone()[0]

    def update(self, tx, area_id, fields, now):
        """Apply `fields` (column -> value) and bump the revision. Returns the new revision."""
        assignments = ", ".join(f"{column}=?" for column in fields)
        tx.execute(f"UPDATE areas SET {assignments}, revision=revision+1, updated_at=? WHERE area_id=?", (*fields.values(), now, area_id))
        return tx.execute("SELECT revision FROM areas WHERE area_id=?", (area_id,)).fetchone()[0]

    def set_sources(self, tx, area_id, source_ids, now):
        """Make the area's sources exactly `source_ids`. Returns True when that changed anything."""
        current = {r[0] for r in tx.execute("SELECT source_id FROM area_sources WHERE area_id=?", (area_id,))}
        wanted = set(source_ids)
        for source_id in current - wanted:
            tx.execute("DELETE FROM area_sources WHERE area_id=? AND source_id=?", (area_id, source_id))
        for source_id in wanted - current:
            tx.execute("INSERT INTO area_sources (area_id, source_id, added_at) VALUES (?, ?, ?)", (area_id, source_id, now))
        return current != wanted

    def row_by_key(self, connection, area_key):
        return connection.execute("SELECT * FROM areas WHERE area_key=?", (area_key,)).fetchone()

    def row_by_request(self, connection, request_id):
        return connection.execute("SELECT * FROM areas WHERE request_id=?", (request_id,)).fetchone()

    def source_ids(self, connection, area_id):
        return [r[0] for r in connection.execute("SELECT source_id FROM area_sources WHERE area_id=?", (area_id,))]

    def record(self, connection, row) -> AreaRecord:
        keys = tuple(r[0] for r in connection.execute("""SELECT s.source_key FROM area_sources a JOIN sources s USING (source_id)
                                                        WHERE a.area_id=? ORDER BY s.source_key""", (row["area_id"],)))
        return AreaRecord(row["area_key"], row["name"], row["latitude"], row["longitude"], row["radius"], row["unit"], bool(row["enabled"]), row["intensity"],
                          row["revision"], row["created_at"], row["updated_at"], keys)

    def all(self, connection):
        return tuple(self.record(connection, row) for row in connection.execute("SELECT * FROM areas ORDER BY name, area_key"))

    def get(self, connection, area_key) -> Optional[AreaRecord]:
        row = self.row_by_key(connection, area_key)
        return self.record(connection, row) if row else None

    def eligible_sources(self, connection):
        """(source_id, source_key, interval_s, last_success_at, quiet_runs, strongest intensity) for every enabled source that at least
        one enabled area uses. One row per source however many areas share it: overlapping areas never mean a second crawl."""
        rows = connection.execute("""SELECT s.source_id, s.source_key, s.interval_s, s.last_success_at, s.quiet_runs,
                                            MAX(CASE a.intensity WHEN 'high' THEN 2 WHEN 'normal' THEN 1 ELSE 0 END) AS rank
                                     FROM sources s JOIN area_sources x ON x.source_id = s.source_id JOIN areas a ON a.area_id = x.area_id
                                     WHERE s.enabled = 1 AND a.enabled = 1 GROUP BY s.source_id ORDER BY s.source_key""").fetchall()
        return [(r[0], r[1], r[2], r[3], r[4], INTENSITIES[r[5]]) for r in rows]


class SettingsRepository:
    def all(self, connection) -> SettingsRecord:
        values = {r[0]: (r[1], r[2]) for r in connection.execute("SELECT key, value, updated_at FROM settings")}
        return SettingsRecord(values["mode"][0], values["intensity"][0], values["paused"][0] == "1", int(values["revision"][0]),
                              max(updated for _, updated in values.values()))

    def set(self, tx, key, value, now):
        tx.execute("UPDATE settings SET value=?, updated_at=? WHERE key=?", (str(value), now, key))

    def bump(self, tx, now):
        tx.execute("UPDATE settings SET value=CAST(CAST(value AS INTEGER) + 1 AS TEXT), updated_at=? WHERE key='revision'", (now,))
        return int(tx.execute("SELECT value FROM settings WHERE key='revision'").fetchone()[0])

    def get(self, connection, key):
        return connection.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()[0]
