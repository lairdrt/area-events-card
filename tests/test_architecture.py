"""Automated architecture checks on the actual import graph of the Discovery Service and the modules it uses.

    SOURCE PRODUCERS -> STANDARD RECORDS -> PROCESSING / VALIDATION -> DEDUPLICATION <-> PERSISTENCE -> PRESENTATION

* A producer (`service/producers/<name>/`) owns everything about one source and depends only on the contract, the fetch client it is given, URL identity and the neutral text and
  time utilities. Producers do not depend on each other, on persistence, or on any earlier-engine code.
* The new application (`service/`) and the neutral modules it relies on never import, directly or through any chain of imports, the earlier engine (`core`, `engine`, `registry`,
  `server`, `workbench`, ...) or its source readers (`sources/`). The earlier engine may import the new code; not the other way round.
* Processing, deduplication and persistence name no source.

The rules are functions of a source tree, `violations(root)`. The real tree must have none; the meta tests inject each kind of violation into a copy of the tree and require the rule
that should catch it to do so. They read the actual import statements (AST): comments and names alone prove nothing here."""
import ast
import dataclasses
import functools
import os
import shutil
import tempfile
import unittest

from discovery.service import producer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DISCOVERY = os.path.join(ROOT, "area_events", "discovery")

# Neutral modules: generic utilities and libraries that belong to no engine and no source. They may import only each other and the standard library.
NEUTRAL = frozenset(("safe", "timeutil", "textutil", "textloc", "evidence", "media", "maps", "images", "classify", "classifiers"))
PRODUCER_MAY_USE = frozenset(("safe", "timeutil", "textutil", "textloc"))
PRODUCER_MAY_USE_SERVICE = frozenset(("producer", "fetching", "urls"))
COMPOSITION_ROOT = "service.app"
PERSISTENCE_CALLS = frozenset(("execute", "executemany", "executescript", "cursor"))
LEGACY_EVENT_BUILDERS = frozenset(("make_event", "finish_event"))
SOURCE_WORDS = ("sandiego_gov", "ut_austin", "localist", "craft_fairs", "sd_craft_fairs", "sandiego.gov", "eventbrite", "sweetandquirky")
DOWNSTREAM = frozenset(("service.service", "service.repositories", "service.events", "service.extraction", "service.processing", "service.enrichment", "service.media_store",
                        "service.reader", "service.api", "service.control", "service.store", "service.scheduler", "service.host", "service.areas", "service.text", "service.fetching",
                        "service.contracts", "service.schema", "service.urls", "service.producer", "service.cli")) | NEUTRAL
# The reviewed layering of the service itself: each module may import only these service modules (new edges must be added here on purpose).
ALLOWED = {
    "service": set(), "service.api": {"service.control", "service.events"}, "service.areas": set(), "service.cli": {"service.app", "service.contracts", "service.control", "service.events",
                                                                                                                     "service.extraction", "service.service"},
    "service.contracts": set(), "service.control": {"service.areas", "service.contracts", "service.store"}, "service.enrichment": {"service.media_store", "service.text"},
    "service.events": {"service.control"}, "service.extraction": {"service.control", "service.processing", "service.text"}, "service.fetching": set(), "service.host": set(),
    "service.media_store": set(), "service.processing": {"service.control", "service.events"}, "service.producer": set(), "service.reader": {"service.contracts"},
    "service.repositories": {"service.contracts", "service.producer", "service.urls"}, "service.scheduler": {"service.areas"}, "service.schema": set(),
    "service.service": {"service.contracts", "service.fetching", "service.producer", "service.repositories", "service.scheduler", "service.store", "service.urls"},
    "service.store": {"service.schema"}, "service.text": set(), "service.urls": set(),
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
                found[rel[:-9] if rel.endswith(".__init__") else rel] = path
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


@functools.lru_cache(maxsize=None)
def facts(text):
    """What the rules look for in one file's code (not its comments or docstrings): imports by name, calls that reach a database, words that name a source, reads of producer state."""
    tree = _parsed(text)
    skipped = docstring_nodes(tree)
    found = {"dynamic": [], "calls": [], "words": [], "state": []}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            attr = getattr(node.func, "id", getattr(node.func, "attr", ""))
            if attr in ("import_module", "__import__"):
                found["dynamic"].append(node.lineno)
            if isinstance(node.func, ast.Attribute) and attr in PERSISTENCE_CALLS:
                found["calls"].append((attr, node.lineno))
        text_of = node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skipped else getattr(node, "id", None) or getattr(node, "attr", None)             or (node.name if isinstance(node, (ast.FunctionDef, ast.ClassDef)) else None)
        if isinstance(text_of, str):
            found["words"].extend((word, node.lineno) for word in SOURCE_WORDS if word in text_of.replace("localist-images", ""))
        if isinstance(node, ast.Attribute) and node.attr == "state":
            found["state"].append(("state", node.lineno))
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and "state_json" in node.value:
            found["state"].append(("state_json", node.lineno))
    return found


def graph(root):
    """{module: {"inside": modules of the package it imports, "names": {module: imported names}, "stdlib": top-level modules outside the package}} from the real imports."""
    mods = modules(root)
    result = {}
    for name, path in mods.items():
        base = name.split(".") if path.endswith("__init__.py") else name.split(".")[:-1]
        inside, names, outside = set(), {}, set()

        def add(target, imported=()):
            while target and target not in mods:
                target = target.rpartition(".")[0]
            if target:
                inside.add(target)
                names.setdefault(target, set()).update(imported)

        for node in ast.walk(parse(path)):
            if isinstance(node, ast.ImportFrom):
                if node.level:
                    if node.level - 1 > len(base):
                        continue
                    anchor = base[:len(base) - (node.level - 1)]
                    parts = anchor + (node.module.split(".") if node.module else [])
                elif (node.module or "").split(".")[0] == "discovery":
                    parts = (node.module or "").split(".")[1:]
                else:
                    outside.add((node.module or "").split(".")[0])
                    continue
                dotted = ".".join(parts)
                for alias in node.names:
                    child = f"{dotted}.{alias.name}" if dotted else alias.name
                    if child in mods:
                        add(child, [alias.name])
                    else:
                        add(dotted, [alias.name])
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] == "discovery":
                        add(".".join(alias.name.split(".")[1:]))
                    else:
                        outside.add(alias.name.split(".")[0])
        result[name] = {"inside": inside - {name}, "names": names, "stdlib": outside}
    return result


