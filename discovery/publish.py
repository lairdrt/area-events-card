"""Safe feed writing and publishing (validate, write beside target, atomic replace)."""
import hashlib
import json
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


def publish_feed(document, destination):
    """Validate, write atomically to `destination`, and read it back to confirm.

    The destination directory must already exist (the HA www folder is created
    by deploy.ps1). An invalid or empty feed never touches the destination.
    """
    validate_feed(document)
    if not os.path.isdir(os.path.dirname(os.path.abspath(destination))):
        raise FeedError(f"destination directory does not exist: {os.path.dirname(destination)}")
    text = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    write_atomic(destination, text)
    with open(destination, encoding="utf-8") as handle:
        written = handle.read()
    if hashlib.sha256(written.encode()).hexdigest() != hashlib.sha256(text.encode()).hexdigest():
        raise FeedError(f"read-back of {destination} does not match what was written")
    validate_feed(json.loads(written))
    return len(document["events"])
