"""Canonical URL identity: one page, one row, however it was written or whichever source found it."""
import hashlib
import types
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

TRACKING = ("utm_", "fbclid", "gclid", "mc_cid", "mc_eid", "_ga")
DEFAULT_PORTS = types.MappingProxyType({"http": 80, "https": 443})


def canonical_url(url):
    """The canonical form: lower-case scheme and host, no default port, no fragment, no tracking parameters, the remaining query
    parameters in sorted order, percent-encoding normalised, and an empty path written "/". Raises ValueError for non-web addresses."""
    parts = urlsplit((url or "").strip())
    scheme = parts.scheme.lower()
    if scheme not in DEFAULT_PORTS or not parts.hostname:
        raise ValueError(f"not a web address: {url!r}")
    if parts.username or parts.password:
        raise ValueError("addresses with credentials are not stored")
    host = parts.hostname.lower().rstrip(".")
    port = parts.port
    netloc = host if port in (None, DEFAULT_PORTS[scheme]) else f"{host}:{port}"
    path = quote(unquote(parts.path or "/"), safe="/:@!$&'()*+,;=-._~") or "/"
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not k.lower().startswith(TRACKING)]
    return urlunsplit((scheme, netloc, path, urlencode(sorted(query)), ""))


def url_key(url):
    """The stable key of a URL: sha256 of its canonical form, 32 hex characters."""
    return hashlib.sha256(canonical_url(url).encode("utf-8")).hexdigest()[:32]
