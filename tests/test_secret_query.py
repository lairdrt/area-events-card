"""Credentials sent as URL query parameters (`secret_query`): they reach the request line and nothing else.

Every test drives the real SafeFetcher and RecordingFetcher through an injected transport and resolver; nothing touches the network.
"""
import dataclasses
import inspect
import unittest
from urllib.parse import parse_qsl, urlsplit

from area_events.discovery.fetching import BudgetExceeded, PageFetch, RecordingFetcher
from area_events.discovery.safe import BlockedError, FetchError, HostPolicy, SafeFetcher, SecretQuery

KEY = "k3y-with spaces+plus&amp=equals/slash?é"
SECRET = {"api_key": KEY}
URL = "https://serpapi.com/search?engine=google_events&q=events+in+Austin"
JSON = {"Content-Type": "application/json"}


def resolver(host, port, type=None):
    return [(None, None, None, None, ("93.184.216.34", port))]


class Transport:
    """Stands in for safe.transport: records what would go on the wire and answers from a table (target prefix -> answer)."""

    def __init__(self, answers=None, robots=(404, {}, b"")):
        self.sent, self.answers, self.robots = [], answers or {}, robots

    def __call__(self, scheme, host, port, ip, target, headers, timeout, limit):
        self.sent.append({"scheme": scheme, "host": host, "target": target, "headers": dict(headers)})
        if target == "/robots.txt":
            return self.robots
        for prefix, answer in self.answers.items():
            if target.startswith(prefix):
                if isinstance(answer, Exception):
                    raise answer
                return answer(target) if callable(answer) else answer
        return 200, dict(JSON), b'{"events_results": []}'

    def targets(self):
        return [s["target"] for s in self.sent]


def fetcher(transport, interval=0, slept=None, state=None):
    return SafeFetcher(interval, resolver=resolver, send=transport, sleep=(slept.append if slept is not None else lambda s: None), state=state or HostPolicy())


def spellings(value=KEY):
    from urllib.parse import quote, quote_plus
    return {value, quote_plus(value), quote(value, safe="")}


def carries_secret(*items):
    """Does anything in these objects (strings, bytes, dataclasses, exceptions, nested lists) hold a spelling of KEY?"""
    def texts(item):
        if item is None:
            return []
        if isinstance(item, (bytes, bytearray)):
            return [item.decode("utf-8", errors="replace")]
        if dataclasses.is_dataclass(item):
            return [t for field in dataclasses.fields(item) for t in texts(getattr(item, field.name))]
        if isinstance(item, (list, tuple)):
            return [t for x in item for t in texts(x)]
        if isinstance(item, BaseException):
            return [str(item), repr(item.args)] + texts(item.__cause__)
        return [str(item)]
    found = [t for item in items for t in texts(item)]
    return any(form.lower() in t.lower() for t in found for form in spellings())


class OrdinaryRequests(unittest.TestCase):
    def test_without_a_secret_the_request_and_response_are_as_before(self):
        wire = Transport()
        response = fetcher(wire).fetch(URL)
        self.assertEqual(wire.targets(), ["/robots.txt", "/search?engine=google_events&q=events+in+Austin"])
        self.assertEqual(response.url, URL)
        self.assertEqual(fetcher(Transport()).get(URL), '{"events_results": []}')

    def test_the_interfaces_only_gained_an_optional_keyword(self):
        for method, before in ((SafeFetcher.get, ["self", "url", "check_robots"]), (SafeFetcher.fetch, ["self", "url", "check_robots", "binary", "timeout"]),
                               (RecordingFetcher.get, ["self", "url", "check_robots"]), (RecordingFetcher.fetch, ["self", "url", "check_robots", "binary", "timeout"])):
            parameters = inspect.signature(method).parameters
            self.assertEqual(list(parameters), before + ["secret_query"], method.__qualname__)
            self.assertIsNone(parameters["secret_query"].default)

    def test_test_clients_without_the_keyword_still_work_for_ordinary_calls(self):
        class OldClient:   # the shape of the existing test doubles: fetch(url) and fetch(url, binary=...)
            def __init__(self):
                self.calls = []

            def fetch(self, url, binary=None):
                self.calls.append((url, binary))
                return SafeFetcherResponse(url)

        client = OldClient()
        recording = RecordingFetcher(client, 5)
        self.assertEqual(recording.get("https://example.org/a"), "page")
        recording.fetch("https://example.org/b.jpg", binary=("image/jpeg",))
        self.assertEqual(client.calls, [("https://example.org/a", None), ("https://example.org/b.jpg", ("image/jpeg",))])


