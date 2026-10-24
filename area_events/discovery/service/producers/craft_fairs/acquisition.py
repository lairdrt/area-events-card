"""Source acquisition pilot: from a few trusted public listings to the organizers' own pages, to craft-fair events.

`SourceAcquirer` starts from seed pages (vendor event listings, or an organizer's page), reads the dated cards of a listing, and follows the links those
cards carry (depth one, a small budget) to organizer and event pages it was never told about. Each page it reaches becomes a *lead*: a URL with the page it
was found on, the words of the link, a decision (accepted or rejected) and the reasons. Accepted evidence is merged into one event per real event, with the
organizer's own page preferred over a listing wherever they differ (and each difference reported).

It fetches only through the fetcher it is given (the service's RecordingFetcher over SafeFetcher: public addresses, robots.txt, per-host spacing, size and
time limits) and keeps its state in the instance. It stores nothing; the adapter hands its events and leads back to the service, which stores them in the one
database. The reading rules (what a card is, what a craft fair is, which county) live in `sources/craftfairs.py`.
"""
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit

from ....safe import FetchError
from ....timeutil import localize_pacific, now_pacific, to_pacific
from ...fetching import BudgetExceeded
from ...producer import EventCandidate, ProducerResult, Provenance, SourceLead, stated
from ...urls import canonical_url
from . import reading as craftfairs

SOCIAL_HOSTS = ("facebook.com", "fb.com", "fb.me", "instagram.com", "tiktok.com", "twitter.com", "x.com", "youtube.com", "youtu.be", "pinterest.com", "linkedin.com",
                "threads.net", "nextdoor.com", "meetup.com", "linktr.ee")
NOT_AN_ORGANIZER_HOSTS = ("google.com", "goo.gl", "bit.ly", "tinyurl.com", "paypal.com", "venmo.com", "etsy.com", "amazon.com", "ebay.com", "apple.com", "maps.app.goo.gl",
                          "mailchi.mp", "constantcontact.com", "shopify.com", "squarespace.com", "wix.com")
TICKETING = (("eventbrite.com", re.compile(r"^/e/")),)
DOCUMENT_SUFFIX = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".doc", ".docx", ".zip", ".ics")


@dataclass(frozen=True)
class AcquisitionConfig:
    max_organizer_pages: int = 10   # pages followed from the seeds' links, in all
    horizon_days: int = 150         # only events starting within this many days
    zone: str = "America/Los_Angeles"
    seed_region: str = ""           # what the operator declares for a seed that is itself an event page and does not name its place (never applied to followed links)


def _host(url):
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def _under(host, names):
    return any(host == n or host.endswith("." + n) for n in names)


