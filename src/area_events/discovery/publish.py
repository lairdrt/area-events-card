"""Feed validation and atomic file writing."""
import os


class FeedError(Exception):
    pass


def validate_feed(document):
    """Raise FeedError unless the document is a usable, non-empty feed."""
    if not isinstance(document, dict) or not isinstance(document.get("events"), list):
        raise FeedError("feed has no 'events' list")
    if not document["events"]:
        raise FeedError("feed contains no events")
    if not document.get("generated_at"):
        raise FeedError("feed has no generated_at")
    for event in document["events"]:
        for field in ("id", "title", "start", "url"):
            if not isinstance(event, dict) or not event.get(field):
                raise FeedError(f"event missing '{field}': {event.get('id') if isinstance(event, dict) else event!r}")


def write_atomic(path, text):
    """Write text next to `path`, flush to disk, then replace `path` in one step."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    temporary = os.path.join(directory, f".{os.path.basename(path)}.{os.getpid()}.tmp")
    try:
        with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)
