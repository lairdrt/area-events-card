"""Which problems need an administrator, from the Discovery Service's own status. No Home Assistant imports and nothing stored.

One pure classifier serves the card's issue badge (through the hub's area view) and the management Overview (through the management snapshot), so
both count the same issues from the same rules. Every issue comes from a recorded fact: the hub's connection, the shared settings, an area's
assigned sources, a source's current scan error (a successful scan clears it), its last successful scan and the SerpApi usage report. A reason
the service did not state is never supplied.

    issue = {id, severity: error|warning, kind, area_keys (empty: every area), source_key, component, summary, detail, last_success_at, action}

Identity: one issue per id. A source attached to two areas is one issue in the all-areas view and one in each area's view; a SerpApi usage hold
is one issue for the account however many SerpApi sources it stops; a stale area is one issue for that area. When the service cannot be reached
only that is reported: what was last read is no longer current. A stale area becomes an issue 12 hours after its newest successful scan: until
then it is listed under `upcoming` with that moment as `active_from`, so a card's age timer can count it without a new read.
"""
import types

SCHEMA = "area_events.diagnostics.v1"
STALE_AFTER_S = 12 * 3600
DETAIL_LIMIT = 300
SERPAPI_TYPE = "serpapi_google_events"
_ORDER = types.MappingProxyType({"service_unreachable": 0, "discovery_paused": 1, "area_no_sources": 2, "source_failing": 3, "serpapi_held": 4,
                                 "area_stale": 5})

# The SerpApi usage states that hold a source's searches (the usage report's own vocabulary), with what to do about each.
SERPAPI_HELD = types.MappingProxyType({
    "blocked_free_tier": ("Google Events searches stopped at the free-tier safety limit",
                          "Searches resume by themselves when SerpApi renews the free allowance{renewal}."),
    "paused_threshold": ("Google Events searches paused at the usage threshold",
                         "Wait for the renewal{renewal}, or raise “Pause at” in a SerpApi producer's settings under Sources."),
    "waiting_for_renewal": ("Google Events searches are waiting for the SerpApi allowance to renew",
                            "Searches resume by themselves when SerpApi reports the new allowance{renewal}."),
    "usage_unavailable": ("Google Events searches held: SerpApi usage could not be read",
                          "Check the SerpApi key and account on the Discovery Service host; searches resume once usage can be read."),
})


def _clip(text):
    if text is None:
        return None
    text = " ".join(str(text).split())
    return text if len(text) <= DETAIL_LIMIT else text[:DETAIL_LIMIT - 1] + "…"


def _issue(id, severity, kind, component, summary, action, area_keys=(), source_key=None, detail=None, last_success_at=None):
    return {"id": id, "severity": severity, "kind": kind, "area_keys": sorted(area_keys), "source_key": source_key, "component": component,
            "summary": summary, "detail": _clip(detail), "last_success_at": last_success_at, "action": action}


def _sorted(issues):
    return sorted(issues, key=lambda i: (i["severity"] != "error", _ORDER.get(i["kind"], 9), i["summary"]))


