"""Producer for Google Events results through SerpApi (https://serpapi.com/google-events-api), with the account guard that keeps it on the free plan.

One producer type serves every SerpApi source; each source is its own search (`query`, `location`, the events' IANA `timezone`, and at most `max_pages` result pages,
each of which is one billable search). Every source shares ONE SerpApi account: one key (SERPAPI_API_KEY on the host, given to `SerpApiAccount` by the composition
root and never stored), one monthly allowance and one usage policy.

    SerpApiAccount.permit()   the only way to make a billable search. Under one lock shared by every SerpApi source of this process it reads the free Account API
                              (https://serpapi.com/account.json, which costs no search) and refuses unless the account is on a $0 plan, its numbers are complete,
                              and the search fits under the free-tier hard stop (always on: the last FREE_TIER_RESERVE free searches are never used) and, when
                              automatic pause is on, under the configured threshold. Searches this process made that the provider does not yet count are added.
                              A refusal raises UsageBlocked, a FetchError: the job is released and tried again later, nothing stored is touched, and a source's
                              manual enabled flag is never changed. A new billing cycle that the provider confirms lets the next attempt search again.

Both requests go through the shared SafeFetcher (the search through the job's RecordingFetcher), with the key as `secret_query` and the key's own field dropped from
the answer (the Account API reports it); the key anywhere else in an answer is refused as an echo. Nothing here persists the key or puts it in an address.
SerpApi's robots.txt (written for crawlers) disallows its own documented API endpoint, https://serpapi.com/search; that one authenticated request is made with
check_robots=False. The Account API, which robots.txt allows, is read with robots.txt honoured, as is every other request.

A search is permitted only when the account reports account_status "Active" (any case); a missing, empty or other status refuses it.

Facts are taken only as the result states them. Google gives a day such as "Nov 6, 7:00 PM" without a year: the start is the nearest upcoming occurrence of
that month and day (from the scan's local date in the source's time zone), recorded as inferred, never as stated. A stated year is always kept, a stated weekday
must agree, and every start must lie within the source's rolling look-ahead window (lookahead_days, default 90, 30 to 365; both ends included); a search that
names one year ("events in San Diego November 2026") accepts only events in that year. A result without a stated start time is set aside, and no time or end
is ever invented. Aggregated results are never treated as verified.

An event's url is its own page as Google lists it: the result's `link`, else a `ticket_info` link (information before tickets), each only when its address
names the event by its title or is a Facebook or Eventbrite event page. A search, map, SerpApi address or a page that does not name the event is never used.
When Google lists no such page, the producer asks the shared EventLinkFinder (event_links.py) once for the event's title, date, venue and town: what
it returns (a page the finder confirmed by reading it) meets the same rule, and is recorded in the evidence as found by search. When Google gave no
street address, the venue's street address that page states is used, recorded with where on the page it was read. Without a finder, or when it finds
nothing, the url stays empty and the address as Google gave it. A link never makes an event verified.

When an event still has no street address (Google often gives only 'Red Silo Brewing, Cookeville, TN'), the producer asks the shared venue resolver
(venues.py) for the venue in the event's town and state, through one scan view (a bounded number of new Maps searches per scan): a match's street address,
as the provider reports it, becomes the event's, recorded in the evidence as from Google Maps. A street address the event already has is kept and no
lookup is made. The event's identity, times, url and verification status never change. A definitive no_match or ambiguity withholds an event this source
has not submitted before (set aside with its reason); an event it already has stays as it was, so withholding never removes an existing event from the
feed. 'unavailable' (no key, a budget, the account or the provider) is never taken as a no_match: the event is kept as it is, and when an earlier scan
gave it a Google Maps address for the same venue, that address is kept.
"""
import hashlib
import json
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from urllib.parse import unquote, urlencode, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..event_links import event_page, names_event
from ..producer import EventCandidate, ProducerResult, Provenance, stated
from ..safe import FetchError
from ..webpage import STREET_LINE

SEARCH_URL = "https://serpapi.com/search"
ACCOUNT_URL = "https://serpapi.com/account.json"
KEY_FIELD = "api_key"
ACCOUNT_PRIVATE_FIELDS = ("api_key", "account_email", "account_id")   # dropped from the Account API's answer the moment it arrives
RESULTS_PER_PAGE = 10
MAX_PAGES = 3
DEFAULT_LOOKAHEAD_DAYS, MIN_LOOKAHEAD_DAYS, MAX_LOOKAHEAD_DAYS = 90, 30, 365
MAX_ROLLOVER_DAYS = 31       # the longest event accepted when its end's year is inferred as the year after its start's
FREE_TIER_RESERVE = 5        # the hard stop: the last five free searches are never used (searches made elsewhere, a provider count that lags)
DEFAULT_PAUSE_AT = 200
REPORT_MAX_AGE_S = 300       # how long the management view reuses an account reading; a search never does
_TIMEZONE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9._+-]*(?:/[A-Za-z0-9._+-]+)+$")