def is_new(name):
    return name == "service" or name.startswith("service.")


def is_producer(name):
    return name.startswith("service.producers.")


def producer_of(name):
    return name.split(".")[2] if is_producer(name) else None


def is_neutral(name):
    return name in NEUTRAL


def is_legacy(name):
    return not is_new(name) and not is_neutral(name)


def docstring_nodes(tree):
    skipped = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body and isinstance(node.body[0], ast.Expr) \
                and isinstance(node.body[0].value, ast.Constant) and isinstance(node.body[0].value.value, str):
            skipped.add(id(node.body[0].value))
    return skipped


def violations(root):
    """Every breach of the architecture in the tree at `root`, as a list of 'rule: detail' strings."""
    mods, found = modules(root), []
    g = graph(root)

    for name, info in g.items():
        for target in info["inside"]:
            if is_new(name) and is_legacy(target):
                found.append(f"new-to-legacy: {name} imports the earlier engine's {target}")
            if is_neutral(name) and not is_neutral(target):
                found.append(f"neutral-purity: {name} imports {target}")
            if is_new(name) and not is_producer(name) and name != COMPOSITION_ROOT and (target == "service.producers" or is_producer(target)):
                found.append(f"producers-wired-in-the-root-only: {name} imports a producer ({target})")
            if is_new(name) and not is_producer(name) and name != COMPOSITION_ROOT and name in ALLOWED and is_new(target) and not is_producer(target):
                if target not in ALLOWED[name]:
                    found.append(f"layering: {name} may not import {target}")
        if is_new(name) and not is_producer(name) and name != "service.producers" and name != COMPOSITION_ROOT and name not in ALLOWED:
            found.append(f"layering: {name} is not in the reviewed layering")

    for name, info in g.items():   # producers
        if not is_producer(name):
            continue
        for target in info["inside"]:
            if is_producer(target) and producer_of(target) != producer_of(name):
                found.append(f"producer-independence: {name} imports another producer ({target})")
            elif is_new(target) and not is_producer(target) and target not in {f"service.{n}" for n in PRODUCER_MAY_USE_SERVICE} | {"service"}:
                found.append(f"persistence-and-pipeline: producer {name} imports {target}")
            elif not is_new(target) and target not in PRODUCER_MAY_USE:
                found.append(f"producer-inputs: {name} imports {target}")
        if "sqlite3" in info["stdlib"]:
            found.append(f"persistence: producer {name} imports sqlite3")
        for attr, line in facts(read(mods[name]))["calls"]:
            found.append(f"persistence: producer {name} calls .{attr}() (line {line})")

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

    for name in sorted(DOWNSTREAM & set(mods)):   # no source named in processing, deduplication, persistence or neutral code
        for word, line in facts(read(mods[name]))["words"]:
            found.append(f"source-knowledge: {name} mentions '{word}' (line {line})")

    for name in sorted({n for n in mods if is_new(n) and not is_producer(n)} - {COMPOSITION_ROOT, "service.cli", "service.producer", "service.repositories", "service.schema", "service.producers"}):
        for what, line in facts(read(mods[name]))["state"]:   # a producer's working state belongs to its producer
            found.append(f"producer-state: {name} reads {what} (line {line})")
    return found


