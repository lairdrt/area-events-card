"""Automated architecture checks on the actual import graph and code of the Discovery Service.

    SOURCE PRODUCERS -> STANDARD RECORDS -> EVENT PROCESSING -> DEDUPLICATION <-> PERSISTENCE (one database) -> PRESENTATION

* A producer (`producers/<name>/`) owns everything about one source and depends only on the contract, the fetch client it is given, URL identity and the neutral text and
  time utilities. Producers do not depend on each other, on persistence, or on any earlier-engine code.
* The application contains only responsibility-based modules; retired engines and source readers are absent.
* Persistence (`persistence/`) exclusively owns the database: only it imports sqlite3, runs SQL or holds a transaction, and the rest of the service reaches it only through
  `persistence/ports.py`. Only the composition root builds it.
* Deduplication (`deduplication/`) owns the matching rules: it imports no database and no persistence, and Persistence knows its interface only, never a concrete strategy.
* Event processing, deduplication and persistence name no source.

The rules are functions of a source tree, `violations(root)`. The real tree must have none; the meta tests inject each kind of violation into a copy of the tree and require the rule
that should catch it to do so. They read the actual import statements and code (AST): comments and names alone prove nothing here."""
import ast
import dataclasses
import functools
import os
import re
import shutil
import tempfile
import unittest

from area_events.discovery import producer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DISCOVERY = os.path.join(ROOT, "src", "area_events")