def _sha(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


class SourceAcquirer:
    def __init__(self, fetcher, source_key, source_name, today, config=AcquisitionConfig()):
        self.fetcher, self.source_key, self.source_name, self.config = fetcher, source_key, source_name, config
        self.now = datetime.now(timezone.utc)
        self.today = today
        self.leads = {}          # canonical url -> lead dict
        self.notes = {"seeds": 0, "seeds_read": 0, "cards": 0, "cards_craft_fair": 0, "links_seen": 0, "links_followed": 0, "pages_read": 0, "not_craft_fair": [],
                      "conflicts": [], "outside_region": [], "budget_stopped": False}
        self._entries, self._pages, self._budget_spent = [], {}, False

    # --- the run ------------------------------------------------------------------------------------------------

    def run(self, seeds):
        """(events, leads, notes). Raises FetchError only when no seed page could be read at all."""
        for seed in seeds:
            self.notes["seeds"] += 1
            self._seed(seed)
        if not self.notes["seeds_read"]:
            raise FetchError("no seed page could be read: " + "; ".join(f"{l['url']}: {'; '.join(l['reasons'])}" for l in self.leads.values() if l["kind"] == "seed")[:300])
        self._follow_links()
        events = self._merge()
        leads = [SourceLead(l["url"], l["kind"], l["decision"], tuple(l["reasons"]), int(l["score"]), l["found_on"], l["link_text"] or None, int(l["events"]), bool(l["fetched"]))
                 for l in self.leads.values()]
        return events, leads, self.notes

    # --- leads ----------------------------------------------------------------------------------------------------

    def _lead(self, url, kind, found_on=None, link_text=None):
        key = canonical_url(url)
        lead = self.leads.get(key)
        if lead is None:
            lead = self.leads[key] = {"url": key, "host": _host(url), "kind": kind, "decision": "pending", "reasons": [], "score": 0, "found_on": found_on, "link_text": link_text,
                                      "events": 0, "fetched": False}
        return lead

    def _decide(self, lead, decision, *reasons, score=None):
        lead["decision"] = decision
        lead["reasons"] = [r for r in reasons if r] or lead["reasons"]
        if score is not None:
            lead["score"] = score

    def _read(self, url, lead):
        """The page's HTML text, or None (the reason is recorded on the lead). A spent budget stops all further reading."""
        if self._budget_spent:
            self._decide(lead, "rejected", "not read: the request budget of this run was spent")
            return None
        try:
            response = self.fetcher.fetch(url)
        except BudgetExceeded as error:
            self._budget_spent = self.notes["budget_stopped"] = True
            self._decide(lead, "rejected", f"not read: {error}")
            return None
        except FetchError as error:   # one unreachable or robots-forbidden page never stops the others
            self._decide(lead, "rejected", f"not read: {str(error)[:120]}")
            return None
        lead["fetched"] = True
        self.notes["pages_read"] += 1
        return response.text if not response.content_type or "html" in response.content_type.lower() else None

    # --- seeds ----------------------------------------------------------------------------------------------------

    def _seed(self, url):
        lead = self._lead(url, "seed")
        text = self._read(url, lead)
        if text is None:
            if lead["decision"] == "pending":
                self._decide(lead, "rejected", "not a web page")
            return
        self.notes["seeds_read"] += 1
        entries = craftfairs.listing_entries(text, url, self.today)
        summary = craftfairs.page_summary(text, url)
        if entries:
            lead["kind"] = "listing"
            region = bool(summary["region"])
            for entry in entries:
                self.notes["cards"] += 1
                craft = craftfairs.craft_fair_text(entry["title"], (entry["description"] or "")[:200])
                where = craftfairs.region_signal(entry["address"], entry["venue"], entry["evidence_text"])
                if not craft:
                    self.notes["not_craft_fair"].append(entry["title"][:60])
                    continue
                self.notes["cards_craft_fair"] += 1
                states_a_place = bool(entry["address"] or entry["venue"])   # the listing's own San Diego heading vouches only for cards that name a place
                if craftfairs.foreign_signal(entry["address"], entry["venue"], entry["evidence_text"]) or not (where or (region and states_a_place)):
                    self.notes["outside_region"].append(entry["title"][:60])
                    continue
                self._entries.append({"seed": url, "entry": entry, "region": where or f"listed under {summary['region']} on {_host(url)}"})
                lead["events"] += 1
            self._decide(lead, "accepted", f"a listing: {len(entries)} dated cards, {lead['events']} craft fairs in San Diego County", score=lead["events"])
        else:
            self._organizer_page(url, text, lead, found_on=None)

    # --- links from the listings' cards -----------------------------------------------------------------------------

    def _classify_link(self, url, text, seed):
        """(accept, reason): whether a link from a craft-fair card is worth reading."""
        parts = urlsplit(url)
        host = _host(url)
        if parts.scheme not in ("http", "https") or not host:
            return False, "not a web address"
        if _under(host, SOCIAL_HOSTS):
            return False, "a social network page (needs a login; not read)"
        if _under(host, NOT_AN_ORGANIZER_HOSTS):
            return False, "a shortener, shop, payment or map address, not an organizer"
        if parts.path.lower().endswith(DOCUMENT_SUFFIX):
            return False, "a document or image, not a page"
        if host == _host(seed):
            return False, "the listing's own site (shop pages)"
        for name, path in TICKETING:
            if _under(host, (name,)):
                return (True, "a ticketing page for one event") if path.search(parts.path) else (False, "a ticketing site page that is not one event")
        return True, "an outside site linked from a craft-fair card"

    def _follow_links(self):
        followed = 0
        for item in self._entries:
            for url, text in item["entry"]["links"]:
                self.notes["links_seen"] += 1
                try:
                    lead = self._lead(url, "organizer", found_on=item["seed"], link_text=text)
                except ValueError:
                    continue
                if lead["decision"] != "pending":
                    continue
                accept, reason = self._classify_link(url, text, item["seed"])
                if not accept:
                    self._decide(lead, "rejected", reason)
                    lead["kind"] = "social" if "social" in reason else "other"
                    continue
                if followed >= self.config.max_organizer_pages:
                    self._decide(lead, "rejected", "over this run's limit of followed links")
                    continue
                followed += 1
                self.notes["links_followed"] += 1
                html = self._read(url, lead)
                if html is not None:
                    self._organizer_page(url, html, lead, found_on=item["seed"])
                    page = self._pages.get(lead["url"])
                    need = craftfairs.distinctive_words(item["entry"]["title"])
                    if page and not page["event"] and need and len(need & page["summary"]["words"]) / len(need) < 0.6:   # a craft-fair page, but not about this card's event
                        del self._pages[lead["url"]]
                        self._decide(lead, "rejected", "does not describe the event on the card it was linked from", *lead["reasons"])

    def _organizer_page(self, url, html, lead, found_on):
        """Assess one page reached by a link (or a seed that is itself an event page): craft fair? San Diego County? a dated event of its own?"""
        summary = craftfairs.page_summary(html, url)
        event = craftfairs.page_event(html, url, self.today)
        region = summary["region"] or (f"declared by the seed list ({self.config.seed_region})" if found_on is None and self.config.seed_region else None)
        reasons = [f"page names a craft fair: {'yes' if summary['craft'] else 'no'}",
                   f"San Diego County: {region or 'not stated on the page'}",
                   f"dated event on the page: {'yes' if event else 'no'}"]
        score = int(summary["craft"]) * 2 + int(bool(summary["region"])) + int(event is not None) * 2 + int(urlsplit(url).scheme == "https")
        if not summary["craft"]:
            lead["kind"] = "other"
            self._decide(lead, "rejected", "not a craft-fair page", *reasons, score=score)
            return
        lead["kind"] = "event" if event else "organizer"
        self._decide(lead, "accepted", *reasons, score=score)
        self._pages[lead["url"]] = {"url": url, "summary": summary, "event": event, "lead": lead, "found_on": found_on, "region": region}
        if event:
            lead["events"] = 1

    # --- merging ----------------------------------------------------------------------------------------------------

    def _merge(self):
        groups = []   # each: {"listing": [items], "pages": [pages]}
        for item in self._entries:
            entry = item["entry"]
            if (entry["day"] - self.today).days > self.config.horizon_days:
                continue
            target = next((g for g in groups if any(craftfairs.same_event(entry_view(i), entry_view(item)) for i in g["listing"])), None)
            if target is None:
                target = {"listing": [], "pages": []}
                groups.append(target)
            target["listing"].append(item)
        for page in self._pages.values():
            linked = [g for g in groups if any(canonical_url(u) == page["lead"]["url"] for i in g["listing"] for u, _ in i["entry"]["links"])]
            if linked:
                linked[0]["pages"].append(page)
                continue
            if page["event"]:   # a dated organizer page that no listing pointed at (a seed that is an event page): its own event, or one a listing also describes
                view = page_view(page, self.config.zone)
                same = next((g for g in groups if any(craftfairs.same_event(entry_view(i), view) for i in g["listing"])), None)
                if same:
                    same["pages"].append(page)
                else:
                    groups.append({"listing": [], "pages": [page]})
        events = []
        for group in groups:
            event = self._event(group)
            if event:
                events.append(event)
        events.sort(key=lambda e: e.starts_at)
        return events

    def _event(self, group):
        zone = self.config.zone
        dated = [p for p in group["pages"] if p["event"]]
        organizer = dated[0] if dated else None
        listing = sorted(group["listing"], key=lambda i: (-sum(bool(i["entry"][k]) for k in ("venue", "address", "description", "end")), -len(i["entry"]["title"])))
        first = listing[0]["entry"] if listing else None
        facts = {}
        if organizer:
            facts.update(page_view(organizer, zone))
            if listing and not craftfairs.distinctive_words(facts["title"]):
                facts["title"] = listing[0]["entry"]["title"]   # the organizer's page only gives a generic name ("Annual Craft Fair"): the listing's name is kept
        if first:
            for key in ("title", "venue", "address", "description"):
                if not facts.get(key):
                    facts[key] = first[key]
            if "naive_start" not in facts:
                facts["naive_start"] = datetime(first["day"].year, first["day"].month, first["day"].day, *first["start"])
                facts["naive_end"] = datetime(first["day"].year, first["day"].month, first["day"].day, *first["end"]) if first["end"] else None
        if "naive_start" not in facts or not facts.get("title"):
            return None
        region = (listing[0]["region"] if listing else None) or (organizer and organizer["region"]) or next((p["region"] for p in group["pages"] if p["region"]), None)
        if not region or any(p["summary"]["foreign"] and not p["summary"]["region"] for p in dated):   # the organizer's own page puts it in another state
            region = None
        if not region:
            self.notes["outside_region"].append(facts["title"][:60])
            return None
        if (facts["naive_start"].date() - self.today).days > self.config.horizon_days:
            return None
        evidence, hosts = [], set()
        for item in listing:
            e = item["entry"]
            hosts.add(_host(item["seed"]))
            evidence.append(f"listed on {_host(item['seed'])}: \"{e['title']}\", {e['day']:%B} {e['day'].day}, {e['start'][0]:02d}:{e['start'][1]:02d}"
                            + (f", {e['address']}" if e["address"] else "") + f" ({item['seed']})")
        for page in group["pages"]:
            hosts.add(_host(page["url"]))
            evidence.append(f"organizer page {page['url']}: " + ("states the date and time itself" if page["event"] else "names the craft fair (no date on the page)")
                            + (f"; found on {_host(page['found_on'])}" if page["found_on"] else "; supplied as a seed"))
        conflicts = self._conflicts(facts, listing, organizer)
        for line in conflicts:
            self.notes["conflicts"].append(line)
            evidence.append("conflict: " + line)
        evidence.append(f"San Diego County: {region}")
        start = localize_pacific(facts["naive_start"])
        end = localize_pacific(facts["naive_end"]) if facts.get("naive_end") else None
        street = craftfairs.street_key(facts.get("address"))
        identity = f"{facts['naive_start'].date()}|{street or ' '.join(sorted(craftfairs.distinctive_words(facts['title'])))}"
        url = (organizer or {}).get("url") or next((p["url"] for p in group["pages"]), None) or (listing[0]["seed"] if listing else None)
        image = organizer["event"].get("image") if organizer else None
        return EventCandidate(local_id=_sha(identity)[:12], title=facts["title"], starts_at=start.isoformat(), ends_at=end.isoformat() if end else None, timezone=zone,
                              venue=facts.get("venue"), address=facts.get("address"), description=facts.get("description"), url=url, image_url=image,
                              image_source=_host(organizer["url"]) if image else None, categories=("Craft Fair",),
                              organizer=(organizer["event"].get("organizer") or None) if organizer else None,
                              provenance=Provenance(self.source_name, listing[0]["seed"] if listing else url, self.now.isoformat(timespec="seconds"),
                                                    tuple(sorted({item["seed"] for item in listing}))),
                              verification=stated(evidence, len(hosts) >= 2, end.isoformat() if end else None, facts.get("venue"), facts.get("address"), facts.get("description"), image))

    @staticmethod
    def _conflicts(facts, listing, organizer):
        """Differences between what a listing says and what the organizer's own page says (the organizer wins)."""
        if not organizer:
            return []
        out, own = [], facts
        for item in listing:
            e = item["entry"]
            said = datetime(e["day"].year, e["day"].month, e["day"].day, *e["start"])
            if said != own["naive_start"]:
                out.append(f"{_host(item['seed'])} says {said:%Y-%m-%d %H:%M}, the organizer says {own['naive_start']:%Y-%m-%d %H:%M}")
            a, b = craftfairs.street_key(e["address"]), craftfairs.street_key(own.get("address"))
            if a and b and a != b:
                out.append(f"{_host(item['seed'])} gives the address '{e['address']}', the organizer '{own['address']}'")
        return out


def entry_view(item):
    entry = item["entry"] if "entry" in item else item
    return {"day": entry["day"], "title": entry["title"], "address": entry["address"]}


def page_view(page, zone):
    event = page["event"]
    if "naive_start" in event:
        naive_start, naive_end = event["naive_start"], event["naive_end"]
    else:
        naive_start = to_pacific(event["start"]).replace(tzinfo=None)
        naive_end = to_pacific(event["end"]).replace(tzinfo=None) if event.get("end") else None
    return {"title": event["title"], "day": naive_start.date(), "naive_start": naive_start, "naive_end": naive_end, "venue": event.get("venue"),
            "address": event.get("address"), "description": event.get("description")}



class CraftFairProducer:
    """Source acquisition pilot: craft fairs found by following public listings to the organizers' own pages (`SourceAcquirer`). The source's `seeds` option lists the pages it
    starts from; everything else (organizer and event pages) is found by following their links. The pages it considered come back as leads."""

    adapter = "craft_fairs"
    extractor = "sources/craftfairs@v1"
    method = "adapter"

    def __init__(self, today=lambda: now_pacific().date()):
        self._today = today

    def crawl(self, fetcher, previous, options):
        seeds = tuple(options.get("seeds") or (options["url"],))
        config = AcquisitionConfig(max_organizer_pages=int(options.get("max_organizer_pages", 10)), horizon_days=int(options.get("horizon_days", 150)),
                                   seed_region=str(options.get("seed_region", "")))
        events, leads, notes = SourceAcquirer(fetcher, options["source_key"], options.get("name", "Craft fairs"), self._today(), config).run(seeds)
        return ProducerResult(tuple(events), tuple(leads), notes)

    @staticmethod
    def page_role(url, base_url):
        return "listing" if url.split("?")[0].rstrip("/") == base_url.split("?")[0].rstrip("/") else "detail"