class SafeFetcherResponse:
    def __init__(self, url, text="page", body=b""):
        self.url, self.status, self.content_type, self.text, self.body = url, 200, "text/html", text, body


class TheCredentialReachesOnlyTheRequestLine(unittest.TestCase):
    def test_it_is_appended_url_encoded_after_the_public_query(self):
        wire = Transport()
        response = fetcher(wire).fetch(URL, secret_query=SECRET)
        target = wire.targets()[-1]
        self.assertTrue(target.startswith("/search?engine=google_events&q=events+in+Austin&api_key="), target)
        self.assertEqual(parse_qsl(urlsplit(target).query), [("engine", "google_events"), ("q", "events in Austin"), ("api_key", KEY)], "standard encoding, decoded intact")
        self.assertIn("api_key=k3y-with+spaces%2Bplus%26amp%3Dequals%2Fslash%3F%C3%A9", target)
        self.assertEqual(response.url, URL, "the response names the public address")
        self.assertFalse(carries_secret(response.url, response.headers))

    def test_an_address_without_a_query_gets_one(self):
        wire = Transport()
        fetcher(wire).fetch("https://api.example.org/v1/events", secret_query={"key": "abc123def456"})
        self.assertEqual(wire.targets()[-1], "/v1/events?key=abc123def456")

    def test_robots_txt_is_read_and_judged_for_the_public_address_and_can_refuse_before_the_key_is_sent(self):
        wire = Transport()
        fetcher(wire).fetch(URL, secret_query=SECRET)
        self.assertEqual(wire.targets()[0], "/robots.txt")
        self.assertFalse(carries_secret(wire.targets()[0]))
        refusing = Transport(robots=(200, {"Content-Type": "text/plain"}, b"User-agent: *\nDisallow: /search"))
        with self.assertRaises(BlockedError):
            fetcher(refusing).fetch(URL, secret_query=SECRET)
        self.assertEqual(refusing.targets(), ["/robots.txt"], "refused before anything carrying the key was sent")

    def test_only_https_carries_a_credential(self):
        wire = Transport()
        with self.assertRaises(BlockedError) as caught:
            fetcher(wire).fetch("http://serpapi.com/search?q=x", secret_query=SECRET)
        self.assertEqual(wire.sent, [], "nothing at all was sent")
        self.assertIn("https", str(caught.exception))

    def test_a_name_in_both_the_address_and_the_credential_is_refused_but_repeated_public_parameters_are_kept(self):
        wire = Transport()
        for url in ("https://serpapi.com/search?q=x&api_key=public", "https://serpapi.com/search?api_key=&q=x"):
            with self.assertRaises(BlockedError) as caught:
                fetcher(wire).fetch(url, secret_query=SECRET)
            self.assertIn("api_key", str(caught.exception))
            self.assertFalse(carries_secret(caught.exception))
        self.assertEqual(wire.sent, [])
        fetcher(wire).fetch("https://serpapi.com/search?q=a&q=b", secret_query=SECRET)
        self.assertEqual(parse_qsl(urlsplit(wire.targets()[-1]).query), [("q", "a"), ("q", "b"), ("api_key", KEY)])
        fetcher(wire).fetch("https://serpapi.com/search?API_KEY=x", secret_query=SECRET)   # names are exact, as servers read them
        self.assertTrue(wire.targets()[-1].endswith("&api_key=" + "k3y-with+spaces%2Bplus%26amp%3Dequals%2Fslash%3F%C3%A9"))

    def test_malformed_secrets_are_refused_before_any_request_and_without_quoting_them(self):
        wire = Transport()
        for bad in ({}, [("api_key", KEY)], KEY, {"api_key": 12345}, {"api_key": ""}, {"": KEY}, {7: KEY}, {"api_key": None}, {"api_key": [KEY]}):
            with self.subTest(bad=type(bad).__name__):
                with self.assertRaises(BlockedError) as caught:
                    fetcher(wire).fetch(URL, secret_query=bad)
                self.assertFalse(carries_secret(caught.exception))
        self.assertEqual(wire.sent, [])


