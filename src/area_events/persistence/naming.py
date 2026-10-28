"""A producer's display name is mutable; its source_key is its identity. This module settles the stored COPIES of the display name when it changes.

Copies kept in operational records: each candidate's `provenance.source_name` (inside the stored candidate document, so also inside its payload hash) and the image credit in
`candidate_media` (the credit of a source-supplied photograph is the source's name). On a rename they are reconciled in the same transaction as the rename, and the hash-keyed
bookkeeping that follows a candidate's content (media enrichment, processing work) is rebased onto the new hash, so a rename causes no re-extraction, no re-enrichment and no
new candidate or event. Evidence of what was extracted, event keys, candidate keys and associations are never touched; the name inside historical extraction results stays as
it was recorded.
"""
import json

from ..discovery import producer


def reconcile_display_name(tx, source_id, new_name, changes, now):
    """Make every operational copy of the source's display name equal `new_name`. Returns {"candidates": n, "media": n}: the rows that were stale."""
    counts = {"candidates": 0, "media": 0}
    for row in tx.execute("SELECT candidate_id, candidate_key, payload_json, payload_hash FROM candidates WHERE source_id=?", (source_id,)).fetchall():
        doc = json.loads(row["payload_json"])
        provenance = doc.get("provenance") if isinstance(doc, dict) else None
        if not isinstance(provenance, dict) or provenance.get("source_name") in (None, new_name):
            continue
        provenance["source_name"] = new_name
        digest = producer.canonical(doc)
        tx.execute("UPDATE candidates SET payload_json=?, payload_hash=? WHERE candidate_id=?", (json.dumps(doc, ensure_ascii=False), digest, row["candidate_id"]))
        if digest != row["payload_hash"]:
            for table, column in (("candidate_media", "payload_hash"), ("processing_work", "done_hash"), ("processing_work", "last_fail_hash")):
                tx.execute(f"UPDATE {table} SET {column}=? WHERE candidate_id=? AND {column}=?", (digest, row["candidate_id"], row["payload_hash"]))
            changes.append(tx, "candidate", row["candidate_key"], digest, now)
        counts["candidates"] += 1
    for row in tx.execute("""SELECT m.candidate_id, m.media_json FROM candidate_media m JOIN candidates c USING (candidate_id) WHERE c.source_id=?""", (source_id,)).fetchall():
        media = json.loads(row["media_json"]) if row["media_json"] else None
        image = media.get("image") if isinstance(media, dict) else None
        if isinstance(image, dict) and image.get("credit") not in (None, new_name):
            image["credit"] = new_name
            tx.execute("UPDATE candidate_media SET media_json=? WHERE candidate_id=?", (json.dumps(media, ensure_ascii=False), row["candidate_id"]))
            counts["media"] += 1
    return counts
