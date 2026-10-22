"""The discovery database schema, as ordered migrations. Each migration is applied once, in its own transaction, and recorded
in `schema_migrations` and `PRAGMA user_version`. A migration is never edited after it has shipped; changes are new migrations.

Times are UTC epoch seconds. Integer ids are local to this database; the stable keys (`source_key`, `url_key`, `candidate_key`,
`content_hash`) are what leaves it.
"""

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
)

LATEST = MIGRATIONS[-1][0]