class NothingCarriesTheCredentialBack(unittest.TestCase):
    def test_a_redirect_is_not_followed_and_names_no_credential(self):
        for status in (301, 302, 303, 307, 308):
            wire = Transport({"/search": (status, {"Location": "https://elsewhere.example/collect"}, b"")})
            with self.assertRaises(BlockedError) as caught:
                fetcher(wire).fetch(URL, secret_query=SECRET)
            self.assertEqual([s["host"] for s in wire.sent], ["serpapi.com", "serpapi.com"], "robots.txt, then the one request: nothing went to the new address")
            self.assertIn("redirect", str(caught.exception))
            self.assertFalse(carries_secret(caught.exception))

    def test_a_redirect_whose_location_repeats_the_key_is_still_only_refused(self):
        wire = Transport({"/search": (302, {"Location": "/search?api_key=" + KEY}, b"")})
        with self.assertRaises(BlockedError) as caught:
            fetcher(wire).fetch(URL, secret_query=SECRET)
        self.assertFalse(carries_secret(caught.exception))

    def test_http_errors_name_the_host_only(self):
        for status, kind in ((500, FetchError), (404, FetchError), (401, BlockedError), (403, BlockedError), (429, BlockedError)):
            with self.subTest(status=status):
                wire = Transport({"/search": (status, dict(JSON), b'{"error": "Invalid API key."}')})
                with self.assertRaises(kind) as caught:
                    fetcher(wire).fetch(URL, secret_query=SECRET)
                self.assertFalse(carries_secret(caught.exception))
                self.assertIn("serpapi.com", str(caught.exception))

    def test_a_transport_error_quoting_the_request_line_is_scrubbed_and_unchained(self):
        def failing(target):
            raise FetchError(f"serpapi.com: cannot send GET {target}")
        wire = Transport({"/search": failing})
        with self.assertRaises(FetchError) as caught:
            fetcher(wire).fetch(URL, secret_query=SECRET)
        self.assertFalse(carries_secret(caught.exception))
        self.assertIn("[credential]", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__, "a logged traceback does not show the original message")

    def test_an_unexpected_error_inside_the_request_becomes_a_plain_failure(self):
        def broken(target):
            raise ValueError(f"bad target {target}")
        wire = Transport({"/search": broken})
        with self.assertRaises(FetchError) as caught:
            fetcher(wire).fetch(URL, secret_query=SECRET)
        self.assertEqual(str(caught.exception), "serpapi.com: the authenticated request failed")
        self.assertTrue(caught.exception.__suppress_context__)

    def test_a_response_that_repeats_the_key_is_refused_in_any_spelling_or_case(self):
        from urllib.parse import quote, quote_plus
        for echo in (KEY, quote_plus(KEY), quote(KEY, safe=""), KEY.upper()):
            with self.subTest(echo=echo):
                wire = Transport({"/search": (200, dict(JSON), ('{"search_parameters": {"api_key": "%s"}}' % echo).encode())})
                with self.assertRaises(BlockedError) as caught:
                    fetcher(wire).fetch(URL, secret_query=SECRET)
                self.assertIn("repeated the request's credential", str(caught.exception))
                self.assertFalse(carries_secret(caught.exception))
        header_echo = Transport({"/search": (200, {"Content-Type": "application/json", "X-Debug": "key=" + KEY}, b"{}")})
        with self.assertRaises(BlockedError):
            fetcher(header_echo).fetch(URL, secret_query=SECRET)