# Neutral modules: generic utilities and libraries that belong to no engine and no source. They may import only each other and the standard library.
NEUTRAL = frozenset(("discovery.safe", "common.timeutil", "common.textutil", "geolocation.textloc", "contracts.evidence", "enrichment.media", "enrichment.maps", "enrichment.images", "image_extractor", "image_extractor.v1", "image_extractor.association", "image_extractor.responsive", "image_extractor.goose3_adapter", "image_extractor.selfcheck", "classification.classify", "classification.classifiers"))
PRODUCER_MAY_USE = frozenset(("image_extractor.v1", "image_extractor.association", "image_extractor.responsive", "discovery.safe", "common.timeutil", "common.textutil", "geolocation.textloc"))
PRODUCER_MAY_USE_SERVICE = frozenset(("discovery.producer", "discovery.fetching", "discovery.urls"))
COMPOSITION_ROOT = "app"
PERSISTENCE = "persistence"
PORTS = "persistence.ports"
BUILD = "persistence.build"
DEDUPLICATION = "deduplication"
DEDUP_STDLIB = frozenset(("dataclasses", "typing", "hashlib", "re", "datetime", "zoneinfo", "rapidfuzz", "__future__"))
PERSISTENCE_CALLS = frozenset(("execute", "executemany", "executescript", "cursor", "commit", "rollback"))
SQL_TEXT = re.compile(r"\b(SELECT\s.+\sFROM|INSERT\s+(OR\s+\w+\s+)?INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|CREATE\s+(TABLE|INDEX)|ALTER\s+TABLE|PRAGMA\s+\w+)\b", re.I | re.S)
MATCHING_NAME = re.compile(r"(^|_)(match|matches|matching|matcher|similar|similarity|same_event|is_duplicate|find_twin|twin|dedupe|dedup)(_|$)")
STORAGE_CLASS = re.compile(r"^(Sqlite\w+|\w+Repository|DiscoveryStore|LocalMedia\w+|LocalDiscoveryReader|Retention)$")
LEGACY_EVENT_BUILDERS = frozenset(("make_event", "finish_event"))
SOURCE_WORDS = ("sandiego_gov", "ut_austin", "localist", "sandiego.gov", "eventbrite")
# The reviewed layering of the service itself: each module may import only these service modules (new edges must be added here on purpose).
LEGACY_PREFIXES = ("discovery.core", "discovery.engine", "discovery.merge", "discovery.profiles", "discovery.publish", "discovery.refresh", "discovery.registry", "discovery.server", "discovery.supervisor", "discovery.workbench", "discovery.sources")
ALLOWED = {
    COMPOSITION_ROOT: set(), "__main__": {"cli"}, "cli": {"app", "contracts.models", "control.service", "api.events", "extraction.service", "discovery.service"},
    "api": set(), "api.events": {"contracts.models", PORTS}, "api.http_api": {"control.service", "api.events"},
    "classification": set(), "classification.classifiers": {"classification.classify"}, "classification.classify": set(), "classification.photo_subject": set(),
    "common": set(), "common.textutil": set(), "common.timeutil": set(),
    "control": set(), "control.service": {"contracts.models", PORTS}, "contracts": set(), "contracts.evidence": set(), "contracts.models": set(),
    DEDUPLICATION: {"contracts.models", f"{DEDUPLICATION}.scoring"}, f"{DEDUPLICATION}.scoring": set(),
    f"{DEDUPLICATION}.exact": {DEDUPLICATION, f"{DEDUPLICATION}.scoring"}, f"{DEDUPLICATION}.tolerant": {DEDUPLICATION, f"{DEDUPLICATION}.scoring"},
    "discovery": set(), "discovery.fetching": {"discovery.safe"}, "discovery.host": set(), "discovery.producer": set(), "discovery.producers": set(),
    "discovery.producers.city_of_san_diego": {"discovery.producer", "discovery.safe", "common.textutil", "common.timeutil", "geolocation.textloc"},
    "discovery.producers.localist": {"discovery.producer", "discovery.safe"},
    "discovery.producers.sources_producer": {"discovery.producer", "discovery.safe", "discovery.urls"},
    "discovery.safe": set(), "discovery.scheduler": {PORTS}, "discovery.service": {"contracts.evidence", "discovery.safe", "discovery.producer", "contracts.models", "discovery.fetching", PORTS}, "discovery.urls": set(),
    "enrichment": set(), "enrichment.images": set(), "enrichment.maps": set(), "enrichment.media": {"enrichment.maps", "geolocation.textloc", "discovery.safe"},
    "enrichment.service": {"classification.classifiers", "enrichment.images", "discovery.safe", "enrichment.media", "contracts.models", PORTS, "extraction.text"},
    "extraction": set(), "extraction.text": {"geolocation.textloc"}, "extraction.service": {"contracts.evidence", "geolocation.textloc", "contracts.models", "extraction.text"},
    "geolocation": set(), "geolocation.location": {"discovery.safe"}, "geolocation.place": {"geolocation.location"}, "geolocation.textloc": set(),
    PERSISTENCE: set(), PORTS: set(), f"{PERSISTENCE}.areas": {"contracts.models"}, f"{PERSISTENCE}.control": {"contracts.models", f"{PERSISTENCE}.naming"}, f"{PERSISTENCE}.naming": {"discovery.producer"},
    f"{PERSISTENCE}.crawl": {"contracts.models", PORTS, f"{PERSISTENCE}.repositories", "discovery.urls"},
    f"{PERSISTENCE}.events": {"contracts.models", DEDUPLICATION, "api.events"}, f"{PERSISTENCE}.media": {"contracts.models"},
    f"{PERSISTENCE}.processing": {"contracts.evidence", "contracts.models", f"{PERSISTENCE}.events"}, f"{PERSISTENCE}.reading": {"contracts.models"},
    f"{PERSISTENCE}.repositories": {"contracts.models", "discovery.producer", "discovery.urls"}, f"{PERSISTENCE}.scheduling": {"contracts.models"}, f"{PERSISTENCE}.schema": set(),
    f"{PERSISTENCE}.store": {"contracts.models", f"{PERSISTENCE}.schema"}, BUILD: {f"{PERSISTENCE}.{n}" for n in ("areas", "control", "crawl", "events", "media", "naming", "processing", "reading", "repositories", "scheduling", "store")} | {PORTS},
    "validation": set(), "validation.processing": {"contracts.models", PORTS},
}


def modules(root):
    """{dotted name relative to the discovery package: path} for every module under `root` (a package's __init__ is the package itself)."""
    found = {}
    for folder, _, files in os.walk(root):
        if "__pycache__" in folder:
            continue
        for name in files:
            if name.endswith(".py"):
                path = os.path.join(folder, name)
                rel = os.path.relpath(path, root)[:-3].replace(os.sep, ".")
                found[rel[:-9] if rel.endswith(".__init__") else ("" if rel == "__init__" else rel)] = path
    found.pop("", None)
    return found


@functools.lru_cache(maxsize=None)
def _parsed(text):
    return ast.parse(text)


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def parse(path):
    return _parsed(read(path))