class UsageBlocked(FetchError):
    """No billable search is made: the reason says why (the account, the threshold, the hard stop or an unreadable account)."""

    def __init__(self, message, state):
        super().__init__(message)
        self.state = state


# --- the account -----------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class AccountUsage:
    """What the Account API reported (without the key, the e-mail address or the account id)."""
    plan_name: str
    monthly_price: float
    searches_per_month: int
    used: int                      # this_month_usage
    left: int                      # plan_searches_left
    renewal: Optional[str]         # plan_renewal_date as the provider gives it, when it does
    account_status: Optional[str]
    checked_at: int


def _count(payload, name):
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise UsageBlocked(f"the SerpApi account reading has no usable {name}; no search is made", "usage_unavailable")
    return value


def _price(value):
    if isinstance(value, bool):
        raise ValueError
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and re.fullmatch(r"\$?\s*\d+(?:\.\d+)?", value.strip()):
        return float(value.strip().lstrip("$").strip())
    raise ValueError


def read_account(payload, now):
    """An AccountUsage from the Account API's JSON, or UsageBlocked('usage_unavailable') when anything a decision needs is missing or unreadable."""
    if not isinstance(payload, dict):
        raise UsageBlocked("the SerpApi account reading is not an object; no search is made", "usage_unavailable")
    if payload.get("error"):
        raise UsageBlocked("the SerpApi Account API reported an error; no search is made", "usage_unavailable")
    try:
        price = _price(payload.get("plan_monthly_price"))
    except ValueError:
        raise UsageBlocked("the SerpApi account reading has no usable plan_monthly_price; no search is made", "usage_unavailable") from None
    renewal = payload.get("plan_renewal_date")
    status = payload.get("account_status")
    return AccountUsage(str(payload.get("plan_name") or ""), price, _count(payload, "searches_per_month"), _count(payload, "this_month_usage"),
                        _count(payload, "plan_searches_left"), renewal if isinstance(renewal, str) and renewal.strip() else None,
                        status if isinstance(status, str) else None, int(now))


def _renewal_day(text):
    try:
        return date.fromisoformat(text[:10]) if text else None
    except ValueError:
        return None


def decide(usage, auto_pause, pause_at, pending=0, today=None):
    """(state, reason) for one more billable search. `pending` counts searches this process made that the provider does not count yet.

    active                the search may be made
    blocked_free_tier     a paid plan, or the free allowance is down to the hard stop (always applies)
    paused_threshold      automatic pause is on and the account has used `pause_at` searches this cycle
    waiting_for_renewal   paused or stopped, the renewal date has come, and the provider does not yet report a new allowance
    usage_unavailable     the account does not report itself active"""
    status = (usage.account_status or "").strip()
    if not status:
        return "usage_unavailable", "the SerpApi account reading does not state an account status, so it is not known to be active"
    if status.lower() != "active":
        return "usage_unavailable", f"the SerpApi account status is '{status[:40]}', not Active"
    if usage.monthly_price != 0:
        return "blocked_free_tier", f"the SerpApi account is on a paid plan (${usage.monthly_price:g} a month); Area Events searches only on a free plan"
    used = usage.used + pending
    remaining = min(usage.left, usage.searches_per_month - usage.used) - pending
    state, reason = "active", None
    if remaining <= FREE_TIER_RESERVE:
        state, reason = "blocked_free_tier", (f"{max(remaining, 0)} free searches remain this cycle; the last {FREE_TIER_RESERVE} are never used")
    elif auto_pause and used >= pause_at:
        state, reason = "paused_threshold", f"{used} searches used this cycle; automatic pause stops scheduled searches at {pause_at}"
    renewal = _renewal_day(usage.renewal)
    if state != "active" and renewal is not None and today is not None and today >= renewal:
        return "waiting_for_renewal", f"{reason}; the allowance was due to renew on {renewal.isoformat()} and the provider does not report it yet"
    return state, reason