class TheRecordedPagesNeverHoldIt(unittest.TestCase):
    def test_a_recorded_page_holds_the_public_address_and_the_body_only(self):
        wire = Transport({"/search": (200, dict(JSON), b'{"events_results": [{"title": "Fair"}]}')})
        recording = RecordingFetcher(fetcher(wire), 5)
        text = recording.get(URL, secret_query=SECRET)
        self.assertEqual(text, '{"events_results": [{"title": "Fair"}]}')
        [page] = recording.fetches
        self.assertEqual((page.url, page.final_url, page.outcome), (URL, URL, "ok"))
        self.assertFalse(carries_secret(recording.fetches), "no field of the stored page: url, final_url, error, body")
        self.assertTrue(carries_secret(wire.targets()[-1]), "while the server did receive it")

    def test_failures_are_recorded_under_the_public_address_with_a_scrubbed_message(self):
        def failing(target):
            raise FetchError(f"serpapi.com: cannot send GET {target}")
        cases = [Transport({"/search": (500, dict(JSON), b"oops")}), Transport({"/search": (302, {"Location": "https://x.example/"}, b"")}),
                 Transport({"/search": (200, dict(JSON), ("echo " + KEY).encode())}), Transport({"/search": failing})]
        for wire in cases:
            recording = RecordingFetcher(fetcher(wire), 5)
            with self.assertRaises(FetchError) as caught:
                recording.fetch(URL, secret_query=SECRET)
            [page] = recording.fetches
            self.assertEqual((page.url, page.final_url, page.body), (URL, None, None))
            self.assertFalse(carries_secret(recording.fetches, caught.exception))
        refused = RecordingFetcher(fetcher(Transport()), 5)
        with self.assertRaises(BlockedError):
            refused.fetch("http://serpapi.com/search", secret_query=SECRET)
        self.assertEqual(refused.fetches[0].outcome, "blocked")
        self.assertFalse(carries_secret(refused.fetches))

    def test_whatever_client_is_injected_a_response_carrying_the_key_is_not_recorded(self):
        class Leaky:   # a client that does not keep the key to itself
            def fetch(self, url, binary=None, secret_query=None):
                return SafeFetcherResponse(f"{url}&api_key={secret_query['api_key']}", text="fine")

        class Echoing:
            def fetch(self, url, binary=None, secret_query=None):
                return SafeFetcherResponse(url, text="you sent " + secret_query["api_key"])

        for client in (Leaky(), Echoing()):
            recording = RecordingFetcher(client, 5)
            with self.assertRaises(BlockedError):
                recording.get(URL, secret_query=SECRET)
            self.assertEqual([(p.outcome, p.body, p.final_url) for p in recording.fetches], [("blocked", None, None)])
            self.assertFalse(carries_secret(recording.fetches))

    def test_a_client_that_takes_the_keyword_receives_the_secret_beside_the_public_address(self):
        seen = []

        class Modern:
            def fetch(self, url, binary=None, secret_query=None):
                seen.append((url, secret_query))
                return SafeFetcherResponse(url)

        RecordingFetcher(Modern(), 5).get(URL, secret_query=SECRET)
        self.assertEqual(seen, [(URL, SECRET)])


class TheUsualLimitsStillApply(unittest.TestCase):
    def test_the_request_budget_counts_authenticated_requests(self):
        recording = RecordingFetcher(fetcher(Transport()), 2)
        recording.get(URL, secret_query=SECRET)
        recording.get(URL, secret_query=SECRET)
        with self.assertRaises(BudgetExceeded):
            recording.get(URL, secret_query=SECRET)

    def test_requests_to_one_host_are_spaced_out(self):
        slept, clock = [], [100.0]
        wire = Transport()
        client = SafeFetcher(2.0, resolver=resolver, send=wire, clock=lambda: clock[0], sleep=slept.append, state=HostPolicy())
        client.fetch(URL, secret_query=SECRET)
        client.fetch(URL, secret_query=SECRET)
        self.assertTrue(slept and slept[-1] > 0, slept)

    def test_a_host_that_asked_us_to_slow_down_is_not_asked_again_with_the_key(self):
        state = HostPolicy()
        wire = Transport({"/search": (429, {"Retry-After": "120"}, b"")})
        with self.assertRaises(BlockedError):
            fetcher(wire, state=state).fetch(URL, secret_query=SECRET)
        count = len(wire.sent)
        with self.assertRaises(BlockedError):
            fetcher(wire, state=state).fetch(URL, secret_query=SECRET)
        self.assertEqual(len(wire.sent), count)

    def test_a_private_address_is_still_refused(self):
        wire = Transport()
        client = SafeFetcher(0, resolver=lambda host, port, type=None: [(None, None, None, None, ("10.0.0.5", port))], send=wire, sleep=lambda s: None, state=HostPolicy())
        with self.assertRaises(BlockedError):
            client.fetch(URL, secret_query=SECRET)
        self.assertEqual(wire.sent, [])

    def test_the_response_size_limit_is_passed_to_the_transport_unchanged(self):
        limits = []

        def send(scheme, host, port, ip, target, headers, timeout, limit):
            limits.append(limit)
            return 200, dict(JSON), b"{}"
        SafeFetcher(0, resolver=resolver, send=send, sleep=lambda s: None, state=HostPolicy()).fetch(URL, secret_query=SECRET)
        from area_events.discovery.safe import MAX_BYTES
        self.assertEqual(set(limits), {MAX_BYTES})