def docstring_nodes(tree):
    skipped = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body and isinstance(node.body[0], ast.Expr) \
                and isinstance(node.body[0].value, ast.Constant) and isinstance(node.body[0].value.value, str):
            skipped.add(id(node.body[0].value))
    return skipped


@functools.lru_cache(maxsize=None)
def facts(text):
    """What the rules look for in one file's code (not its comments or docstrings): imports by name, calls that reach a database, SQL text, words that name a source, reads of a
    producer's state, functions named like matching."""
    tree = _parsed(text)
    skipped = docstring_nodes(tree)
    found = {"dynamic": [], "calls": [], "sql": [], "words": [], "state": [], "matching": []}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            attr = getattr(node.func, "id", getattr(node.func, "attr", ""))
            if attr in ("import_module", "__import__"):
                found["dynamic"].append(node.lineno)
            if isinstance(node.func, ast.Attribute) and attr in PERSISTENCE_CALLS:
                found["calls"].append((attr, node.lineno))
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skipped and SQL_TEXT.search(node.value):
            found["sql"].append(node.lineno)
        text_of = node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skipped else getattr(node, "id", None) or getattr(node, "attr", None) \
            or (node.name if isinstance(node, (ast.FunctionDef, ast.ClassDef)) else None)
        if isinstance(text_of, str):
            found["words"].extend((word, node.lineno) for word in SOURCE_WORDS if word in text_of.replace("localist-images", ""))
        if isinstance(node, ast.Attribute) and node.attr == "state":
            found["state"].append(("state", node.lineno))
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and "state_json" in node.value:
            found["state"].append(("state_json", node.lineno))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and MATCHING_NAME.search(node.name):
            found["matching"].append((node.name, node.lineno))
    return found


@functools.lru_cache(maxsize=None)
def imports_in(text):
    """(from-imports as (level, module, names), plain imports as dotted names) of one file's code."""
    froms, plain = [], []
    for node in ast.walk(_parsed(text)):
        if isinstance(node, ast.ImportFrom):
            froms.append((node.level, node.module or "", tuple(alias.name for alias in node.names)))
        elif isinstance(node, ast.Import):
            plain.extend(alias.name for alias in node.names)
    return tuple(froms), tuple(plain)


def graph(root):
    """{module: {"inside": modules of the package it imports, "names": {module: imported names}, "stdlib": top-level modules outside the package}} from the real imports."""
    mods = modules(root)
    result = {}
    for name, path in mods.items():
        base = name.split(".") if path.endswith("__init__.py") else name.split(".")[:-1]
        inside, names, outside = set(), {}, set()

        def add(target, imported=()):
            requested = target
            while target and target not in mods and not is_legacy(target):
                target = target.rpartition(".")[0]
            if is_legacy(requested):
                target = requested
            if target:
                inside.add(target)
                names.setdefault(target, set()).update(imported)

        froms, plain = imports_in(read(path))
        for level, module, imported in froms:
            if level:
                if level - 1 > len(base):
                    continue
                parts = base[:len(base) - (level - 1)] + (module.split(".") if module else [])
            elif module.startswith("area_events."):
                parts = module.split(".")[1:]
            else:
                outside.add(module.split(".")[0])
                continue
            dotted = ".".join(parts)
            for alias in imported:
                child = f"{dotted}.{alias}" if dotted else alias
                add(child if child in mods or is_legacy(child) else dotted, [alias])
        for dotted_name in plain:
            if dotted_name.startswith("area_events."):
                add(".".join(dotted_name.split(".")[1:]))
            else:
                outside.add(dotted_name.split(".")[0])
        result[name] = {"inside": inside - {name}, "names": names, "stdlib": outside}
    return result


def is_new(name):
    return not is_legacy(name) and not is_neutral(name)


def is_producer(name):
    return name.startswith("discovery.producers.")


def producer_of(name):
    return name.split(".")[2] if is_producer(name) else None


def is_neutral(name):
    return name in NEUTRAL


def is_legacy(name):
    return any(name == prefix or name.startswith(prefix + ".") for prefix in LEGACY_PREFIXES)


def is_persistence(name):
    return name == PERSISTENCE or name.startswith(PERSISTENCE + ".")


def is_dedup(name):
    return name == DEDUPLICATION or name.startswith(DEDUPLICATION + ".")