class SerpApiAccount:
    """The one SerpApi account of this process: its key (held in memory only), its usage, and the permit every billable search needs.

    `client` is the shared SafeFetcher; `policy()` returns the current SettingsRecord (serpapi_auto_pause, serpapi_pause_at)."""

    def __init__(self, client, api_key, policy, clock=time.time):
        self._client, self._key, self._policy, self._clock = client, api_key if isinstance(api_key, str) and api_key.strip() else None, policy, clock
        self._lock = threading.Lock()      # one search at a time across every SerpApi source of this process
        self._pending, self._seen_used = 0, None
        self._last, self._last_error = None, None

    @property
    def configured(self):
        return self._key is not None

    def secret(self):
        if self._key is None:
            raise UsageBlocked("no SerpApi key is configured (SERPAPI_API_KEY); no search is made", "usage_unavailable")
        return {KEY_FIELD: self._key}

    def check(self):
        """A fresh reading of the account from the free Account API (no search is used)."""
        secret = self.secret()
        try:
            text = self._client.fetch(ACCOUNT_URL, secret_query=secret, drop_fields=ACCOUNT_PRIVATE_FIELDS).text
            payload = json.loads(text)
        except UsageBlocked:
            raise
        except (FetchError, ValueError) as error:
            self._last_error = f"the SerpApi account could not be read ({type(error).__name__})"
            raise UsageBlocked(self._last_error + "; no search is made", "usage_unavailable") from None
        try:
            usage = read_account(payload, self._clock())
        except UsageBlocked as error:
            self._last_error = str(error)
            raise
        self._last, self._last_error = usage, None
        return usage

    def _policy_values(self):
        try:
            settings = self._policy()
            return bool(settings.serpapi_auto_pause), int(settings.serpapi_pause_at)
        except Exception:   # an unreadable policy is the safe default, not no policy
            return True, DEFAULT_PAUSE_AT

    def _today(self, usage):
        return datetime.fromtimestamp(usage.checked_at, timezone.utc).date()

    @contextmanager
    def permit(self):
        """Hold this to make ONE billable search. Raises UsageBlocked (and makes no search) unless a fresh account reading allows it."""
        with self._lock:
            usage = self.check()
            if self._seen_used is not None:
                if usage.used < self._seen_used:    # a new billing cycle: the provider's count started again
                    self._pending = 0
                else:                               # the provider now counts some of the searches made here
                    self._pending = max(0, self._pending - (usage.used - self._seen_used))
            self._seen_used = usage.used
            auto_pause, pause_at = self._policy_values()
            state, reason = decide(usage, auto_pause, pause_at, self._pending, self._today(usage))
            if state != "active":
                raise UsageBlocked(f"SerpApi search not made: {reason}", state)
            try:
                yield usage
            finally:
                self._pending += 1    # counted whether or not the search succeeded: once asked, it may have been billed

    def report(self, sources=()):
        """The management view (no key): the latest account reading (reread when older than REPORT_MAX_AGE_S), the policy, the account-wide
        state and each SerpApi source's effective state. `sources` are the configured SourceRecords."""
        auto_pause, pause_at = self._policy_values()
        error = None
        if self.configured and (self._last is None or self._clock() - self._last.checked_at > REPORT_MAX_AGE_S):
            try:
                self.check()
            except UsageBlocked as blocked:
                error = str(blocked)
        elif not self.configured:
            error = "no SerpApi key is configured (SERPAPI_API_KEY)"
        usage = self._last
        stale = usage is None or error is not None or self._clock() - usage.checked_at > REPORT_MAX_AGE_S
        if usage is None or error is not None:
            state, reason = "usage_unavailable", error or self._last_error or "the SerpApi account has not been read"
        else:
            state, reason = decide(usage, auto_pause, pause_at, self._pending, self._today(usage))
        account = None if usage is None else {
            "plan_name": usage.plan_name, "monthly_price": usage.monthly_price, "searches_per_month": usage.searches_per_month, "used": usage.used,
            "remaining": max(0, min(usage.left, usage.searches_per_month - usage.used)), "renewal": usage.renewal, "checked_at": usage.checked_at}
        per_source = {}
        for source in sources:
            if getattr(source, "adapter", None) != SerpApiGoogleEventsProducer.adapter:
                continue
            per_source[source.source_key] = ({"state": "manually_disabled", "reason": "disabled by an operator"} if not source.enabled else
                                             {"state": "active" if state == "active" else state, "reason": reason})
        return {"configured": self.configured, "account": account, "stale": stale, "error": error, "pending_searches": self._pending,
                "policy": {"auto_pause": auto_pause, "pause_at": pause_at, "free_tier_reserve": FREE_TIER_RESERVE},
                "state": state, "reason": reason, "resumes": usage.renewal if usage is not None and state != "active" else None, "sources": per_source}


# --- reading a result ------------------------------------------------------------------------------------------------

_MONTHS = {name: number for number, names in enumerate((("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"), ("may",), ("jun", "june"),
                                                         ("jul", "july"), ("aug", "august"), ("sep", "sept", "september"), ("oct", "october"),
                                                         ("nov", "november"), ("dec", "december")), start=1) for name in names}
