"""Where media enrichment keeps its results: metadata in the one discovery database, image files in a bounded managed folder.

    MediaRepository      the SQL (records of resolved places and cached source photographs, file metadata, per-candidate media, the work list)
    SqliteMediaStore     the same interface the accepted imagery code (`discovery/media.py`, MediaService) used for its JSON index and files,
                         so that code runs unchanged on top of the database
    LocalMediaWork       what the enrichment worker sees of the database: pending candidates in, finished media out. No SQL leaves this module.
    LocalMediaFiles      what the API sees: a stored file by its content-derived name

Files are named by content or by what they depict, validated on every read and write (no path ever comes from a request), and bounded: records
expire, files no record uses are removed, and the whole folder is kept under a byte budget by dropping the oldest records.
"""
import copy
import json
import os
import re
import time
import types

from ..contracts import ENRICHMENT_VERSION, MAX_FILE_BYTES, MEDIA_SCHEMA

FILE_NAME = re.compile(r"[a-z0-9][a-z0-9.-]{3,60}\.(svg|jpg|png|webp)")
CONTENT_TYPES = types.MappingProxyType({"svg": "image/svg+xml", "jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"})
RECORD_TTL = 60 * 86400
REFUSAL_TTL = 14 * 86400
MAX_RECORDS = 4000
MILES_PER_KM = 0.621371


def content_type_of(name):
    match = FILE_NAME.fullmatch(name or "")
    return CONTENT_TYPES[name.rsplit(".", 1)[1]] if match else None