def is_strategy(name):
    return is_dedup(name) and name != DEDUPLICATION


def violations(root):
    """Every breach of the architecture in the tree at `root`, as a list of 'rule: detail' strings."""
    mods, found = modules(root), []
    g = graph(root)
    outside_persistence = [n for n in mods if not is_persistence(n)]

    for name, info in g.items():
        for target in info["inside"]:
            if is_new(name) and is_legacy(target):
                found.append(f"new-to-legacy: {name} imports the earlier engine's {target}")
            if is_neutral(name) and not is_neutral(target):
                found.append(f"neutral-purity: {name} imports {target}")
            if is_new(name) and not is_producer(name) and name != COMPOSITION_ROOT and (target == "discovery.producers" or is_producer(target)):
                found.append(f"producers-wired-in-the-root-only: {name} imports a producer ({target})")
            if is_new(name) and not is_producer(name) and name != COMPOSITION_ROOT and is_new(target) and not is_producer(target):
                if name in ALLOWED and target not in ALLOWED[name]:
                    found.append(f"layering: {name} may not import {target}")
            if is_new(name) and not is_persistence(name) and name != COMPOSITION_ROOT and is_persistence(target) and target != PORTS:
                found.append(f"persistence-ports-only: {name} imports {target}, a Persistence implementation")
            if is_persistence(name) and is_strategy(target):
                found.append(f"persistence-concrete-matcher: {name} imports the matching strategy {target}")
            if is_new(name) and not is_dedup(name) and name != COMPOSITION_ROOT and is_strategy(target):
                found.append(f"strategies-wired-in-the-root-only: {name} imports the matching strategy {target}")
            if is_new(name) and not is_persistence(name) and name != COMPOSITION_ROOT:
                concrete = sorted(n for n in info["names"].get(target, ()) if STORAGE_CLASS.match(n))
                if concrete:
                    found.append(f"concrete-storage: {name} imports the storage class(es) {concrete} from {target}")
        if is_new(name) and not is_producer(name) and name != "discovery.producers" and name != COMPOSITION_ROOT and name not in ALLOWED:
            found.append(f"layering: {name} is not in the reviewed layering")

    for name in outside_persistence:   # exclusive ownership of the database
        if is_legacy(name):
            continue
        info = g[name]
        if "sqlite3" in info["stdlib"]:
            found.append(f"sqlite-outside-persistence: {name} imports sqlite3")
        f = facts(read(mods[name]))
        for attr, line in f["calls"]:
            found.append(f"sql-outside-persistence: {name} calls .{attr}() (line {line})")
        for line in f["sql"]:
            found.append(f"sql-outside-persistence: {name} contains SQL text (line {line})")

    for name in mods:   # Persistence decides nothing about what a duplicate is
        if is_persistence(name):
            for function, line in facts(read(mods[name]))["matching"]:
                found.append(f"matching-in-persistence: {name}.{function} (line {line}) looks like matching logic; deduplication owns it")

    for name, info in g.items():   # deduplication: a pure decision component
        if not is_dedup(name):
            continue
        bad = info["stdlib"] - DEDUP_STDLIB
        if bad:
            found.append(f"dedup-purity: {name} imports {sorted(bad)}")
        for target in info["inside"]:
            if not (is_dedup(target) or target == "contracts.models"):
                found.append(f"dedup-purity: {name} imports {target}")

    if PORTS in mods:   # a port speaks the language of the caller, never SQL
        tree = parse(mods[PORTS])
        for node in ast.walk(tree):
            if isinstance(node, ast.arg) and node.arg in ("tx", "connection", "cursor", "conn", "transaction"):
                found.append(f"port-signature: {PORTS} takes a {node.arg} (line {node.lineno})")

    for name, info in g.items():   # producers
        if not is_producer(name):
            continue
        for target in info["inside"]:
            if is_producer(target) and producer_of(target) != producer_of(name):
                found.append(f"producer-independence: {name} imports another producer ({target})")
            elif is_new(target) and not is_producer(target) and target not in {f"{n}" for n in PRODUCER_MAY_USE_SERVICE} | {""}:
                found.append(f"persistence-and-pipeline: producer {name} imports {target}")
            elif not is_new(target) and target not in PRODUCER_MAY_USE:
                found.append(f"producer-inputs: {name} imports {target}")
        if "sqlite3" in info["stdlib"]:
            found.append(f"persistence: producer {name} imports sqlite3")

    for name in mods:   # an import by name at run time would hide a dependency from every check here
        if not is_legacy(name):
            for line in facts(read(mods[name]))["dynamic"]:
                found.append(f"dynamic-import: {name} imports by name at run time (line {line})")

    # transitive: nothing the new application or a neutral module uses may lead to the earlier engine
    for start in [n for n in g if is_new(n) or is_neutral(n)]:
        stack, seen = [(start, [start])], {start}
        while stack:
            node, path = stack.pop()
            for target in sorted(g[node]["inside"]):
                if is_legacy(target):
                    found.append(f"transitive-legacy: {' -> '.join(path + [target])}")
                elif target not in seen:
                    seen.add(target)
                    stack.append((target, path + [target]))

    visiting, done = set(), set()

    def visit(node, path):
        if node in done:
            return
        if node in visiting:
            found.append(f"cycle: {' -> '.join(path[path.index(node):] + [node])}")
            return
        visiting.add(node)
        for target in sorted(g[node]["inside"]):
            if not is_legacy(target):
                visit(target, path + [node])
        visiting.discard(node)
        done.add(node)

    for name in sorted(g):
        if not is_legacy(name):
            visit(name, [])

    for name, info in g.items():   # legacy event dictionaries are not built by the new application or by neutral code
        if is_legacy(name):
            continue
        for target, imported in info["names"].items():
            if LEGACY_EVENT_BUILDERS & set(imported):
                found.append(f"legacy-event-builders: {name} imports {sorted(LEGACY_EVENT_BUILDERS & set(imported))} from {target}")

    for name in sorted(n for n in mods if (is_new(n) and not is_producer(n) and n not in ("discovery.producers", COMPOSITION_ROOT)) or is_neutral(n)):   # no source named downstream
        for word, line in facts(read(mods[name]))["words"]:
            found.append(f"source-knowledge: {name} mentions '{word}' (line {line})")

    for name in sorted({n for n in mods if is_new(n) and not is_producer(n) and not is_persistence(n)} - {COMPOSITION_ROOT, "cli", "discovery.producer", "discovery.producers"}):
        for what, line in facts(read(mods[name]))["state"]:   # a producer's working state belongs to its producer
            found.append(f"producer-state: {name} reads {what} (line {line})")
    return found