_WEEKDAYS = {name: number for number, names in enumerate((("mon", "monday"), ("tue", "tues", "tuesday"), ("wed", "wednesday"), ("thu", "thur", "thurs", "thursday"),
                                                           ("fri", "friday"), ("sat", "saturday"), ("sun", "sunday"))) for name in names}
_DAY = re.compile(r"^(?:(?P<weekday>[A-Za-z]{3,9})\.?,?\s+)?(?P<month>[A-Za-z]{3,9})\.?\s+(?P<day>\d{1,2})(?:,?\s+(?P<year>\d{4}))?$")
_TIME = re.compile(r"^(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<meridiem>[AaPp]\.?[Mm]\.?)?$")
_DASH = re.compile(r"\s+[–—-]\s+|\s*[–—]\s*")


class Unusable(ValueError):
    """A result that does not state enough to be an event (the reason is recorded in the run's notes)."""


def _parse_day(text):
    match = _DAY.match(text.strip())
    if not match:
        return None
    month = _MONTHS.get(match["month"].lower())
    weekday = _WEEKDAYS.get(match["weekday"].lower()) if match["weekday"] else None
    if month is None or (match["weekday"] and weekday is None):
        return None
    return {"month": month, "day": int(match["day"]), "year": int(match["year"]) if match["year"] else None, "weekday": weekday}


def _parse_time(text):
    match = _TIME.match(text.strip())
    if not match:
        return None
    hour, minute = int(match["hour"]), int(match["minute"] or 0)
    meridiem = match["meridiem"][0].lower() if match["meridiem"] else None
    if minute > 59 or (meridiem and not 1 <= hour <= 12) or (not meridiem and hour > 23):
        return None
    return {"hour": hour, "minute": minute, "meridiem": meridiem}


def _side(text):
    """One side of a 'when' range: (day or None, time or None). 'Sat, Nov 7, 10 AM', 'Nov 7', '4 PM'."""
    text = text.strip()
    time_only = _parse_time(text)
    if time_only:
        return None, time_only
    head, _, tail = text.rpartition(",")
    if head and _parse_time(tail):
        day = _parse_day(head)
        return (day, _parse_time(tail)) if day else (None, None)
    return _parse_day(text), None


def _clock(t, fallback_meridiem=None):
    meridiem = t["meridiem"] or fallback_meridiem
    if meridiem is None:
        return None if t["hour"] <= 12 else (t["hour"], t["minute"])   # a 24-hour reading needs no meridiem
    return ((t["hour"] % 12) + (12 if meridiem == "p" else 0), t["minute"])


def _upcoming(month, day_of_month, today):
    """The nearest occurrence of a month and day on or after `today`: this year's, else next year's (a 29 February only in a leap year)."""
    for year in (today.year, today.year + 1, today.year + 2):
        try:
            found = date(year, month, day_of_month)
        except ValueError:
            continue
        if found >= today:
            return found
    raise Unusable("the stated date does not exist")


def _start_date(day, today, lookahead, query_year):
    """(date, how) of the stated start day, `how` being 'stated' (the year is the result's own) or 'inferred' (the nearest upcoming occurrence).
    The start must lie in the rolling window [today, today + lookahead days] and, when the search names one year, in that year."""
    if day["year"] is not None:
        try:
            found, how = date(day["year"], day["month"], day["day"]), "stated"
        except ValueError:
            raise Unusable("the stated date does not exist") from None
    else:
        found, how = _upcoming(day["month"], day["day"], today), "inferred"
    if day["weekday"] is not None and found.weekday() != day["weekday"]:
        raise Unusable("the stated weekday does not match the date" + ("" if how == "stated" else " of its next occurrence"))
    if not today <= found <= today + timedelta(days=lookahead):
        raise Unusable(f"the date is outside the {lookahead}-day look-ahead window")
    if query_year is not None and found.year != query_year:
        raise Unusable(f"the search asks for {query_year} events; this one is in {found.year}")
    return found, how


def query_year(query):
    """The one calendar year a search names ('events in San Diego November 2026'), or None when it names none or several."""
    years = set(re.findall(r"\b(?:19|20)\d{2}\b", query or ""))
    return int(years.pop()) if len(years) == 1 else None


