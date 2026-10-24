"""Producer for the City of San Diego events listing (https://www.sandiego.gov/events): listing pages, then a bounded number of event pages per run, with event pages
already read taken from the state left on the previous run's records. The page-reading functions are the source's own (`sources/sandiego_gov.py`); this module turns what they
read into standard records."""
from ...core import TIMEZONE_NAME, FetchError, now_pacific
from ...sources import sandiego_gov
from ...sources.detailwork import DetailWork
from ..producer import EventCandidate, ProducerResult, Provenance, stated


class CityOfSanDiegoProducer:
    adapter = "sandiego_gov"
    extractor = f"sources/sandiego_gov@v{sandiego_gov.DETAIL_VERSION}"
    method = "adapter"

    def crawl(self, fetcher, previous, options):
        source_name, url = options.get("name", sandiego_gov.NAME), options.get("url", sandiego_gov.LISTING_URL)
        pages = int(options.get("pages", 2))
        retrieved_at = now_pacific().isoformat(timespec="seconds")
        now = now_pacific()
        work = DetailWork(previous, lambda record: record.local_id, int(options.get("max_details_per_run", 10)), enabled=True, version=sandiego_gov.DETAIL_VERSION,
                          state_of=lambda record: record.state)
        notes = {"pages": pages, "listing_items": 0, "unparsed_date_skipped": 0, "past_skipped": 0}
        rows, seen, pages_ok = [], set(), 0
        for number in range(pages):
            page_url = url if number == 0 else f"{url}?page={number}"
            try:
                page = fetcher.get(page_url)
            except Exception as error:
                notes.setdefault("listing_errors", []).append(str(error))
                continue
            pages_ok += 1
            for item in sandiego_gov._parse_listing(page, url):
                notes["listing_items"] += 1
                start, end = sandiego_gov._parse_start_end(item["date_text"])
                if not start:
                    notes["unparsed_date_skipped"] += 1
                    continue
                if (end or start) < now:
                    notes["past_skipped"] += 1
                    continue
                local_id = f"{item['url'].rstrip('/').rsplit('/', 1)[-1]}@{start.date().isoformat()}"
                if local_id in seen:
                    continue
                seen.add(local_id)
                failed, detail, old = False, None, work.reusable(local_id)
                if old:
                    detail = {"venue": old.venue, "address": old.address, "date_on_page": old.verification.status == "corroborated"}
                    work.reused()
                elif work.should_fetch(local_id):   # what is not fetched now is picked up by a later refresh
                    try:
                        detail = sandiego_gov._parse_detail(fetcher.get(item["url"]), item["title"], start)
                        work.done()
                    except Exception as error:
                        work.failed(local_id, item["url"], error)
                        failed = True
                evidence = [f"listing date line '{item['date_text']}' parsed as a single-day Pacific time start"]
                date_on_page = bool(detail and detail.get("date_on_page"))
                if date_on_page:
                    evidence.append("event page repeats the title and the event date")
                if (detail or {}).get("address"):
                    evidence.append("address from the event page's Get Directions line (it names a street that the page's location text also names)")
                rows.append({"local_id": local_id, "title": item["title"], "start": start.isoformat(), "end": end.isoformat() if end else None, "url": item["url"],
                             "venue": (detail or {}).get("venue"), "address": (detail or {}).get("address"), "description": item["description"], "evidence": evidence,
                             "corroborated": date_on_page, "state": work.next_state(local_id, detail is not None, failed)})
        if not pages_ok:
            raise FetchError("; ".join(notes.get("listing_errors", ["no listing page readable"])))
        titles = {}
        for row in rows:
            if row["address"]:
                titles.setdefault(row["address"].lower(), set()).add(row["title"])
        general = {address for address, names in titles.items() if len(names) >= sandiego_gov.REPEATED_ADDRESS}
        for row in rows:
            if row["address"] and row["address"].lower() in general:
                row["address"] = None   # the same address on several unrelated events is not a venue
                row["evidence"] = [e for e in row["evidence"] if "Get Directions" not in e]
                notes["general_address_dropped"] = notes.get("general_address_dropped", 0) + 1
        provenance = Provenance(source_name, url, retrieved_at)
        candidates = [EventCandidate(local_id=row["local_id"], title=row["title"], starts_at=row["start"], ends_at=row["end"], timezone=TIMEZONE_NAME, venue=row["venue"],
                                     address=row["address"], description=row["description"], url=row["url"], provenance=provenance, state=row["state"],
                                     verification=stated(row["evidence"], row["corroborated"], row["end"], row["venue"], row["address"], row["description"], None))
                      for row in rows]
        return ProducerResult(tuple(candidates), (), work.notes(notes))

    @staticmethod
    def page_role(url, base_url):
        return "listing" if url.split("?")[0].rstrip("/") == base_url.split("?")[0].rstrip("/") else "detail"