class Real(unittest.TestCase):
    def test_retired_engine_addon_and_specialized_producer_are_absent(self):
        retired = (
            "area_events/DOCS.md", "area_events/Dockerfile", "area_events/config.yaml", "repository.yaml", "scripts/discovery.py", "scripts/workbench.py", "scripts/deploy-ha.ps1", "scripts/install-ha.ps1",
            "src/area_events/discovery/core.py", "src/area_events/discovery/engine.py", "src/area_events/discovery/sources/__init__.py",
            "tests/test_engine.py",
        )
        for relative in retired:
            self.assertFalse(os.path.exists(os.path.join(ROOT, relative)), relative)

    def test_the_real_tree_has_no_violation(self):
        self.assertEqual(violations(DISCOVERY), [])

    def test_the_graph_is_what_was_reviewed(self):
        g = graph(DISCOVERY)
        producers = {n for n in g if is_producer(n)}
        self.assertTrue({"discovery.producers.city_of_san_diego", "discovery.producers.localist"} <= producers)
        self.assertTrue({"discovery.producer", "discovery.service", "extraction.service", "enrichment.service", "validation.processing", PORTS, BUILD, DEDUPLICATION} <= set(g))
        self.assertTrue(set(ALLOWED) - {""} <= set(g), "the reviewed layering names real modules only")
        for name in g:
            if is_new(name) and not is_producer(name) and name not in ("discovery.producers", COMPOSITION_ROOT):
                self.assertIn(name, ALLOWED, f"{name} is not in the reviewed layering")

    def test_what_the_new_application_reaches_is_only_new_or_neutral(self):
        g = graph(DISCOVERY)
        reached, stack = set(), [n for n in g if is_new(n)]
        while stack:
            node = stack.pop()
            if node in reached:
                continue
            reached.add(node)
            stack.extend(g[node]["inside"])
        self.assertEqual({n for n in reached if is_legacy(n)}, set())
        self.assertEqual(sorted(n for n in reached if is_neutral(n)), sorted(NEUTRAL & reached), "and the neutral modules it uses are the declared ones")

    def test_only_persistence_touches_the_database(self):
        g = graph(DISCOVERY)
        users = sorted(n for n, info in g.items() if "sqlite3" in info["stdlib"] and not is_legacy(n))
        self.assertTrue(users and all(is_persistence(n) for n in users), users)
        for name in g:
            if is_new(name) and not is_persistence(name):
                self.assertNotIn("sqlite3", g[name]["stdlib"], name)

    def test_only_the_root_builds_persistence_and_chooses_the_deduplication_strategy(self):
        g = graph(DISCOVERY)
        self.assertEqual(sorted(n for n, info in g.items() if BUILD in info["inside"]), [COMPOSITION_ROOT])
        self.assertEqual(sorted(n for n, info in g.items() if any(is_strategy(t) for t in info["inside"]) and not is_dedup(n)), [COMPOSITION_ROOT])
        self.assertEqual(g[DEDUPLICATION]["inside"], {"contracts.models", "deduplication.scoring"})

    def test_a_new_producer_needs_only_its_own_package_and_the_root_wiring(self):
        g = graph(DISCOVERY)
        importers = {n for n, info in g.items() if any(is_producer(t) or t == "discovery.producers" for t in info["inside"]) and not is_producer(n)}
        self.assertEqual(importers - {n for n in g if not is_new(n)}, {COMPOSITION_ROOT})
        self.assertEqual(g["discovery.producer"]["inside"], set())

    def test_deduplication_uses_event_facts_and_stable_keys_without_generated_match_identity(self):
        strategy_text = "\n".join(read(os.path.join(DISCOVERY, "deduplication", name)) for name in ("__init__.py", "exact.py", "tolerant.py"))
        persistence_events = read(os.path.join(DISCOVERY, "persistence", "events.py"))
        schema = read(os.path.join(DISCOVERY, "persistence", "schema.py"))
        for source in (strategy_text, persistence_events):
            self.assertNotIn("match_key", source)
            self.assertNotIn("by_identity", source)
        self.assertIn("starting_between", strategy_text)
        self.assertIn("event_key", strategy_text)
        self.assertIn("DROP COLUMN match_key", schema, "removal is an explicit migration, not an edited historical schema")

    def test_imagery_enrichment_receives_only_the_media_cache_port(self):
        app = read(os.path.join(DISCOVERY, "app.py"))
        enrichment = read(os.path.join(DISCOVERY, "enrichment", "service.py"))
        ports = read(os.path.join(DISCOVERY, "persistence", "ports.py"))
        self.assertIn("persistence.media_cache", app)
        self.assertNotIn("persistence.media_store", app)
        self.assertIn("store: MediaCache", enrichment)
        self.assertIn("class MediaCache(Protocol)", ports)
        self.assertIn("def get_record(", ports)
        self.assertIn("def put_record(", ports)
        self.assertNotIn("index: dict", ports)