def parse_when(when, zone, today, lookahead=DEFAULT_LOOKAHEAD_DAYS, year_constraint=None):
    """(start, end or None, how) as aware datetimes in `zone`, from Google's 'when' text, or Unusable. `how` says where the start's year came from:
    'stated' (the text gives it) or 'inferred' (the nearest upcoming occurrence of the month and day; see _start_date). No time is ever invented."""
    if not isinstance(when, str) or not when.strip():
        raise Unusable("no date is stated")
    parts = [p for p in _DASH.split(when.strip()) if p.strip()]
    if len(parts) > 2:
        raise Unusable("the date is not one start and one end")
    left_day, left_time = _side(parts[0])
    right_day, right_time = _side(parts[1]) if len(parts) == 2 else (None, None)
    if len(parts) == 2 and right_day is None and right_time is None:
        raise Unusable("the end of the date range is not readable")
    if left_day is None:
        raise Unusable("the start day is not readable (relative days such as 'Today' are not used)")
    if left_time is None:
        raise Unusable("no start time is stated")
    borrowed = left_time["meridiem"] is None and left_time["hour"] <= 12 and right_time is not None and right_day is None
    start_clock = _clock(left_time, right_time["meridiem"] if borrowed else None)   # '7 – 10 PM': the start shares the end's PM
    if start_clock is None:
        raise Unusable("the start time does not say AM or PM")
    start_date, how = _start_date(left_day, today, lookahead, year_constraint)
    start = datetime(start_date.year, start_date.month, start_date.day, *start_clock, tzinfo=zone)
    end = None
    if right_time is not None:
        end_clock = _clock(right_time)
        if end_clock is None:
            raise Unusable("the end time does not say AM or PM")
        rolled = False   # the end's year is inferred as the year after the start's
        if right_day is not None:
            if right_day["year"] is not None:
                end_year = right_day["year"]
            else:   # the end day follows the start: the first such date on or after it
                end_year = start_date.year if (right_day["month"], right_day["day"]) >= (start_date.month, start_date.day) else start_date.year + 1
                rolled = end_year != start_date.year
            try:
                end_date = date(end_year, right_day["month"], right_day["day"])
            except ValueError:
                raise Unusable("the stated end date does not exist") from None
            if right_day["weekday"] is not None and end_date.weekday() != right_day["weekday"]:
                raise Unusable("the stated end weekday does not match the end date")
        else:
            end_date = start_date
        end = datetime(end_date.year, end_date.month, end_date.day, *end_clock, tzinfo=zone)
        if right_day is None and end <= start:
            if borrowed:   # '11 – 1 PM' read as 11 PM to 1 PM: the start's own half of the day is not stated
                raise Unusable("the start time does not say AM or PM")
            end += timedelta(days=1)   # '10 PM – 2 AM': the end is on the next day
        if end <= start:
            raise Unusable("the end is not after the start")
        if rolled and end - start > timedelta(days=MAX_ROLLOVER_DAYS):   # 'Nov 7 – Nov 6' is not a year-long event
            raise Unusable(f"the end date, read as next year's, would make the event last more than {MAX_ROLLOVER_DAYS} days")
    return start, end, how


def _text(value, limit=2000):
    return re.sub(r"\s+", " ", value).strip()[:limit] or None if isinstance(value, str) else None


def _web(value):
    parts = urlsplit(value) if isinstance(value, str) else None
    return value if parts and parts.scheme in ("http", "https") and parts.hostname else None


# What may be one event's own page, and whether its title names the event: the shared rules (event_links), which a link found by search meets too
_page = event_page
# Event pages by the platform's own address structure: a Facebook event (/events/.../<id>) and an Eventbrite event (/e/<slug>-<id>)
_PLATFORM_EVENT = ((re.compile(r"(?:^|\.)facebook\.com$"), re.compile(r"^/events/(?:[^/]+/)*\d{6,}/?$")),
                   (re.compile(r"(?:^|\.)eventbrite\.[a-z.]+$"), re.compile(r"^/e/(?:[^/]*-)?\d{6,}/?$")))


def _names_event(url, title, place):
    """Does the address name this event? A known platform's event-page structure, or the title's distinctive words in its path (names_event)."""
    parts = urlsplit(url)
    host, path = parts.hostname.lower(), unquote(parts.path)
    return any(hosts.search(host) and paths.match(path) for hosts, paths in _PLATFORM_EVENT) or names_event(url, title, place)


def _city(result, options):
    """The event's town as the result's last address line gives it ('San Diego, CA'), else the search location's first part."""
    lines = [line for line in (result.get("address") or []) if isinstance(line, str) and line.strip()] if isinstance(result.get("address"), list) else []
    return (lines[-1] if len(lines) > 1 else options.get("location") or "").split(",")[0].strip()


def _town_line(result):
    """The result's last address line when it has more than one ('San Diego, CA'), else None."""
    lines = [line for line in (result.get("address") or []) if isinstance(line, str) and line.strip()] if isinstance(result.get("address"), list) else []
    return lines[-1].strip() if len(lines) > 1 else None