def recapitalized():
    """Every spelling of KEY (as given, + encoded, % encoded) in upper case, lower case and swapped case: what a server or library may echo."""
    return sorted({change(form) for form in spellings() for change in (str.upper, str.lower, str.swapcase)} - spellings())


class RedactionIgnoresCase(unittest.TestCase):
    """A credential written in another capitalization is still the credential: it is redacted wherever it is detected."""

    def test_safe_fetcher_errors_redact_the_credential_in_any_capitalization_and_encoding(self):
        for form in recapitalized():
            with self.subTest(form=form):
                def failing(target, form=form):
                    raise FetchError(f"serpapi.com: upstream rejected api_key={form} (sent {target.upper()})")
                with self.assertRaises(FetchError) as caught:
                    fetcher(Transport({"/search": failing})).fetch(URL, secret_query=SECRET)
                self.assertFalse(carries_secret(caught.exception), str(caught.exception))
                self.assertIn("[credential]", str(caught.exception))

    def test_recorded_failure_diagnostics_redact_the_credential_in_any_capitalization_and_encoding(self):
        for form in recapitalized():
            with self.subTest(form=form):
                def failing(target, form=form):
                    raise FetchError(f"serpapi.com: upstream rejected api_key={form}")
                recording = RecordingFetcher(fetcher(Transport({"/search": failing})), 5)
                with self.assertRaises(FetchError) as caught:
                    recording.fetch(URL, secret_query=SECRET)
                [page] = recording.fetches
                self.assertFalse(carries_secret(page.error, caught.exception), page.error)
                self.assertIn("[credential]", page.error)

        class Shouting:   # a client of its own whose error quotes the key in capitals: RecordingFetcher redacts it too
            def fetch(self, url, binary=None, secret_query=None):
                raise FetchError("rejected key " + secret_query["api_key"].upper())
        recording = RecordingFetcher(Shouting(), 5)
        with self.assertRaises(FetchError) as caught:
            recording.fetch(URL, secret_query=SECRET)
        self.assertFalse(carries_secret(recording.fetches, caught.exception), recording.fetches[0].error)

    def test_scrub_and_echoed_agree(self):
        secret = SecretQuery(URL, SECRET)
        for form in sorted(spellings()) + recapitalized():
            with self.subTest(form=form):
                text = f"before {form} after"
                self.assertTrue(secret.echoed(text))
                self.assertFalse(secret.echoed(secret.scrub(text)), secret.scrub(text))
                self.assertEqual(secret.scrub(text), "before [credential] after")

    def test_the_replacement_never_spells_the_credential(self):
        for value in ("credential", "CREDENTIAL", "[", "e", "ed"):   # values the placeholder itself contains
            with self.subTest(value=value):
                secret = SecretQuery(URL, {"api_key": value})
                scrubbed = secret.scrub(f"rejected {value.swapcase()} and {value}")
                self.assertFalse(secret.echoed(scrubbed), scrubbed)


class TheHelper(unittest.TestCase):
    def test_scrub_replaces_every_spelling(self):
        secret = SecretQuery(URL, SECRET)
        text = " | ".join(sorted(spellings()))
        self.assertNotIn(KEY, secret.scrub(text))
        self.assertFalse(carries_secret(secret.scrub(text)))
        self.assertTrue(secret.echoed(b"x " + KEY.encode()), "bytes")
        self.assertFalse(secret.echoed(None, b"", "nothing here"))

    def test_page_fetch_has_no_field_for_a_credential(self):
        self.assertEqual([f.name for f in dataclasses.fields(PageFetch)], ["url", "outcome", "final_url", "http_status", "media_type", "body", "error"])


if __name__ == "__main__":
    unittest.main()