class Records(unittest.TestCase):
    def test_the_records_have_exactly_the_documented_fields(self):
        self.assertEqual([f.name for f in dataclasses.fields(producer.SourceLead)],
                         ["url", "kind", "decision", "reasons", "score", "found_on", "link_text", "events", "fetched", "schema"])
        self.assertEqual([f.name for f in dataclasses.fields(producer.ProducerResult)], ["candidates", "leads", "notes", "schema"])
        self.assertEqual([f.name for f in dataclasses.fields(producer.Provenance)], ["source_name", "listing_url", "retrieved_at", "found_on"])
        self.assertEqual([f.name for f in dataclasses.fields(producer.Verification)], ["status", "evidence", "missing", "note", "verified"])

    def test_the_stored_document_is_the_records_standard_fields_and_nothing_else(self):
        stored = producer.document(producer.EventCandidate(local_id="a", title="t", starts_at="2030-01-01T00:00:00+00:00", state={"anything": 1}))
        self.assertEqual(set(stored), {f.name for f in dataclasses.fields(producer.EventCandidate)} - {"state"})
        self.assertNotIn("anything", repr(stored))

    def test_no_trace_of_the_earlier_stored_form_or_adapter_result(self):
        banned = ("AdapterResult", "from_event_dict", "to_payload", "from_events", "discovery.service.adapters", "from .adapters", "payloads(connection")
        here = os.path.abspath(__file__)
        for top in ("src/area_events", "tests", "scripts"):
            for folder, _, files in os.walk(os.path.join(ROOT, top)):
                if "__pycache__" in folder:
                    continue
                for name in files:
                    path = os.path.join(folder, name)
                    if name.endswith(".py") and os.path.abspath(path) != here:
                        with open(path, encoding="utf-8") as handle:
                            text = handle.read()
                        for word in banned:
                            if word in text:
                                self.fail(f"{os.path.relpath(path, ROOT)} still uses {word}")