class Real(unittest.TestCase):
    def test_the_real_tree_has_no_violation(self):
        self.assertEqual(violations(DISCOVERY), [])

    def test_the_graph_is_what_was_reviewed(self):
        g = graph(DISCOVERY)
        producers = {n for n in g if is_producer(n)}
        self.assertTrue({"service.producers.city_of_san_diego", "service.producers.localist"} <= producers)
        self.assertTrue(any(n.startswith("service.producers.craft_fairs") for n in producers))
        self.assertTrue({"service.producer", "service.service", "service.repositories", "service.extraction", "service.enrichment"} <= set(g))
        self.assertTrue(set(ALLOWED) - {"service"} <= set(g), "the reviewed layering names real modules only")
        for name in g:
            if is_producer(name):
                self.assertTrue(g[name]["inside"] - {n for n in g[name]["inside"] if is_producer(n)} <= PRODUCER_MAY_USE | {f"service.{n}" for n in PRODUCER_MAY_USE_SERVICE} | {"service"}, name)

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

    def test_a_new_producer_needs_only_its_own_package_and_the_root_wiring(self):
        """No module other than the composition root names a producer; the contract is a leaf."""
        g = graph(DISCOVERY)
        importers = {n for n, info in g.items() if any(is_producer(t) or t == "service.producers" for t in info["inside"]) and not is_producer(n)}
        self.assertEqual(importers - {n for n in g if not is_new(n)}, {COMPOSITION_ROOT})
        self.assertEqual(g["service.producer"]["inside"], set())


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
        for top in ("area_events", "tests", "scripts"):
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
                with open(os.path.join(root, *relative.split("/")), "a", encoding="utf-8") as handle:
                    handle.write("\n" + text + "\n")
            return violations(root)

    def assertCaught(self, edits, *rules):
        found = self.inject(edits)
        for rule in rules:
            self.assertTrue(any(v.startswith(rule) for v in found), f"{rule} did not fire; got {found}")

    def test_a_copy_of_the_real_tree_is_clean(self):
        self.assertEqual(self.inject({}), [])

    def test_a_producer_importing_a_legacy_source_reader(self):
        self.assertCaught({"service/producers/localist.py": "from ...sources import web"}, "new-to-legacy", "transitive-legacy", "producer-inputs")

    def test_a_producer_importing_the_legacy_engine_or_core(self):
        self.assertCaught({"service/producers/localist.py": "from ... import engine"}, "new-to-legacy", "transitive-legacy")
        self.assertCaught({"service/producers/city_of_san_diego.py": "from ...core import make_event"}, "new-to-legacy", "legacy-event-builders")

    def test_a_transitive_dependency_on_a_legacy_pipeline(self):
        self.assertCaught({"textloc.py": "from . import registry"}, "neutral-purity", "transitive-legacy")
        self.assertCaught({"media.py": "from . import workbench"}, "transitive-legacy")

    def test_a_producer_reaching_persistence(self):
        self.assertCaught({"service/producers/city_of_san_diego.py": "from ..repositories import CandidateRepository"}, "persistence-and-pipeline")
        self.assertCaught({"service/producers/localist.py": "import sqlite3\nconnection = sqlite3.connect('x')\nconnection.execute('DELETE FROM candidates')"}, "persistence: producer", "persistence: producer")

    def test_dependencies_between_unrelated_producers(self):
        self.assertCaught({"service/producers/city_of_san_diego.py": "from .localist import LocalistProducer"}, "producer-independence")
        self.assertCaught({"service/producers/localist.py": "from .craft_fairs import reading"}, "producer-independence")

    def test_a_downstream_module_depending_on_a_producer(self):
        self.assertCaught({"service/events.py": "from .producers import localist"}, "producers-wired-in-the-root-only")
        self.assertCaught({"service/extraction.py": "from .producers.city_of_san_diego import parse_listing"}, "producers-wired-in-the-root-only")

    def test_source_specific_code_in_generic_processing(self):
        self.assertCaught({"service/extraction.py": "WHICH = 'sandiego_gov'"}, "source-knowledge")
        self.assertCaught({"service/repositories.py": "def handle_localist(record):\n    return record"}, "source-knowledge")
        self.assertCaught({"textutil.py": "x = 'eventbrite'"}, "source-knowledge")

    def test_an_unreviewed_cross_layer_import(self):
        self.assertCaught({"service/repositories.py": "from . import extraction"}, "layering")
        self.assertCaught({"service/events.py": "from . import enrichment"}, "layering")
        self.assertCaught({"service/store.py": "from . import repositories"}, "layering")

    def test_an_import_by_name_at_run_time(self):
        self.assertCaught({"service/producers/localist.py": "import importlib\nlegacy = importlib.import_module('discovery.core')"}, "dynamic-import")

    def test_a_cycle(self):
        self.assertCaught({"service/store.py": "from . import repositories", "service/repositories.py": "from . import store"}, "cycle")

    def test_processing_reading_a_producers_working_state(self):
        self.assertCaught({"service/extraction.py": "def peek(record):\n    return record.state"}, "producer-state")
        self.assertCaught({"service/enrichment.py": "Q = 'SELECT state_json FROM candidates'"}, "producer-state")


if __name__ == "__main__":
    unittest.main()
