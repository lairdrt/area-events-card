"""A producer's display name is mutable; its source_key is its identity. This module settles the stored COPIES of the display name.

Copies kept in operational records: each candidate's `provenance.source_name` (inside the stored candidate document, so also inside its payload hash) and the image credit in
`candidate_media` (the credit of a source-supplied photograph is the source's name). `reconcile_display_name` makes them equal to the source's current name, and rebases the
hash-keyed bookkeeping that follows a candidate's content (media enrichment, processing work) onto the new hash, so that nothing is re-extracted or re-enriched and no
candidate or event is created. Evidence of what was extracted, event keys, candidate keys and associations are never touched; the name inside historical extraction results
stays as it was recorded.

It runs inside the transaction of a rename, and `DisplayNames` runs it on demand for sources that were renamed before it existed (a dry run first). Ownership is by
source_id: the sources.name of the owning row is the only authority, never a name found in a record.
"""
import json

from ..discovery import producer


def reconcile_display_name(tx, source_id, new_name, changes, now, dry_run=False):
    """Make every operational copy of the source's display name equal `new_name`. Returns what was (or, in a dry run, would be) stale:
    {"candidates": n, "image_credits": n, "bookkeeping_rebased": n, "stale_names": [names found]}."""
    counts = {"candidates": 0, "image_credits": 0, "bookkeeping_rebased": 0}
    found = set()
    for row in tx.execute("SELECT candidate_id, candidate_key, payload_json, payload_hash FROM candidates WHERE source_id=?", (source_id,)).fetchall():
        doc = json.loads(row["payload_json"])
        provenance = doc.get("provenance") if isinstance(doc, dict) else None
        if not isinstance(provenance, dict) or provenance.get("source_name") in (None, new_name):
            continue
        found.add(provenance["source_name"])
        provenance["source_name"] = new_name
        digest = producer.canonical(doc)
        if not dry_run:
            tx.execute("UPDATE candidates SET payload_json=?, payload_hash=? WHERE candidate_id=?", (json.dumps(doc, ensure_ascii=False), digest, row["candidate_id"]))
        if digest != row["payload_hash"]:
            for table, column in (("candidate_media", "payload_hash"), ("processing_work", "done_hash"), ("processing_work", "last_fail_hash")):
                if dry_run:
                    counts["bookkeeping_rebased"] += tx.execute(f"SELECT COUNT(*) FROM {table} WHERE candidate_id=? AND {column}=?", (row["candidate_id"], row["payload_hash"])).fetchone()[0]
                else:
                    counts["bookkeeping_rebased"] += tx.execute(f"UPDATE {table} SET {column}=? WHERE candidate_id=? AND {column}=?",
                                                                (digest, row["candidate_id"], row["payload_hash"])).rowcount
            if not dry_run:
                changes.append(tx, "candidate", row["candidate_key"], digest, now)
        counts["candidates"] += 1
    for row in tx.execute("""SELECT m.candidate_id, m.media_json FROM candidate_media m JOIN candidates c USING (candidate_id) WHERE c.source_id=?""", (source_id,)).fetchall():
        media = json.loads(row["media_json"]) if row["media_json"] else None
        image = media.get("image") if isinstance(media, dict) else None
        if isinstance(image, dict) and image.get("credit") not in (None, new_name):
            found.add(image["credit"])
            image["credit"] = new_name
            if not dry_run:
                tx.execute("UPDATE candidate_media SET media_json=? WHERE candidate_id=?", (json.dumps(media, ensure_ascii=False), row["candidate_id"]))
            counts["image_credits"] += 1
    return {**counts, "stale_names": sorted(found)}


class DisplayNames:
    """Maintenance for sources renamed before renames reconciled their records: a read-only report, and one transaction that applies it. Idempotent."""

    def __init__(self, store, changes, clock):
        self._store, self._changes, self._clock = store, changes, clock

    @staticmethod
    def _sources(connection, source_key):
        rows = connection.execute("SELECT source_id, source_key, name FROM sources" + (" WHERE source_key=?" if source_key else "") + " ORDER BY source_key",
                                  (source_key,) if source_key else ()).fetchall()
        if source_key and not rows:
            raise ValueError(f"source {source_key!r} does not exist")
        return rows

    def preview(self, source_key=None):
        """Read-only: per source, the stale copies of its display name and what applying would do."""
        with self._store.read() as connection:
            return [{"source_key": row["source_key"], "name": row["name"], **reconcile_display_name(connection, row["source_id"], row["name"], None, 0, dry_run=True)}
                    for row in self._sources(connection, source_key)]

    def apply(self, source_key=None):
        """Reconcile every stale copy to the current sources.name in ONE write transaction. A second run finds nothing and writes nothing."""
        now = int(self._clock())
        with self._store.write() as tx:
            return [{"source_key": row["source_key"], "name": row["name"], **reconcile_display_name(tx, row["source_id"], row["name"], self._changes, now)}
                    for row in self._sources(tx, source_key)]