def _town_and_state(result, options):
    """(town, state) of the event: the result's town line ('Cookeville, TN'), else the search location ('Cookeville, Tennessee, United States')."""
    line = [p.strip() for p in (_town_line(result) or "").split(",") if p.strip()]
    if len(line) >= 2:
        return line[0], line[1]
    location = [p.strip() for p in (options.get("location") or "").split(",") if p.strip()]
    return (line[0] if line else location[0] if location else ""), (location[1] if len(location) > 1 else "")


def _addressed(candidate, address, line):
    """The candidate with a street address it lacked and the evidence line that says where it came from; 'address' is no longer missing."""
    verification = replace(candidate.verification, evidence=candidate.verification.evidence + (line,),
                           missing=tuple(name for name in candidate.verification.missing if name != "address"))
    return replace(candidate, address=address, verification=verification)


def _event_url(result, title, place):
    """The event's own page, or None: Google's top-level `link`, else a ticket_info link, an information link ("more info") before a ticket seller's; each
    only when it names the event (a refused top-level link still lets ticket_info be read). Nothing is made up, searched for or taken from the venue, the
    map or SerpApi's details search."""
    entries = [entry for entry in (result.get("ticket_info") or []) if isinstance(entry, dict)] if isinstance(result.get("ticket_info"), list) else []
    ranked = sorted(entries, key=lambda entry: str(entry.get("link_type") or "").strip().lower() == "tickets")   # stable: Google's order within each kind
    for link in [result.get("link")] + [entry.get("link") for entry in ranked]:
        url = _page(link)
        if url and _names_event(url, title, place):
            return url
    return None


