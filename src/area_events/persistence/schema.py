"""The discovery database schema, as ordered migrations. Each migration is applied once, in its own transaction, and recorded
in `schema_migrations` and `PRAGMA user_version`. A migration is never edited after it has shipped; changes are new migrations.

Times are UTC epoch seconds. Integer ids are local to this database; the stable keys (`source_key`, `url_key`, `candidate_key`,
`content_hash`) are what leaves it.
"""

def _standard_candidates(tx):
    """Migration 7 (data): every stored candidate becomes a version-1 standard record, and the producer's working state moves out of it into its own column.

    Before this, a candidate was stored as the event dictionary the reading code built (`id` with the source key, `start`, `end`, `source`, `verification`, `extraction`, ...). A
    migration is frozen in time, so it carries its own copy of the field mapping and of the identity hash instead of importing either from the program. The mapping is applied once;
    nothing at run time reads the old form. Work already done for a candidate (extraction, media) is carried over to the new hash, because the facts did not change, only how they
    are written."""
    tx.execute("ALTER TABLE candidates ADD COLUMN state_json TEXT NOT NULL DEFAULT '{}'")
    import hashlib
    import json
    rows = tx.execute("SELECT c.candidate_id, c.candidate_key, c.payload_json, c.payload_hash, s.source_key FROM candidates c JOIN sources s USING (source_id)").fetchall()
    for row in rows:
        old = json.loads(row["payload_json"])
        prefix = row["source_key"] + ":"
        source, verification = old.get("source") or {}, old.get("verification") or {}
        document = {"schema": "discovery.producer.v1", "local_id": row["candidate_key"][len(prefix):] if row["candidate_key"].startswith(prefix) else row["candidate_key"],
                    "title": old.get("title"), "starts_at": old.get("start"), "ends_at": old.get("end"), "timezone": old.get("timezone"), "venue": old.get("venue"),
                    "address": old.get("address"), "description": old.get("description"), "url": old.get("url"), "image_url": old.get("image_url"),
                    "image_source": old.get("image_source"), "categories": list(old.get("categories") or []), "organizer": old.get("organizer"), "price": old.get("price"),
                    "geo": old.get("geo"),
                    "provenance": {"source_name": source.get("name"), "listing_url": source.get("listing_url"), "retrieved_at": old.get("retrieved_at"),
                                   "found_on": list(old.get("found_on") or [])},
                    "verification": {"status": verification.get("status", "unverified"), "evidence": list(verification.get("evidence") or []),
                                     "missing": list(verification.get("missing") or []), "note": verification.get("note", ""), "verified": bool(old.get("verified", False))}}
        stable = {k: v for k, v in document.items() if k != "provenance"}
        stable["provenance"] = {k: v for k, v in document["provenance"].items() if k != "retrieved_at"}
        digest = hashlib.sha256(json.dumps(stable, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
        tx.execute("UPDATE candidates SET payload_json=?, payload_hash=?, state_json=? WHERE candidate_id=?",
                   (json.dumps(document, ensure_ascii=False), digest, json.dumps(old.get("extraction") or {}, ensure_ascii=False), row["candidate_id"]))
        tx.execute("UPDATE processing_work SET done_hash=? WHERE candidate_id=? AND done_hash=?", (digest, row["candidate_id"], row["payload_hash"]))
        tx.execute("UPDATE processing_work SET last_fail_hash=? WHERE candidate_id=? AND last_fail_hash=?", (digest, row["candidate_id"], row["payload_hash"]))
        tx.execute("UPDATE candidate_media SET payload_hash=? WHERE candidate_id=? AND payload_hash=?", (digest, row["candidate_id"], row["payload_hash"]))


MIGRATIONS = (
    (1, "sources, pages, source attribution, contents, fetches, candidates, evidence, jobs, change log, consumers", """
CREATE TABLE sources (
  source_id      INTEGER PRIMARY KEY,
  source_key     TEXT NOT NULL UNIQUE CHECK (length(source_key) BETWEEN 1 AND 80),
  adapter        TEXT NOT NULL,
  name           TEXT NOT NULL,
  base_url       TEXT NOT NULL,
  options_json   TEXT NOT NULL DEFAULT '{}',
  enabled        INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
  interval_s     INTEGER NOT NULL DEFAULT 3600 CHECK (interval_s >= 60),
  last_success_at INTEGER,
  last_attempt_at INTEGER,
  created_at     INTEGER NOT NULL,
  updated_at     INTEGER NOT NULL
);

CREATE TABLE contents (
  content_hash TEXT PRIMARY KEY CHECK (length(content_hash) = 64),
  media_type   TEXT NOT NULL,
  bytes        INTEGER NOT NULL CHECK (bytes >= 0 AND bytes <= 2000000),
  body         BLOB NOT NULL,
  stored_at    INTEGER NOT NULL
);

CREATE TABLE pages (
  page_id      INTEGER PRIMARY KEY,
  url_key      TEXT NOT NULL UNIQUE CHECK (length(url_key) = 32),
  url          TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'not_fetched'
               CHECK (status IN ('not_fetched', 'ok', 'http_error', 'blocked', 'too_large', 'unsupported', 'network')),
  content_hash TEXT REFERENCES contents(content_hash),
  media_type   TEXT,
  bytes        INTEGER,
  final_url    TEXT,
  fingerprint  TEXT,                      -- hash of the page's readable text: unchanged text means unchanged content
  first_seen_at INTEGER NOT NULL,
  fetched_at   INTEGER,
  changed_at   INTEGER,
  CHECK (status <> 'ok' OR content_hash IS NOT NULL)
);

CREATE TABLE source_pages (
  source_id    INTEGER NOT NULL REFERENCES sources(source_id) ON DELETE CASCADE,
  page_id      INTEGER NOT NULL REFERENCES pages(page_id) ON DELETE CASCADE,
  role         TEXT NOT NULL CHECK (role IN ('listing', 'detail', 'event', 'feed', 'document', 'other')),
  first_seen_at INTEGER NOT NULL,
  last_seen_at INTEGER NOT NULL,
  PRIMARY KEY (source_id, page_id, role)
);
CREATE INDEX source_pages_page ON source_pages(page_id);

CREATE TABLE fetches (
  fetch_id     INTEGER PRIMARY KEY,
  page_id      INTEGER NOT NULL REFERENCES pages(page_id) ON DELETE CASCADE,
  job_id       INTEGER REFERENCES jobs(job_id) ON DELETE SET NULL,
  fetched_at   INTEGER NOT NULL,
  outcome      TEXT NOT NULL CHECK (outcome IN ('ok', 'http_error', 'blocked', 'too_large', 'unsupported', 'network')),
  http_status  INTEGER,
  bytes        INTEGER,
  content_hash TEXT,
  error        TEXT
);
CREATE INDEX fetches_page_time ON fetches(page_id, fetched_at);

CREATE TABLE candidates (
  candidate_id  INTEGER PRIMARY KEY,
  candidate_key TEXT NOT NULL UNIQUE CHECK (length(candidate_key) BETWEEN 3 AND 300),
  source_id     INTEGER NOT NULL REFERENCES sources(source_id) ON DELETE CASCADE,
  page_id       INTEGER REFERENCES pages(page_id) ON DELETE SET NULL,
  method        TEXT NOT NULL,
  extractor     TEXT NOT NULL,
  title         TEXT,
  starts_at     INTEGER,
  payload_json  TEXT NOT NULL,
  payload_hash  TEXT NOT NULL CHECK (length(payload_hash) = 64),
  first_seen_at INTEGER NOT NULL,
  last_seen_at  INTEGER NOT NULL,
  updated_at    INTEGER NOT NULL
);
CREATE INDEX candidates_source_start ON candidates(source_id, starts_at);
CREATE INDEX candidates_start ON candidates(starts_at);

CREATE TABLE evidence (
  evidence_id  INTEGER PRIMARY KEY,
  candidate_id INTEGER NOT NULL REFERENCES candidates(candidate_id) ON DELETE CASCADE,
  field        TEXT NOT NULL,
  kind         TEXT NOT NULL CHECK (kind IN ('extracted', 'inferred', 'recorded')),
  quote        TEXT CHECK (quote IS NULL OR length(quote) <= 400),
  page_id      INTEGER REFERENCES pages(page_id) ON DELETE SET NULL,
  content_hash TEXT REFERENCES contents(content_hash),
  note         TEXT
);
CREATE INDEX evidence_candidate ON evidence(candidate_id);
CREATE INDEX evidence_content ON evidence(content_hash);

CREATE TABLE jobs (
  job_id       INTEGER PRIMARY KEY,
  job_key      TEXT NOT NULL UNIQUE,
  kind         TEXT NOT NULL CHECK (kind IN ('crawl_source')),
  source_id    INTEGER NOT NULL REFERENCES sources(source_id) ON DELETE CASCADE,
  priority     INTEGER NOT NULL DEFAULT 50 CHECK (priority BETWEEN 0 AND 100),
  state        TEXT NOT NULL CHECK (state IN ('ready', 'leased', 'done', 'failed')),
  next_run_at  INTEGER NOT NULL,
  attempts     INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  max_attempts INTEGER NOT NULL DEFAULT 4 CHECK (max_attempts >= 1),
  lease_owner  TEXT,
  lease_token  TEXT,
  lease_until  INTEGER,
  last_error   TEXT,
  runs         INTEGER NOT NULL DEFAULT 0,
  created_at   INTEGER NOT NULL,
  updated_at   INTEGER NOT NULL,
  CHECK ((state = 'leased') = (lease_token IS NOT NULL AND lease_until IS NOT NULL))
);
CREATE INDEX jobs_claim ON jobs(next_run_at, priority DESC, job_id);

CREATE TABLE changes (
  seq          INTEGER PRIMARY KEY AUTOINCREMENT,
  entity       TEXT NOT NULL CHECK (entity IN ('candidate', 'page')),
  entity_key   TEXT NOT NULL,
  op           TEXT NOT NULL CHECK (op IN ('upsert', 'delete')),
  payload_hash TEXT NOT NULL,
  created_at   INTEGER NOT NULL
);
CREATE INDEX changes_entity ON changes(entity, entity_key);

CREATE TABLE consumers (
  consumer     TEXT PRIMARY KEY CHECK (length(consumer) BETWEEN 1 AND 100),
  acked_seq    INTEGER NOT NULL DEFAULT 0 CHECK (acked_seq >= 0),
  acked_at     INTEGER NOT NULL
);

CREATE TABLE store_meta (
  key          TEXT PRIMARY KEY,
  value        TEXT NOT NULL
);
INSERT INTO store_meta (key, value) VALUES ('pruned_through_seq', '0');
"""),
    (2, "areas, area/source relation, control settings, typed change log, quiet-run pacing", """
ALTER TABLE sources ADD COLUMN quiet_runs INTEGER NOT NULL DEFAULT 0;

CREATE TABLE areas (
  area_id      INTEGER PRIMARY KEY,
  area_key     TEXT NOT NULL UNIQUE CHECK (length(area_key) BETWEEN 8 AND 40),
  request_id   TEXT UNIQUE CHECK (request_id IS NULL OR length(request_id) BETWEEN 1 AND 100),
  name         TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
  latitude     REAL NOT NULL CHECK (latitude BETWEEN -90 AND 90),
  longitude    REAL NOT NULL CHECK (longitude BETWEEN -180 AND 180),
  radius       REAL NOT NULL CHECK (radius > 0 AND radius <= 500),
  unit         TEXT NOT NULL CHECK (unit IN ('mi', 'km')),
  enabled      INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
  intensity    TEXT NOT NULL DEFAULT 'normal' CHECK (intensity IN ('low', 'normal', 'high')),
  revision     INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
  created_at   INTEGER NOT NULL,
  updated_at   INTEGER NOT NULL
);

CREATE TABLE area_sources (
  area_id      INTEGER NOT NULL REFERENCES areas(area_id) ON DELETE CASCADE,
  source_id    INTEGER NOT NULL REFERENCES sources(source_id) ON DELETE CASCADE,
  added_at     INTEGER NOT NULL,
  PRIMARY KEY (area_id, source_id)
);
CREATE INDEX area_sources_source ON area_sources(source_id);

CREATE TABLE settings (
  key          TEXT PRIMARY KEY,
  value        TEXT NOT NULL,
  updated_at   INTEGER NOT NULL
);
INSERT INTO settings (key, value, updated_at) VALUES ('mode', 'automatic', 0), ('intensity', 'normal', 0), ('paused', '0', 0),
                                                     ('revision', '1', 0), ('scheduler_seen_seq', '0', 0);

INSERT INTO store_meta (key, value) VALUES ('seq_before_2', COALESCE((SELECT CAST(seq AS TEXT) FROM sqlite_sequence WHERE name = 'changes'), '0'));
CREATE TABLE changes_new (
  seq          INTEGER PRIMARY KEY AUTOINCREMENT,
  entity       TEXT NOT NULL CHECK (entity IN ('candidate', 'page', 'area', 'settings', 'discovery')),
  entity_key   TEXT NOT NULL,
  op           TEXT NOT NULL CHECK (op IN ('upsert', 'delete')),
  kind         TEXT NOT NULL DEFAULT '',
  payload_hash TEXT NOT NULL,
  created_at   INTEGER NOT NULL
);
INSERT INTO changes_new (seq, entity, entity_key, op, kind, payload_hash, created_at)
  SELECT seq, entity, entity_key, op, entity || '.changed', payload_hash, created_at FROM changes;
DROP TABLE changes;
ALTER TABLE changes_new RENAME TO changes;
CREATE INDEX changes_entity ON changes(entity, entity_key);
DELETE FROM sqlite_sequence WHERE name = 'changes';
INSERT INTO sqlite_sequence (name, seq) VALUES ('changes', MAX((SELECT COALESCE(MAX(seq), 0) FROM changes),
                                                              (SELECT CAST(value AS INTEGER) FROM store_meta WHERE key = 'seq_before_2')));
DELETE FROM store_meta WHERE key = 'seq_before_2';
"""),
    (3, "normalized events, extraction results and revisions, event/candidate relation, extraction work", """
CREATE TABLE events (
  event_id     INTEGER PRIMARY KEY,
  event_key    TEXT NOT NULL UNIQUE CHECK (length(event_key) BETWEEN 8 AND 60),
  match_key    TEXT,                       -- hash of title, start and address: only set when an address is known; links duplicate candidates
  title        TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 300),
  description  TEXT,
  starts_at    INTEGER NOT NULL,
  ends_at      INTEGER,
  timezone     TEXT,
  venue_name   TEXT,
  location_text TEXT,                      -- a written description of where, as the source gave it; not a venue name and not geocoded
  address      TEXT,
  location_status TEXT NOT NULL CHECK (location_status IN ('address', 'venue', 'description_only', 'unknown')),
  source_url   TEXT,
  status       TEXT NOT NULL CHECK (status IN ('complete', 'partial')),
  missing_json TEXT NOT NULL DEFAULT '[]',
  revision     INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
  extraction_id INTEGER,                   -- the extraction that produced the current values
  first_extracted_at INTEGER NOT NULL,
  updated_at   INTEGER NOT NULL
);
CREATE INDEX events_start ON events(starts_at, event_id);
CREATE INDEX events_match ON events(match_key) WHERE match_key IS NOT NULL;

CREATE TABLE event_candidates (
  candidate_id INTEGER PRIMARY KEY REFERENCES candidates(candidate_id) ON DELETE CASCADE,
  event_id     INTEGER NOT NULL REFERENCES events(event_id) ON DELETE CASCADE,
  role         TEXT NOT NULL CHECK (role IN ('primary', 'duplicate')),
  linked_at    INTEGER NOT NULL
);
CREATE INDEX event_candidates_event ON event_candidates(event_id);

CREATE TABLE extractions (
  extraction_id INTEGER PRIMARY KEY,
  candidate_id INTEGER NOT NULL REFERENCES candidates(candidate_id) ON DELETE CASCADE,
  event_id     INTEGER REFERENCES events(event_id) ON DELETE SET NULL,
  candidate_payload_hash TEXT NOT NULL,    -- the source revision this result was made from
  extractor    TEXT NOT NULL,
  version      INTEGER NOT NULL CHECK (version >= 1),
  status       TEXT NOT NULL CHECK (status IN ('complete', 'partial', 'failed')),
  failure_reason TEXT,
  result_json  TEXT NOT NULL,
  result_hash  TEXT NOT NULL,
  worker       TEXT,
  created_at   INTEGER NOT NULL,
  UNIQUE (candidate_id, candidate_payload_hash, extractor, version, result_hash)
);
CREATE INDEX extractions_candidate ON extractions(candidate_id, extraction_id);

CREATE TABLE event_revisions (
  event_id     INTEGER NOT NULL REFERENCES events(event_id) ON DELETE CASCADE,
  revision     INTEGER NOT NULL,
  extraction_id INTEGER NOT NULL REFERENCES extractions(extraction_id) ON DELETE CASCADE,
  changed_json TEXT NOT NULL,              -- {"field": [old, new]}; empty for the first revision
  created_at   INTEGER NOT NULL,
  PRIMARY KEY (event_id, revision)
);

CREATE TABLE processing_work (
  candidate_id INTEGER NOT NULL REFERENCES candidates(candidate_id) ON DELETE CASCADE,
  extractor    TEXT NOT NULL,
  state        TEXT NOT NULL CHECK (state IN ('ready', 'leased', 'done', 'failed')),
  done_hash    TEXT,
  done_version INTEGER NOT NULL DEFAULT 0,
  claimed_hash TEXT,
  attempts     INTEGER NOT NULL DEFAULT 0,
  next_attempt_at INTEGER NOT NULL DEFAULT 0,
  last_fail_hash TEXT,
  fail_version INTEGER NOT NULL DEFAULT 0,
  lease_owner  TEXT,
  lease_token  TEXT,
  lease_until  INTEGER,
  last_error   TEXT,
  updated_at   INTEGER NOT NULL,
  PRIMARY KEY (candidate_id, extractor),
  CHECK ((state = 'leased') = (lease_token IS NOT NULL AND lease_until IS NOT NULL))
);

INSERT INTO store_meta (key, value) VALUES ('seq_before_3', COALESCE((SELECT CAST(seq AS TEXT) FROM sqlite_sequence WHERE name = 'changes'), '0'));
CREATE TABLE changes_new (
  seq          INTEGER PRIMARY KEY AUTOINCREMENT,
  entity       TEXT NOT NULL CHECK (entity IN ('candidate', 'page', 'area', 'settings', 'discovery', 'event')),
  entity_key   TEXT NOT NULL,
  op           TEXT NOT NULL CHECK (op IN ('upsert', 'delete')),
  kind         TEXT NOT NULL DEFAULT '',
  payload_hash TEXT NOT NULL,
  created_at   INTEGER NOT NULL
);
INSERT INTO changes_new (seq, entity, entity_key, op, kind, payload_hash, created_at) SELECT seq, entity, entity_key, op, kind, payload_hash, created_at FROM changes;
DROP TABLE changes;
ALTER TABLE changes_new RENAME TO changes;
CREATE INDEX changes_entity ON changes(entity, entity_key);
DELETE FROM sqlite_sequence WHERE name = 'changes';
INSERT INTO sqlite_sequence (name, seq) VALUES ('changes', MAX((SELECT COALESCE(MAX(seq), 0) FROM changes),
                                                              (SELECT CAST(value AS INTEGER) FROM store_meta WHERE key = 'seq_before_3')));
DELETE FROM store_meta WHERE key = 'seq_before_3';
"""),
    (4, "media enrichment: resolved places and photographs, cached file metadata, per-candidate media", """
CREATE TABLE media_records (
  key          TEXT PRIMARY KEY CHECK (length(key) BETWEEN 3 AND 80),
  ok           INTEGER NOT NULL CHECK (ok IN (0, 1)),
  at           INTEGER NOT NULL,
  record_json  TEXT NOT NULL                -- a verified place (map, venue photograph), a cached refusal, or a cached source photograph
);

CREATE TABLE media_files (
  name         TEXT PRIMARY KEY CHECK (length(name) BETWEEN 5 AND 64),
  media_type   TEXT NOT NULL,
  bytes        INTEGER NOT NULL CHECK (bytes BETWEEN 1 AND 2000000),
  stored_at    INTEGER NOT NULL              -- the bytes live in the managed media folder, named by content
);

CREATE TABLE candidate_media (
  candidate_id INTEGER PRIMARY KEY REFERENCES candidates(candidate_id) ON DELETE CASCADE,
  payload_hash TEXT NOT NULL,                -- the candidate revision this was made for
  media_json   TEXT NOT NULL,                -- {"image", "photo", "map"}: what the card may show, with credits
  illustration_json TEXT,                    -- the category illustration decision
  complete     INTEGER NOT NULL CHECK (complete IN (0, 1)),   -- 1 once every lookup for this candidate has finished
  updated_at   INTEGER NOT NULL
);
CREATE INDEX candidate_media_open ON candidate_media(complete, updated_at);
"""),
    (5, "media enrichment version per candidate (maps gain a short place label)", """
ALTER TABLE candidate_media ADD COLUMN version INTEGER NOT NULL DEFAULT 1;
"""),
    (6, "source leads: pages a source-acquisition adapter considered, where it found them, and why they were accepted or rejected", """
CREATE TABLE source_leads (
  lead_id       INTEGER PRIMARY KEY,
  source_id     INTEGER NOT NULL REFERENCES sources(source_id) ON DELETE CASCADE,
  url_key       TEXT NOT NULL,
  url           TEXT NOT NULL,
  host          TEXT NOT NULL,
  kind          TEXT NOT NULL,
  decision      TEXT NOT NULL CHECK (decision IN ('accepted', 'rejected', 'pending')),
  reasons_json  TEXT NOT NULL DEFAULT '[]',
  score         INTEGER NOT NULL DEFAULT 0,
  found_on      TEXT,
  link_text     TEXT,
  events        INTEGER NOT NULL DEFAULT 0,
  fetched       INTEGER NOT NULL DEFAULT 0 CHECK (fetched IN (0, 1)),
  first_seen_at INTEGER NOT NULL,
  last_seen_at  INTEGER NOT NULL,
  UNIQUE (source_id, url_key)
);
CREATE INDEX source_leads_by_decision ON source_leads (source_id, decision);
"""),
    (7, "candidates are stored as version-1 standard records; the producer's working state is kept apart in state_json", _standard_candidates),
    (8, "event matching uses event facts and stable event keys, not generated match keys", """
DROP INDEX events_match;
ALTER TABLE events DROP COLUMN match_key;
CREATE INDEX events_start_key ON events(starts_at, event_key);
"""),
    (9, "database-backed producer configuration with revision-safe source changes", """
ALTER TABLE sources ADD COLUMN revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1);
INSERT INTO store_meta (key, value) VALUES ('seq_before_sources_migration', COALESCE((SELECT CAST(seq AS TEXT) FROM sqlite_sequence WHERE name = 'changes'), '0'));
CREATE TABLE changes_new (
  seq          INTEGER PRIMARY KEY AUTOINCREMENT,
  entity       TEXT NOT NULL CHECK (entity IN ('candidate', 'page', 'area', 'settings', 'discovery', 'event', 'source')),
  entity_key   TEXT NOT NULL,
  op           TEXT NOT NULL CHECK (op IN ('upsert', 'delete')),
  kind         TEXT NOT NULL DEFAULT '',
  payload_hash TEXT NOT NULL,
  created_at   INTEGER NOT NULL
);
INSERT INTO changes_new (seq, entity, entity_key, op, kind, payload_hash, created_at) SELECT seq, entity, entity_key, op, kind, payload_hash, created_at FROM changes;
DROP TABLE changes;
ALTER TABLE changes_new RENAME TO changes;
CREATE INDEX changes_entity ON changes(entity, entity_key);
DELETE FROM sqlite_sequence WHERE name = 'changes';
INSERT INTO sqlite_sequence (name, seq) VALUES ('changes', MAX((SELECT COALESCE(MAX(seq), 0) FROM changes),
                                                              (SELECT CAST(value AS INTEGER) FROM store_meta WHERE key = 'seq_before_sources_migration')));
DELETE FROM store_meta WHERE key = 'seq_before_sources_migration';
"""),
    (10, "trashcan: events an administrator deleted, kept as records (no picture bytes) that stop rediscovery from bringing them back", """
CREATE TABLE trashcan (
  trash_id      INTEGER PRIMARY KEY,
  event_key     TEXT NOT NULL UNIQUE CHECK (length(event_key) BETWEEN 8 AND 60),
  title         TEXT NOT NULL,
  starts_at     INTEGER NOT NULL,
  ends_at       INTEGER,
  reason        TEXT NOT NULL CHECK (reason IN ('duplicate', 'not_relevant', 'incorrect', 'expired_or_canceled', 'advertisement', 'other')),
  record_json   TEXT NOT NULL,              -- the deleted event, its candidates' standard records and its revisions
  match_json    TEXT NOT NULL,              -- normalized facts rediscovery is compared with: sources, candidate keys, starts, titles, event pages, places
  deleted_at    INTEGER NOT NULL,
  suppressed_count INTEGER NOT NULL DEFAULT 0 CHECK (suppressed_count >= 0),
  last_suppressed_at INTEGER
);

CREATE TABLE trashcan_occurrences (
  trash_id      INTEGER NOT NULL REFERENCES trashcan(trash_id) ON DELETE CASCADE,
  source_key    TEXT NOT NULL,              -- the immutable source key, so a record outlives a source's rename
  starts_at     INTEGER NOT NULL,
  PRIMARY KEY (trash_id, source_key, starts_at)
);
CREATE INDEX trashcan_occurrences_lookup ON trashcan_occurrences(source_key, starts_at);
"""),
)

LATEST = MIGRATIONS[-1][0]