class Detection(unittest.TestCase):
    """The rules above must catch what they are for: each case injects a deliberate violation into a copy of the tree."""

    def inject(self, edits):
        with tempfile.TemporaryDirectory() as folder:
            root = os.path.join(folder, "discovery")
            shutil.copytree(DISCOVERY, root, ignore=shutil.ignore_patterns("__pycache__", "*.json"))
            for relative, text in edits.items():
                aliases = {
                    "events.py": "api/events.py", "extraction.py": "extraction/service.py",
                    "processing.py": "validation/processing.py", "enrichment.py": "enrichment/service.py",
                    "control.py": "control/service.py", "py": "app.py", "textutil.py": "common/textutil.py",
                }
                relative = aliases.get(relative, relative)
                if relative.startswith("producers/"):
                    relative = "discovery/" + relative
                target = os.path.join(root, *relative.split("/"))
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, "a", encoding="utf-8") as handle:
                    handle.write("\n" + text + "\n")
            return violations(root)

    def assertCaught(self, edits, *rules):
        found = self.inject(edits)
        for rule in rules:
            self.assertTrue(any(v.startswith(rule) for v in found), f"{rule} did not fire; got {found}")

    def test_a_copy_of_the_real_tree_is_clean(self):
        self.assertEqual(self.inject({}), [])

    def test_a_producer_importing_a_legacy_source_reader(self):
        self.assertCaught({"producers/localist.py": "from area_events.discovery.sources import web"}, "new-to-legacy", "transitive-legacy", "producer-inputs")

    def test_a_producer_importing_the_legacy_engine_or_core(self):
        self.assertCaught({"producers/localist.py": "from area_events.discovery import engine"}, "new-to-legacy", "transitive-legacy")
        self.assertCaught({"producers/city_of_san_diego.py": "from area_events.discovery.core import make_event"}, "new-to-legacy", "legacy-event-builders")

    def test_a_transitive_dependency_on_a_legacy_pipeline(self):
        self.assertCaught({"geolocation/textloc.py": "from area_events.discovery import registry"}, "neutral-purity", "transitive-legacy")
        self.assertCaught({"enrichment/media.py": "from area_events.discovery import workbench"}, "transitive-legacy")

    def test_a_producer_reaching_persistence(self):
        self.assertCaught({"producers/city_of_san_diego.py": "from area_events.persistence import crawl"}, "persistence-and-pipeline")
        self.assertCaught({"producers/city_of_san_diego.py": "from area_events.persistence.ports import CrawlStorage"}, "persistence-and-pipeline")
        self.assertCaught({"producers/localist.py": "import sqlite3\nconnection = sqlite3.connect('x')\nconnection.execute('DELETE FROM candidates')"},
                          "persistence: producer", "sqlite-outside-persistence", "sql-outside-persistence")

    def test_dependencies_between_unrelated_producers(self):
        self.assertCaught({"producers/city_of_san_diego.py": "from area_events.discovery.producers.localist import LocalistProducer"}, "producer-independence")
        self.assertCaught({"producers/localist.py": "from area_events.discovery.producers.city_of_san_diego import CityOfSanDiegoProducer"}, "producer-independence")

    def test_a_downstream_module_depending_on_a_producer(self):
        self.assertCaught({"events.py": "from area_events.discovery.producers import localist"}, "producers-wired-in-the-root-only")
        self.assertCaught({"extraction.py": "from area_events.discovery.producers.city_of_san_diego import parse_listing"}, "producers-wired-in-the-root-only")

    def test_source_specific_code_in_generic_processing(self):
        self.assertCaught({"extraction.py": "WHICH = 'sandiego_gov'"}, "source-knowledge")
        self.assertCaught({"processing.py": "def handle_localist(record):\n    return record"}, "source-knowledge")
        self.assertCaught({"persistence/events.py": "KIND = 'sandiego_gov'"}, "source-knowledge")
        self.assertCaught({"deduplication/exact.py": "if source == 'ut_austin':\n    pass"}, "source-knowledge")
        self.assertCaught({"textutil.py": "x = 'eventbrite'"}, "source-knowledge")

    def test_an_unreviewed_cross_layer_import(self):
        self.assertCaught({"events.py": "from area_events.enrichment import service"}, "layering")
        self.assertCaught({"processing.py": "from area_events.extraction import service"}, "layering")
        self.assertCaught({"persistence/repositories.py": "from area_events.extraction import service"}, "layering")

    def test_an_import_by_name_at_run_time(self):
        self.assertCaught({"producers/localist.py": "import importlib\nlegacy = importlib.import_module('area_events.discovery.core')"}, "dynamic-import")

    def test_a_cycle(self):
        self.assertCaught({"persistence/store.py": "from . import crawl", "persistence/crawl.py": "from . import store"}, "cycle")

    def test_processing_reading_a_producers_working_state(self):
        self.assertCaught({"extraction.py": "def peek(record):\n    return record.state"}, "producer-state")
        self.assertCaught({"enrichment.py": "Q = 'SELECT state_json FROM candidates'"}, "producer-state", "sql-outside-persistence")

    def test_sqlite_or_sql_outside_persistence(self):
        self.assertCaught({"processing.py": "import sqlite3"}, "sqlite-outside-persistence")
        self.assertCaught({"events.py": "def rows(connection):\n    return connection.execute('x')"}, "sql-outside-persistence")
        self.assertCaught({"control.py": "Q = 'UPDATE areas SET name = 1 WHERE area_id = 2'"}, "sql-outside-persistence")
        self.assertCaught({"py": "def commit(tx):\n    tx.commit()"}, "sql-outside-persistence")
        self.assertCaught({"geolocation/textloc.py": "import sqlite3"}, "sqlite-outside-persistence")

    def test_processing_importing_a_concrete_storage_class(self):
        self.assertCaught({"processing.py": "from area_events.persistence.processing import SqliteProcessingStorage"}, "persistence-ports-only", "concrete-storage")
        self.assertCaught({"extraction.py": "from area_events.persistence.repositories import CandidateRepository"}, "persistence-ports-only", "concrete-storage")
        self.assertCaught({"events.py": "from area_events.persistence import store"}, "persistence-ports-only")

    def test_persistence_importing_a_concrete_matcher(self):
        self.assertCaught({"persistence/events.py": "from area_events.deduplication.exact import ExactMatch"}, "persistence-concrete-matcher", "layering")
        self.assertCaught({"persistence/processing.py": "from area_events.deduplication import tolerant"}, "persistence-concrete-matcher")
        self.assertCaught({"processing.py": "from area_events.deduplication.exact import ExactMatch"}, "strategies-wired-in-the-root-only", "layering")

    def test_deduplication_decisions_inside_persistence(self):
        self.assertCaught({"persistence/repositories.py": "def find_twin(title, starts_at):\n    return None"}, "matching-in-persistence")
        self.assertCaught({"persistence/events.py": "def same_event(a, b):\n    return a == b"}, "matching-in-persistence")

    def test_deduplication_reaching_for_the_database(self):
        self.assertCaught({"deduplication/exact.py": "import sqlite3"}, "dedup-purity")
        self.assertCaught({"deduplication/tolerant.py": "from area_events.persistence import events"}, "dedup-purity", "layering")
        self.assertCaught({"deduplication/__init__.py": "import os"}, "dedup-purity")

    def test_a_port_that_lets_a_transaction_through(self):
        self.assertCaught({"persistence/ports.py": "def leak(tx):\n    return tx"}, "port-signature")

    def test_the_root_is_the_only_builder_of_persistence(self):
        self.assertCaught({"cli.py": "from area_events.persistence.build import open_persistence"}, "persistence-ports-only", "layering")


if __name__ == "__main__":
    unittest.main()
