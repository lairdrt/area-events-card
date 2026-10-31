"""Fetching untrusted, user-entered websites.

`SafeFetcher` is for addresses a person typed into the card, which must never be able to reach the local network:

* only http and https, on ports 80 and 443, with no credentials in the address;
* the host is resolved once, every address must be public, and the connection is made to that exact address
  (the name is not resolved again), so DNS rebinding cannot swap in an internal address;
* every redirect is followed by hand, up to a small limit, and each destination goes through the same checks;
* robots.txt is honoured per RFC 9309 (a missing file allows, an unreachable one forbids, Crawl-delay is respected);
* requests to one host are spaced out (state shared by all fetchers), responses are limited in size and time,
  compressed bodies are inflated with a limit, and only text-like documents are accepted.

A site that answers 401, 403 or 429 is reported as blocked and never retried with other tricks: there is no
login or CAPTCHA handling, and no JavaScript is run anywhere in this program.

An API that takes its key as a query parameter is asked with `secret_query={"api_key": key}` beside the public address (see
SecretQuery). The key is added only to the request target sent to the server: every address this module returns, records or
quotes in an error is the public one, the request is refused unless it is https, a redirect is not followed, and a response that
repeats the key is refused instead of returned.
"""
import http.client
import ipaddress
import re
import socket
import ssl
import threading
import time
import urllib.robotparser
import zlib
from collections.abc import Mapping
from urllib.parse import parse_qsl, quote, quote_plus, urlencode, urljoin, urlsplit

USER_AGENT = "area-events-card-poc/0.1 (personal Home Assistant project; +https://github.com/lairdrt/area-events-card)"


class FetchError(Exception):
    """A page could not be fetched or was refused."""

MAX_BYTES = 1_500_000
MAX_REDIRECTS = 4
TIMEOUT = 15
TOTAL_SECONDS = 30
MAX_CRAWL_DELAY = 10
ROBOTS_TTL = 3600
PORTS = {80: "http", 443: "https"}
REDIRECTS = (301, 302, 303, 307, 308)


class BlockedError(FetchError):
    """The site (or robots.txt, or a safety rule) refuses this request."""


class SecretQuery:
    """Query parameters that carry a credential, kept apart from the public address they are sent with.

    Checked on creation: a non-empty mapping of non-empty text names to non-empty text values, for an https address whose own query
    has none of those names (a name in both would leave which one the server reads to chance). `query` is the standard URL encoding
    (application/x-www-form-urlencoded) that is appended to the request target. Error messages name the rule, never a value."""

    def __init__(self, url, secret_query):
        if not isinstance(secret_query, Mapping) or not secret_query:
            raise BlockedError("secret query parameters must be a non-empty mapping of names to values")
        pairs = []
        for name, value in secret_query.items():
            if not isinstance(name, str) or not isinstance(value, str) or not name or not value:
                raise BlockedError("secret query parameter names and values must be non-empty text")
            pairs.append((name, value))
        parts = urlsplit(url)
        if parts.scheme != "https":
            raise BlockedError("a request carrying credentials is only sent over https")
        clash = sorted({name for name, _ in parse_qsl(parts.query, keep_blank_values=True)} & {name for name, _ in pairs})
        if clash:
            raise BlockedError(f"the address already has the query parameter {clash[0]!r} that a credential would set")
        self.query = urlencode(pairs)
        # every spelling of a value that could come back: as given, and URL-encoded either way
        forms = sorted({form for _, value in pairs for form in (value, quote_plus(value), quote(value, safe=""))}, key=len, reverse=True)
        # one case-insensitive pattern, longest spellings first, used by both scrub and echoed so that they always agree
        self._pattern = re.compile("|".join(re.escape(form) for form in forms), re.IGNORECASE)

    def scrub(self, text):
        """`text` with every spelling of every value replaced, ignoring case as `echoed` does. The placeholder is one that spells no
        value; if every candidate does, values are removed outright, again until none is left (each pass shortens the text)."""
        placeholder = next((p for p in ("[credential]", "[redacted]", "***", "###") if not self.echoed(p)), "")
        text = self._pattern.sub(placeholder, text)
        while self.echoed(text):
            text = self._pattern.sub("", text)
        return text

    def echoed(self, *data):
        """Does any of these texts or byte strings contain a value (in any spelling, ignoring case)?"""
        for item in data:
            if not item:
                continue
            haystack = item.decode("utf-8", errors="replace") if isinstance(item, (bytes, bytearray)) else str(item)
            if self._pattern.search(haystack):
                return True
        return False


class Response:
    def __init__(self, url, status, content_type, text, body=b"", headers=None):
        self.url, self.status, self.content_type, self.text, self.body = url, status, content_type, text, body
        self.headers = headers or {}