def classify(connected, last_error, status, areas, names=None, types=None, usage=None, now=0):
    """{"schema", "issues": the active issues, "upcoming": issues that become active at their `active_from`} for every area.

    `status` is GET /v1/discovery/status's status, `areas` GET /v1/areas, `names` and `types` {source_key: configured name / producer type}
    from GET /v1/sources, `usage` GET /v1/usage (None when not read or not reported)."""
    if not connected:
        return {"schema": SCHEMA, "upcoming": [], "issues": [_issue(
            "service:unreachable", "error", "service_unreachable", "Discovery Service", "Discovery Service is not reachable",
            "Check that the Discovery Service host is running and reachable from Home Assistant. Events shown are from the last successful read.",
            detail=last_error or "no answer")]}
    status, names, types = status or {}, names or {}, types or {}
    settings = status.get("settings") or {}
    states = {s.get("source_key"): s for s in status.get("sources") or ()}
    held = ((usage or {}).get("serpapi") or {})
    held_sources = held.get("sources") or {}
    paused = bool(settings.get("paused"))
    issues, upcoming, failing, holds = [], [], {}, {}
    if paused:
        issues.append(_issue("discovery:paused", "warning", "discovery_paused", "Discovery", "Discovery is paused",
                             "Resume discovery once any maintenance is finished; nothing is scanned while it is paused.",
                             detail="No source is being scanned; events found earlier are still shown."))
    for area in areas or ():
        if not area.get("enabled"):
            continue   # switched off on purpose: nothing about it is expected to happen
        key, name, sources = area.get("area_key"), area.get("name") or area.get("area_key"), list(area.get("sources") or ())
        if not sources:
            issues.append(_issue(f"area:{key}:no_sources", "warning", "area_no_sources", name, f"{name} has no event sources",
                                 f"Assign at least one producer to {name} under Areas.", area_keys=[key]))
            continue
        enabled = [states[s] for s in sources if s in states and states[s].get("enabled")]
        for state in enabled:
            source = state["source_key"]
            if not state.get("last_error"):
                continue
            hold = (held_sources.get(source) or {}).get("state") if types.get(source) == SERPAPI_TYPE else None
            bucket = holds.setdefault(hold, {}) if hold in SERPAPI_HELD else failing
            bucket.setdefault(source, {"state": state, "areas": set()})["areas"].add(key)
        successes = [s.get("last_success_at") for s in enabled if s.get("last_success_at")]
        if enabled and successes and not paused:   # paused: being out of date is the pause, already reported; never read yet: not stale
            newest = max(successes)
            stale = _issue(f"area:{key}:stale", "warning", "area_stale", name, f"{name} — event information is stale",
                           "Check that discovery is running and that this area's sources can be read; failing sources are listed separately.",
                           area_keys=[key], detail="No source of this area has completed a scan in the last 12 hours.", last_success_at=newest)
            if now - newest > STALE_AFTER_S:
                issues.append(stale)
            else:
                upcoming.append({**stale, "active_from": newest + STALE_AFTER_S + 1})
    for source, found in failing.items():
        state = found["state"]
        label = names.get(source) or source
        issues.append(_issue(f"source:{source}:failing", "error", "source_failing", label, f"{label} — last scan failed",
                             "Open Sources and check this producer's settings and the site it reads. The next scheduled scan retries by itself.",
                             area_keys=found["areas"], source_key=source, detail=state.get("last_error"), last_success_at=state.get("last_success_at")))
    for hold, found in holds.items():
        summary, action = SERPAPI_HELD[hold]
        renewal = held.get("resumes") or ((held.get("account") or {}).get("renewal"))
        reason = next((held_sources[s].get("reason") for s in sorted(found) if (held_sources.get(s) or {}).get("reason")), None) or held.get("reason")
        affected = ", ".join(sorted(names.get(s) or s for s in found))
        issues.append(_issue(f"serpapi:{hold}", "warning", "serpapi_held", "SerpApi account", summary,
                             action.format(renewal=f" (renewal {renewal})" if renewal else ""),
                             area_keys=set().union(*(f["areas"] for f in found.values())),
                             detail=f"{reason}. Affected: {affected}." if reason else f"Affected: {affected}."))
    return {"schema": SCHEMA, "issues": _sorted(issues), "upcoming": _sorted(upcoming)}


def for_area(diagnostics, area_key):
    """The issues of one area's view: its own and those of every area (a paused service, an unreachable one)."""
    def mine(issue):
        return not issue["area_keys"] or area_key in issue["area_keys"]
    return {"schema": diagnostics["schema"], "issues": [i for i in diagnostics["issues"] if mine(i)],
            "upcoming": [i for i in diagnostics["upcoming"] if mine(i)]}


def active(diagnostics, now):
    """The issues active at `now`: those listed, and the upcoming ones whose moment has come (what the card and Overview count)."""
    return diagnostics["issues"] + [i for i in diagnostics["upcoming"] if i["active_from"] <= now]