class SerpApiGoogleEventsProducer:
    adapter = "serpapi_google_events"
    display_name = "SerpApi Google Events"
    extractor = "producers/serpapi_google_events@v1"
    method = "adapter"
    configuration_fields = (
        {"name": "query", "label": "Event search query", "kind": "text", "required": True, "placeholder": "events in San Diego"},
        {"name": "location", "label": "Search location", "kind": "text", "required": True, "placeholder": "San Diego, California, United States"},
        {"name": "timezone", "label": "Event time zone", "kind": "text", "required": True, "placeholder": "America/Los_Angeles"},
        {"name": "max_pages", "label": "Result pages per scan (each page is one billable search)", "kind": "integer", "default": 1, "min": 1, "max": MAX_PAGES},
        {"name": "lookahead_days", "label": "Event look-ahead (days)", "kind": "integer", "default": DEFAULT_LOOKAHEAD_DAYS, "min": MIN_LOOKAHEAD_DAYS,
         "max": MAX_LOOKAHEAD_DAYS},
    )

    def __init__(self, account=None, clock=time.time, links=None, venues=None):
        self.account, self.clock, self.links = account, clock, links   # links: an EventLinkFinder, asked only for an event Google gives no page for
        self.venues = venues   # a VenueResolver, asked only for an event without a street address

    @staticmethod
    def validate_options(options):
        if any(not isinstance(key, str) for key in options):
            raise ValueError("option names must be text")
        unknown = sorted(set(options) - {"query", "location", "timezone", "max_pages", "lookahead_days", "url"})
        if unknown:
            raise ValueError(f"unsupported serpapi_google_events options: {', '.join(unknown)[:100]}")
        clean = {}
        for name in ("query", "location"):
            value = options.get(name)
            if not isinstance(value, str) or not 1 <= len(value.strip()) <= 200:
                raise ValueError(f"{name} must be text of 1 to 200 characters")
            clean[name] = value.strip()
        zone = options.get("timezone")
        if not isinstance(zone, str) or not _TIMEZONE_NAME.fullmatch(zone):
            raise ValueError("timezone must be an IANA time-zone name")
        try:
            ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("timezone must be an IANA time-zone name") from None
        clean["timezone"] = zone
        pages = options.get("max_pages", 1)
        if isinstance(pages, bool) or not isinstance(pages, int) or not 1 <= pages <= MAX_PAGES:
            raise ValueError(f"max_pages must be an integer between 1 and {MAX_PAGES}")
        if "max_pages" in options:
            clean["max_pages"] = pages
        lookahead = options.get("lookahead_days", DEFAULT_LOOKAHEAD_DAYS)
        if isinstance(lookahead, bool) or not isinstance(lookahead, int) or not MIN_LOOKAHEAD_DAYS <= lookahead <= MAX_LOOKAHEAD_DAYS:
            raise ValueError(f"lookahead_days must be an integer between {MIN_LOOKAHEAD_DAYS} and {MAX_LOOKAHEAD_DAYS}")
        if "lookahead_days" in options:
            clean["lookahead_days"] = lookahead
        if "url" in options and options["url"] not in (SEARCH_URL, SEARCH_URL + "/"):
            raise ValueError(f"the address of a SerpApi search is always {SEARCH_URL}")
        return SEARCH_URL, clean

    @staticmethod
    def search_url(options, page=0):
        query = {"engine": "google_events", "q": options["query"], "location": options["location"]}
        if page:
            query["start"] = page * RESULTS_PER_PAGE
        return f"{SEARCH_URL}?{urlencode(query)}"

    def crawl(self, fetcher, previous, options):
        if self.account is None:
            raise UsageBlocked("no SerpApi account is configured for this service; no search is made", "usage_unavailable")
        zone = ZoneInfo(options["timezone"])
        now = datetime.fromtimestamp(self.clock(), timezone.utc)
        retrieved_at, today = now.isoformat(timespec="seconds"), now.astimezone(zone).date()
        notes = {"searches": 0, "results": 0, "candidates": 0, "set_aside": 0, "pages": 0}
        reasons, candidates, seen = {}, [], set()
        places = self.venues.for_scan() if self.venues is not None else None
        submitted = {record.local_id: record for record in previous or ()}
        for page in range(int(options.get("max_pages", 1))):
            url = self.search_url(options, page)
            try:
                with self.account.permit():
                    notes["searches"] += 1
                    # SerpApi documents /search as its keyed API endpoint; its robots.txt, written for crawlers, disallows that path. Only this
                    # authenticated request waives robots.txt; every other protection of the shared fetcher still applies.
                    text = fetcher.get(url, check_robots=not url.startswith(SEARCH_URL + "?"), secret_query=self.account.secret(), drop_fields=(KEY_FIELD,))
            except UsageBlocked as blocked:
                if page == 0:
                    raise
                notes["stopped"] = blocked.state      # later pages wait for the next scan; what was read is kept
                break
            try:
                payload = json.loads(text)
            except ValueError:
                raise FetchError("serpapi.com: the search answer is not JSON") from None
            if not isinstance(payload, dict):
                raise FetchError("serpapi.com: the search answer is not an object")
            if payload.get("error"):
                if "hasn't returned any results" in str(payload["error"]):
                    break
                raise FetchError(f"serpapi.com: the search reported an error ({_text(str(payload['error']), 160)})")
            results = payload.get("events_results")
            if not isinstance(results, list):
                raise FetchError("serpapi.com: the search answer has no events_results list")
            notes["pages"] += 1
            for result in results:
                notes["results"] += 1
                try:
                    candidate = self._candidate(result, options, zone, today, url, retrieved_at)
                except Unusable as reason:
                    notes["set_aside"] += 1
                    reasons[str(reason)] = reasons.get(str(reason), 0) + 1
                    continue
                if candidate.local_id in seen:
                    continue
                seen.add(candidate.local_id)
                if candidate.url is None and self.links is not None:
                    place = " ".join(filter(None, (candidate.venue, candidate.address, options.get("location"))))
                    candidate = self._linked(candidate, _city(result, options), _town_line(result), place, notes)
                if places is not None:
                    placed = self._placed(candidate, *_town_and_state(result, options), places, submitted.get(candidate.local_id), notes)
                    if placed is None:
                        notes["set_aside"] += 1
                        reason = "a new event whose venue is not one Google Maps business with a street address in its town"
                        reasons[reason] = reasons.get(reason, 0) + 1
                        continue
                    candidate = placed
                candidates.append(candidate)
            if len(results) < RESULTS_PER_PAGE or not (payload.get("serpapi_pagination") or {}).get("next"):
                break
        notes["candidates"] = len(candidates)
        if "links_looked_up" in notes:
            notes["link_search"] = self.links.status
        if "venues_looked_up" in notes:
            notes["venue_searches"] = places.searches
        for number, (reason, count) in enumerate(sorted(reasons.items(), key=lambda item: -item[1])[:8]):
            notes[f"set_aside_{number}"] = f"{count}: {reason}"
        return ProducerResult(tuple(candidates), (), notes)

    def _linked(self, candidate, city, town_line, place, notes):
        """The candidate with the event page the shared finder returns, if it meets this producer's own link rule, and, when the candidate has no street
        address, the venue's street address that page states (its street line completed with Google's own town line when the page writes none);
        everything else, its identity, times and verification status included, as it was."""
        notes["links_looked_up"] = notes.get("links_looked_up", 0) + 1
        link = self.links.find_event_link(candidate.title, candidate.starts_at, candidate.venue, city)
        url = _page(link.url) if link else None
        if not url or not _names_event(url, candidate.title, place):
            return candidate
        notes["links_found"] = notes.get("links_found", 0) + 1
        evidence = candidate.verification.evidence + ("Event page found by a web search for the title, date and place and confirmed by reading it "
                                                      "(its title, day and venue); Google listed none",)
        address = candidate.address
        if link.address and not STREET_LINE.search(candidate.address or ""):   # a street address the source gave is kept
            town = set(re.findall(r"[a-z0-9]+", city.lower()))
            written = set(re.findall(r"[a-z0-9]+", link.address.lower()))
            if town and town <= written:
                address = link.address
            elif "," not in link.address and town_line:
                address = f"{link.address}, {town_line}"
            if address != candidate.address:
                notes["addresses_found"] = notes.get("addresses_found", 0) + 1
                evidence += (f"Street address {link.address} from {link.address_basis} on {url}"
                             + ("" if address == link.address else f", with Google's town line {town_line}"),)
        return replace(candidate, url=url, address=address, verification=replace(candidate.verification, evidence=evidence))

    @staticmethod
    def _placed(candidate, city, state, places, before, notes):
        """The candidate with its venue's Google Maps street address when it has none; the candidate as it is when it has one, names no venue, or no
        answer is available (with the address an earlier scan took from Google Maps for the same venue); None for a new candidate whose venue is
        definitively not one business with a street address in its town. Its identity, times, url and verification status never change."""
        if STREET_LINE.search(candidate.address or "") or not candidate.venue:
            return candidate
        notes["venues_looked_up"] = notes.get("venues_looked_up", 0) + 1
        found = places.resolve_venue(candidate.venue, city, state)
        if found.matched:
            notes["venue_addresses"] = notes.get("venue_addresses", 0) + 1
            return _addressed(candidate, found.address, f"Street address {found.address} from Google Maps: {found.evidence}")
        if found.status == "unavailable":
            notes["venues_unavailable"] = notes.get("venues_unavailable", 0) + 1
            earlier = next((line for line in (before.verification.evidence if before else ()) if line.startswith("Street address ") and " from Google Maps: " in line),
                           None)
            if earlier and before.venue == candidate.venue and STREET_LINE.search(before.address or ""):
                return _addressed(candidate, before.address, earlier)
            return candidate
        if before is not None:   # an event this source already has stays as it is: withholding it would not remove it from the feed
            notes["venues_unmatched_kept"] = notes.get("venues_unmatched_kept", 0) + 1
            return candidate
        return None

    @staticmethod
    def _candidate(result, options, zone, today, listing_url, retrieved_at):
        if not isinstance(result, dict):
            raise Unusable("the result is not an object")
        title = _text(result.get("title"), 300)
        if not title:
            raise Unusable("no title is stated")
        when = (result.get("date") or {}).get("when") if isinstance(result.get("date"), dict) else None
        lookahead = int(options.get("lookahead_days", DEFAULT_LOOKAHEAD_DAYS))
        start, end, how = parse_when(when, zone, today, lookahead, query_year(options.get("query")))
        venue = _text((result.get("venue") or {}).get("name"), 200) if isinstance(result.get("venue"), dict) else None
        lines = [line for line in (result.get("address") or []) if isinstance(line, str) and line.strip()] if isinstance(result.get("address"), list) else []
        address = _text(", ".join(lines), 300)
        description = _text(result.get("description"))
        url = _event_url(result, title, " ".join(filter(None, (venue, address, options.get("location")))))
        image = _web(result.get("image")) or _web(result.get("thumbnail"))
        kind = _text(result.get("event_type"), 60)
        identity = "|".join((re.sub(r"[^a-z0-9]+", " ", title.lower()).strip(), start.isoformat(), re.sub(r"[^a-z0-9]+", " ", (lines[0] if lines else venue or "").lower()).strip()))
        evidence = [f"Title from a SerpApi Google Events result for '{options['query']}' near {options['location']}", f"Date shown: {when.strip()}",
                    f"Local timezone supplied by configured source: {options['timezone']}"]
        if how == "inferred":
            evidence.append(f"Google stated '{when.strip()}' without a year. Year {start.year} inferred as the next calendar occurrence of {start:%B} {start.day} "
                            f"within the configured {lookahead}-day look-ahead window (from {today.isoformat()} in {options['timezone']})")
        else:
            evidence.append(f"Year {start.year} as stated in Google's date")
        if address:
            evidence.append(f"Address shown: {address}")
        return EventCandidate(local_id=hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32], title=title, starts_at=start.isoformat(),
                              ends_at=end.isoformat() if end else None, timezone=options["timezone"], venue=venue, address=address, description=description,
                              url=url, image_url=image, image_source="SerpApi Google Events result image" if image else None, categories=(kind,) if kind else (),
                              provenance=Provenance(options.get("name", "SerpApi Google Events"), listing_url, retrieved_at, (listing_url,)),
                              verification=stated(evidence, False, end.isoformat() if end else None, venue, address, description, image))

    @staticmethod
    def page_role(url, base_url):
        return "feed"