class HostPolicy:
    """Politeness towards each host, shared by every fetcher that is given the same instance: request spacing, the robots.txt
    cache, and backing off from a host that answered 429 or 503. The Discovery Service creates one and injects it into all of its
    fetchers for one Discovery Service instance."""

    def __init__(self, backoff_seconds=600, max_backoff_seconds=3600):
        self.lock = threading.Lock()
        self.last = {}
        self.robots = {}
        self.backoff = {}
        self.backoff_seconds, self.max_backoff_seconds = backoff_seconds, max_backoff_seconds

    def refused(self, host, now, retry_after=None):
        """Remember that `host` asked us to slow down (HTTP 429 or 503)."""
        try:
            wait = float(retry_after) if retry_after is not None else self.backoff_seconds
        except (TypeError, ValueError):
            wait = self.backoff_seconds
        with self.lock:
            self.backoff[host] = now + min(max(wait, 0.0), self.max_backoff_seconds)

    def backing_off(self, host, now):
        with self.lock:
            until = self.backoff.get(host)
        return until is not None and until > now


def public_addresses(host, port, resolver=socket.getaddrinfo):
    """The host's addresses, if (and only if) every one is a public internet address."""
    try:
        infos = resolver(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError) as error:
        raise FetchError(f"cannot resolve {host}: {error}") from error
    found = []
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not ip.is_global:
            raise BlockedError(f"{host} is not a public website address")
        found.append(str(ip))
    if not found:
        raise FetchError(f"cannot resolve {host}")
    return found


class _Connection:
    """Connects to a pinned address while the request still names the real host (Host header, TLS server name)."""

    def __init__(self, scheme, host, port, ip, timeout):
        if scheme == "https":
            self.conn = http.client.HTTPSConnection(host, port, timeout=timeout, context=ssl.create_default_context())
        else:
            self.conn = http.client.HTTPConnection(host, port, timeout=timeout)
        raw, ip_address = self.conn, ip

        def connect():
            sock = socket.create_connection((ip_address, port), timeout)
            raw.sock = raw._context.wrap_socket(sock, server_hostname=host) if scheme == "https" else sock

        self.conn.connect = connect


def _read_limited(response, limit, deadline, clock):
    body = bytearray()
    while True:
        chunk = response.read(65536)
        if not chunk:
            return bytes(body)
        body += chunk
        if len(body) > limit:
            raise FetchError("the response is too large")
        if clock() > deadline:
            raise FetchError("the site answered too slowly")


def _inflate(body, encoding, limit):
    if encoding in ("", "identity"):
        return body
    if encoding not in ("gzip", "deflate", "x-gzip"):
        raise FetchError(f"unsupported content encoding {encoding}")
    decoder = zlib.decompressobj(zlib.MAX_WBITS | 32)
    try:
        out = decoder.decompress(body, limit + 1)
    except zlib.error as error:
        raise FetchError("the response could not be decompressed") from error
    if len(out) > limit or decoder.unconsumed_tail:
        raise FetchError("the response is too large")
    return out


def transport(scheme, host, port, ip, target, headers, timeout, limit, clock=time.monotonic):
    """One GET to a pinned address: (status, headers, body). Redirect bodies are not read."""
    connection = _Connection(scheme, host, port, ip, timeout).conn
    try:
        connection.request("GET", target, headers=headers)
        response = connection.getresponse()
        if response.status in REDIRECTS:
            return response.status, response.headers, b""
        body = _read_limited(response, limit, clock() + max(TOTAL_SECONDS, timeout * 2), clock)
        return response.status, response.headers, _inflate(body, (response.headers.get("Content-Encoding") or "").lower(), limit)
    except (OSError, http.client.HTTPException) as error:
        raise FetchError(f"{host}: {error}") from error
    finally:
        connection.close()