class MediaRepository:
    def records(self, connection):
        return {r["key"]: json.loads(r["record_json"]) for r in connection.execute("SELECT key, record_json FROM media_records")}

    def save_record(self, tx, key, record):
        tx.execute("""INSERT INTO media_records (key, ok, at, record_json) VALUES (?, ?, ?, ?)
                      ON CONFLICT(key) DO UPDATE SET ok=excluded.ok, at=excluded.at, record_json=excluded.record_json""",
                   (key, 1 if record.get("ok") else 0, int(record.get("at") or 0), json.dumps(record, ensure_ascii=False, default=str)))

    def delete_record(self, tx, key):
        tx.execute("DELETE FROM media_records WHERE key=?", (key,))

    def register_file(self, tx, name, size, now):
        tx.execute("""INSERT INTO media_files (name, media_type, bytes, stored_at) VALUES (?, ?, ?, ?)
                      ON CONFLICT(name) DO UPDATE SET media_type=excluded.media_type, bytes=excluded.bytes""", (name, content_type_of(name), size, now))

    def known_files(self, connection):
        return {r["name"]: r["bytes"] for r in connection.execute("SELECT name, bytes FROM media_files")}

    def forget_files(self, tx, names):
        tx.executemany("DELETE FROM media_files WHERE name=?", [(n,) for n in names])

    def save_candidate_media(self, tx, candidate_id, payload_hash, media, illustration, complete, now, version=ENRICHMENT_VERSION):
        tx.execute("""INSERT INTO candidate_media (candidate_id, payload_hash, media_json, illustration_json, complete, updated_at, version) VALUES (?, ?, ?, ?, ?, ?, ?)
                      ON CONFLICT(candidate_id) DO UPDATE SET payload_hash=excluded.payload_hash, media_json=excluded.media_json, illustration_json=excluded.illustration_json,
                        complete=excluded.complete, updated_at=excluded.updated_at, version=excluded.version""",
                   (candidate_id, payload_hash, json.dumps(media, ensure_ascii=False), json.dumps(illustration, ensure_ascii=False) if illustration else None, 1 if complete else 0, now, version))

    def for_event(self, connection, event_id):
        """The media of an event's primary candidate: (media dict, illustration dict) or (None, None)."""
        row = connection.execute("""SELECT m.media_json, m.illustration_json FROM event_candidates l JOIN candidate_media m USING (candidate_id)
                                    WHERE l.event_id=? AND l.role='primary'""", (event_id,)).fetchone()
        if row is None:
            return None, None
        return json.loads(row["media_json"]) or None, json.loads(row["illustration_json"]) if row["illustration_json"] else None

    def pending(self, connection, floor, retry_before, limit):
        """Upcoming candidates of enabled areas that have no media yet, were enriched for an older revision, or are unfinished and due for another look."""
        return connection.execute("""SELECT c.candidate_id, c.candidate_key, c.payload_hash, c.payload_json, c.source_id, s.source_key, s.name AS source_name
                                     FROM candidates c JOIN sources s USING (source_id) LEFT JOIN candidate_media m ON m.candidate_id = c.candidate_id
                                     WHERE c.starts_at >= :floor
                                       AND EXISTS (SELECT 1 FROM area_sources x JOIN areas a USING (area_id) WHERE x.source_id = c.source_id AND a.enabled = 1)
                                       AND (m.candidate_id IS NULL OR m.payload_hash <> c.payload_hash OR m.version < :version OR (m.complete = 0 AND m.updated_at <= :retry))
                                     ORDER BY c.starts_at, c.candidate_id LIMIT :limit""", {"floor": floor, "retry": retry_before, "limit": limit, "version": ENRICHMENT_VERSION}).fetchall()

    def area_of(self, connection, source_id):
        row = connection.execute("""SELECT a.* FROM area_sources x JOIN areas a USING (area_id) WHERE x.source_id=? AND a.enabled=1 ORDER BY a.area_id LIMIT 1""", (source_id,)).fetchone()
        if row is None:
            return None
        miles = row["radius"] * (MILES_PER_KM if row["unit"] == "km" else 1.0)
        return {"latitude": row["latitude"], "longitude": row["longitude"], "radius_miles": miles, "name": row["name"], "display_name": row["name"]}

    def titles_sharing_image(self, connection, url):
        """How many different event titles carry exactly this image address: an image shared by several unrelated events is a default, not a photograph of any of them."""
        needle = json.dumps({"image_url": url})[1:-1]
        return connection.execute("SELECT COUNT(DISTINCT title) FROM candidates WHERE payload_json LIKE ? ESCAPE '\\'",
                                  ("%" + needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%",)).fetchone()[0]

    def counts(self, connection):
        one = lambda sql: int(connection.execute(sql).fetchone()[0])  # noqa: E731
        return {"candidates_with_media": one("SELECT COUNT(*) FROM candidate_media"), "complete": one("SELECT COUNT(*) FROM candidate_media WHERE complete = 1"),
                "records": one("SELECT COUNT(*) FROM media_records"), "verified_places": one("SELECT COUNT(*) FROM media_records WHERE ok = 1 AND record_json LIKE '%\"map\"%'"),
                "files": one("SELECT COUNT(*) FROM media_files"), "file_bytes": one("SELECT COALESCE(SUM(bytes), 0) FROM media_files")}


class SqliteMediaStore:
    """Private cache state and file access backed by Persistence; callers use explicit record operations."""

    def __init__(self, store, repository, folder, clock=time.time, max_bytes=250_000_000):
        self._store, self._repository, self.clock, self.max_bytes = store, repository, clock, max_bytes
        self.folder = folder
        self.files = os.path.join(folder, "files")
        os.makedirs(self.files, exist_ok=True)
        with store.read() as connection:
            records = repository.records(connection)
        self._index = {"version": 1, "saved_at": 0, "records": records}
        self._saved = {key: json.dumps(record, sort_keys=True, default=str) for key, record in records.items()}

    def get_record(self, key):
        return copy.deepcopy(self._index["records"].get(key))

    def get_records(self, keys):
        records = self._index["records"]
        return {key: copy.deepcopy(records[key]) for key in keys if key in records}

    def put_record(self, key, record):
        if not isinstance(key, str) or not 3 <= len(key) <= 80 or not isinstance(record, dict):
            raise ValueError("invalid media cache record")
        self._index["records"][key] = copy.deepcopy(record)

    def mark_saved(self, at):
        self._index["saved_at"] = int(at)

    def revision(self):
        records = self._index["records"]
        return int(self._index.get("saved_at") or 0) if records else 0

    # --- files -----------------------------------------------------------------------------------------------------

    def put_file(self, name, data):
        if not FILE_NAME.fullmatch(name or "") or not isinstance(data, (bytes, bytearray)) or not 0 < len(data) <= MAX_FILE_BYTES:
            raise ValueError("unsafe or oversized file")
        temporary = os.path.join(self.files, name + ".tmp")
        with open(temporary, "wb") as handle:
            handle.write(data)
        os.replace(temporary, os.path.join(self.files, name))
        with self._store.write() as tx:
            self._repository.register_file(tx, name, len(data), int(self.clock()))

    def read_file(self, name):
        if not FILE_NAME.fullmatch(name or ""):
            return None
        try:
            with open(os.path.join(self.files, name), "rb") as handle:
                return handle.read(MAX_FILE_BYTES + 1)
        except OSError:
            return None

    def has_file(self, name):
        return bool(FILE_NAME.fullmatch(name or "")) and os.path.isfile(os.path.join(self.files, name))

    def intact(self, record):
        """Is every file a verified record points at still stored? (A record whose files are gone must not be advertised.)"""
        if not record.get("ok"):
            return True
        names = list((record.get("map") or {}).get("files", {}).values())
        if record.get("photo"):
            names.append(record["photo"].get("file"))
        return bool(names) and all(self.has_file(name) for name in names)

    # --- records ------------------------------------------------------------------------------------------------------

    def save(self):
        """Write the records that changed since the last save (and remove the ones that were dropped)."""
        records = self._index["records"]
        now = {key: json.dumps(record, sort_keys=True, default=str) for key, record in records.items()}
        changed = [key for key, text in now.items() if self._saved.get(key) != text]
        removed = [key for key in self._saved if key not in now]
        if changed or removed:
            with self._store.write() as tx:
                for key in changed:
                    self._repository.save_record(tx, key, records[key])
                for key in removed:
                    self._repository.delete_record(tx, key)
        self._saved = now

    def prune(self):
        """Drop expired records, the oldest beyond the record and byte budgets, and files no record points at."""
        now, records = self.clock(), self._index["records"]
        for key in [k for k, r in records.items() if now - r.get("at", 0) > (RECORD_TTL if r.get("ok") else REFUSAL_TTL)]:
            del records[key]
        if len(records) > MAX_RECORDS:
            for key in sorted(records, key=lambda k: records[k].get("at", 0))[:len(records) - MAX_RECORDS]:
                del records[key]
        sizes = self._sizes(self._referenced())
        for key in sorted(records, key=lambda k: records[k].get("at", 0)):
            if sum(sizes.values()) <= self.max_bytes:
                break
            del records[key]
            sizes = self._sizes(self._referenced())
        keep = set(self._referenced())
        removed = []
        try:
            for name in os.listdir(self.files):
                if name not in keep:
                    os.remove(os.path.join(self.files, name))
                    removed.append(name)
        except OSError:
            pass
        if removed:
            with self._store.write() as tx:
                self._repository.forget_files(tx, removed)

    def _referenced(self):
        names = set()
        for record in self._index["records"].values():
            if not record.get("ok"):
                continue
            if record.get("photo"):
                names.add(record["photo"]["file"])
            names |= set((record.get("map") or {}).get("files", {}).values())
        return names

    def _sizes(self, names):
        sizes = {}
        for name in names:
            try:
                sizes[name] = os.path.getsize(os.path.join(self.files, name))
            except OSError:
                sizes[name] = 0
        return sizes


class LocalMediaWork:
    """The enrichment worker's view of the database: `pending` work out, `finish` results in. The worker holds no connection and runs no SQL."""

    def __init__(self, store, repository, clock=time.time, floor_hours=12, retry_after_s=60):
        self._store, self._repository, self._clock = store, repository, clock
        self.floor_hours, self.retry_after_s = floor_hours, retry_after_s

    def pending(self, limit):
        now = int(self._clock())
        with self._store.read() as connection:
            rows = self._repository.pending(connection, now - self.floor_hours * 3600, now - self.retry_after_s, limit)
            items = []
            for row in rows:
                area = self._repository.area_of(connection, row["source_id"])
                if area is None:
                    continue
                items.append({"candidate_id": row["candidate_id"], "candidate_key": row["candidate_key"], "payload_hash": row["payload_hash"], "payload": json.loads(row["payload_json"]),
                              "source_key": row["source_key"], "source_name": row["source_name"], "area": area})
        return items

    def titles_sharing_image(self, url):
        with self._store.read() as connection:
            return self._repository.titles_sharing_image(connection, url)

    def finish(self, results):
        """Store finished media. A result made for an older revision of its candidate is stored anyway (it is replaced by the next pass), never lost."""
        now = int(self._clock())
        with self._store.write() as tx:
            for item in results:
                self._repository.save_candidate_media(tx, item["candidate_id"], item["payload_hash"], item["media"], item.get("illustration"), item["complete"], now,
                                                      item.get("version", ENRICHMENT_VERSION))

    def status(self):
        with self._store.read() as connection:
            return {"schema": MEDIA_SCHEMA, **self._repository.counts(connection)}


class LocalMediaFiles:
    """The API's view: the bytes and content type of a stored file, by name."""

    def __init__(self, media_store):
        self._store = media_store

    def read(self, name):
        kind = content_type_of(name)
        data = self._store.read_file(name) if kind else None
        return (data, kind) if data and len(data) <= MAX_FILE_BYTES else None