def _decode(body, content_type):
    charset = "utf-8"
    for part in (content_type or "").split(";")[1:]:
        key, _, value = part.strip().partition("=")
        if key.lower() == "charset" and value.strip():
            charset = value.strip().strip('"\'')
    try:
        return body.decode(charset, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def _texty(content_type):
    kind = (content_type or "").split(";")[0].strip().lower()
    return not kind or kind.startswith("text/") or any(word in kind for word in ("json", "xml", "calendar", "javascript"))


class SafeFetcher:
    """`get(url)` returns text; `fetch(url)` also returns the final address and content type."""

    def __init__(self, interval=2.0, resolver=socket.getaddrinfo, send=transport, clock=time.monotonic, sleep=time.sleep, state=None):
        self.interval, self.resolver, self.send, self.clock, self.sleep = interval, resolver, send, clock, sleep
        self.state = state if state is not None else HostPolicy()

    def get(self, url, check_robots=True, secret_query=None):
        return self.fetch(url, check_robots, secret_query=secret_query).text

    def fetch(self, url, check_robots=True, binary=None, timeout=TIMEOUT, secret_query=None):
        """The response for `url`. `binary` is a tuple of accepted content types (for example ("image/jpeg",)) to fetch a
        non-text body, which is then in `response.body`; without it only text-like documents are accepted. `secret_query` holds
        credential parameters added only to the request sent (see SecretQuery); the response and every error name `url` alone."""
        secret = SecretQuery(url, secret_query) if secret_query is not None else None
        host = urlsplit(url).hostname
        if host and self.state.backing_off(host, self.clock()):
            raise BlockedError(f"{host} asked us to slow down; waiting before asking again")
        if secret is None:
            response = self._request(url, check_robots, timeout)
        else:
            try:
                response = self._request(url, check_robots, timeout, secret)
            except FetchError as error:   # the message may quote what the connection was asked for: only its scrubbed text goes on
                raise type(error)(secret.scrub(str(error))) from None
            except Exception:
                raise FetchError(f"{host}: the authenticated request failed") from None
        host = urlsplit(response.url).hostname
        if response.status in (429, 503):
            self.state.refused(host, self.clock(), response.headers.get("Retry-After"))
        if response.status in (401, 403, 429):
            raise BlockedError(f"{host} refused access (HTTP {response.status})")
        if response.status >= 400:
            raise FetchError(f"{host}: HTTP {response.status}")
        if binary:
            kind = (response.content_type or "").split(";")[0].strip().lower()
            if kind not in binary:
                raise FetchError(f"{host}: unexpected content type ({response.content_type})")
        elif not _texty(response.content_type):
            raise FetchError(f"{host}: not a text document ({response.content_type})")
        return response

    # --- internals ---------------------------------------------------------------------------------

    def _check(self, url):
        parts = urlsplit(url)
        try:
            port = parts.port or (443 if parts.scheme == "https" else 80)
        except ValueError as error:
            raise BlockedError("the address has an invalid port") from error
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise BlockedError("only http and https website addresses are read")
        if parts.username or parts.password:
            raise BlockedError("addresses with a user name or password are not read")
        if PORTS.get(port) != parts.scheme:
            raise BlockedError(f"port {port} is not read")
        return parts, port

    def _pace(self, host, delay):
        with self.state.lock:  # reserve the next slot for this host, then wait for it outside the lock
            now = self.clock()
            slot = max(now, self.state.last.get(host, float("-inf")) + max(self.interval, delay))
            self.state.last[host] = slot
        if slot > now:
            self.sleep(slot - now)

    def _request(self, url, check_robots, timeout=TIMEOUT, secret=None):
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            parts, port = self._check(current)
            ips = public_addresses(parts.hostname, port, self.resolver)
            delay = 0
            if check_robots:   # robots.txt is read, and judged, for the public address
                allowed, delay = self._robots(parts, port, ips[0])
                if not allowed:
                    raise BlockedError(f"robots.txt of {parts.hostname} does not allow this page")
            self._pace(parts.hostname, delay)
            target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
            if secret is not None:   # the only place the credential joins the address: the request line itself
                target += ("&" if parts.query else "?") + secret.query
            headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,application/xml,text/calendar,application/json;q=0.9,*/*;q=0.5",
                       "Accept-Encoding": "gzip, identity"}
            status, received, body = self.send(parts.scheme, parts.hostname, port, ips[0], target, headers, timeout, MAX_BYTES)
            if secret is not None:
                if status in REDIRECTS:   # never carried to another address
                    raise BlockedError(f"{parts.hostname}: an authenticated request was redirected (HTTP {status}); the redirect is not followed")
                if secret.echoed(body, *(str(value) for value in received.values())):
                    raise BlockedError(f"{parts.hostname}: the response repeated the request's credential; it is not kept")
            if status in REDIRECTS:
                location = received.get("Location")
                if not location:
                    raise FetchError(f"{parts.hostname}: redirect without a destination")
                current = urljoin(current, location)
                continue
            content_type = received.get("Content-Type") or ""
            return Response(current, status, content_type, _decode(body, content_type), body, {k: v for k, v in received.items() if k in ("Retry-After", "ETag", "Last-Modified")})
        raise FetchError(f"{urlsplit(url).hostname}: too many redirects")

    def _robots(self, parts, port, ip):
        """(allowed, crawl_delay) for this URL under the host's robots.txt (RFC 9309 semantics)."""
        origin = f"{parts.scheme}://{parts.netloc}"
        with self.state.lock:
            cached = self.state.robots.get(origin)
        if cached is None or self.clock() - cached[0] > ROBOTS_TTL:
            parser = urllib.robotparser.RobotFileParser()
            try:
                answer = self._request(f"{origin}/robots.txt", check_robots=False)
                if answer.status >= 500:
                    parser.parse(["User-agent: *", "Disallow: /"])  # unreachable: assume nothing is allowed
                elif answer.status >= 400:
                    parser.parse([])  # no robots.txt: everything is allowed
                else:
                    parser.parse(answer.text.splitlines())
            except BlockedError:
                raise
            except FetchError:
                parser.parse(["User-agent: *", "Disallow: /"])
            cached = (self.clock(), parser)
            with self.state.lock:
                self.state.robots[origin] = cached
        parser = cached[1]
        delay = parser.crawl_delay(USER_AGENT) or 0
        url = parts.path or "/"
        if parts.query:
            url += f"?{parts.query}"
        return parser.can_fetch(USER_AGENT, url), min(float(delay), MAX_CRAWL_DELAY)
