"""Automated architecture checks on the actual module dependency graph of the Discovery Service.

Source -> Producer -> Standard Records -> Processing: producers are the only code that knows a source's format, processing consumes standard records without knowing where they came
from, and nothing downstream depends on a producer. These tests read the real imports (AST), not a diagram."""
import ast
import dataclasses
import os
import unittest

from discovery.service import producer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVICE = os.path.join(ROOT, "area_events", "discovery", "service")
COMPOSITION_ROOT = {"app"}                        # builds the objects and wires producers to sources; the only place that may import them
PRODUCER_MODULES = lambda name: name.startswith("producers.")   # noqa: E731
PRODUCER_MAY_USE = {"producer", "fetching", "urls"}              # the contract, the fetch client it is given, and URL identity
DOWNSTREAM = {"service", "repositories", "events", "extraction", "processing", "enrichment", "media_store", "reader", "api", "control", "store", "scheduler", "host", "areas",
              "text", "fetching", "contracts", "schema", "urls"}
SOURCE_WORDS = ("sandiego_gov", "ut_austin", "localist", "craft_fairs", "sd_craft_fairs", "sandiego.gov", "eventbrite", "sweetandquirky")
LEGACY_EVENT_BUILDERS = {"make_event", "finish_event"}


def modules():
    found = {}
    for folder, _, files in os.walk(SERVICE):
        if "__pycache__" in folder:
            continue
        for name in files:
            if name.endswith(".py"):
                path = os.path.join(folder, name)
                rel = os.path.relpath(path, SERVICE)[:-3].replace(os.sep, ".")
                found[rel[:-9] if rel.endswith(".__init__") else rel] = path
    return found


def parse(path):
    with open(path, encoding="utf-8") as handle:
        return ast.parse(handle.read())


def graph():
    """{module: {"inside": set of service modules it imports, "outside": [(package-relative module, imported names)]}} from the real import statements."""
    result = {}
    for name, path in modules().items():
        base = name.split(".") if path.endswith("__init__.py") else name.split(".")[:-1]
        inside, outside = set(), []
        for node in ast.walk(parse(path)):
            if not isinstance(node, ast.ImportFrom) or not node.level:
                continue
            if node.level - 1 > len(base):   # reaches out of the service package: ..core, ..sources, ...
                outside.append((node.level - len(base) - 1, node.module or "", [a.name for a in node.names]))
                continue
            up = base[:len(base) - (node.level - 1)]
            if node.module:
                inside.add(".".join(up + [node.module]))
            else:
                inside.update(".".join(up + [a.name]) for a in node.names)
        result[name] = {"inside": inside, "outside": outside}
    return result


def sources(info):
    """The imports that reach a source's reading code (the `sources` package)."""
    return [o for o in info["outside"] if o[1].split(".")[0] == "sources"]


class Dependencies(unittest.TestCase):
    def setUp(self):
        self.graph = graph()

    def test_the_graph_is_what_was_reviewed(self):
        self.assertTrue({"producer", "service", "repositories", "extraction", "enrichment", "producers.city_of_san_diego", "producers.localist", "producers.craft_fairs"} <= set(self.graph))
        self.assertNotIn("adapters", self.graph, "the earlier adapter module is gone")
        self.assertNotIn("acquisition", self.graph)

    def test_only_the_composition_root_imports_a_producer(self):
        for name, info in self.graph.items():
            if name in COMPOSITION_ROOT or PRODUCER_MODULES(name) or name == "producers":
                continue
            self.assertFalse([d for d in info["inside"] if d == "producers" or PRODUCER_MODULES(d)], f"{name} depends on a producer")

    def test_processing_and_persistence_do_not_depend_on_any_source_reading_code(self):
        for name in DOWNSTREAM:
            self.assertFalse(sources(self.graph[name]), f"{name} imports source reading code")

    def test_a_producer_depends_on_the_contract_and_its_inputs_only(self):
        for name, info in self.graph.items():
            if PRODUCER_MODULES(name):
                self.assertLessEqual({d for d in info["inside"] if not PRODUCER_MODULES(d)} - {"producers"}, PRODUCER_MAY_USE, f"{name} reaches into the pipeline")

    def test_the_contract_is_a_leaf(self):
        info = self.graph["producer"]
        self.assertEqual((info["inside"], [o for o in info["outside"] if o[1].split(".")[0] in ("sources", "core")]), (set(), []), "the contract imports nothing of the service or of any source")

    def test_nothing_imports_the_legacy_event_builders(self):
        for name, info in self.graph.items():
            used = {n for _, module, names in info["outside"] if module == "core" for n in names} & LEGACY_EVENT_BUILDERS
            self.assertFalse(used, f"{name} builds legacy event dictionaries ({used})")

    def test_the_dependency_graph_has_no_cycles(self):
        visiting, done = set(), set()

        def visit(node, path):
            if node in done:
                return
            self.assertNotIn(node, visiting, f"cycle: {' -> '.join(path + [node])}")
            visiting.add(node)
            for other in self.graph.get(node, {}).get("inside", ()):
                if other in self.graph:
                    visit(other, path + [node])
            visiting.discard(node)
            done.add(node)

        for name in self.graph:
            visit(name, [])


class NoLegacyPath(unittest.TestCase):
    def python_files(self):
        for top in ("area_events", "tests", "scripts"):
            for folder, _, files in os.walk(os.path.join(ROOT, top)):
                if "__pycache__" in folder:
                    continue
                for name in files:
                    if name.endswith(".py"):
                        yield os.path.join(folder, name)

    def test_the_earlier_adapter_result_and_its_conversion_paths_are_gone(self):
        banned = ("AdapterResult", "from_event_dict", "to_payload", "from_events", "discovery.service.adapters", "from .adapters", "payloads(connection")
        here = os.path.abspath(__file__)
        for path in self.python_files():
            if os.path.abspath(path) == here:
                continue
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            for word in banned:
                if word in text:
                    self.fail(f"{os.path.relpath(path, ROOT)} still uses {word}")

    def test_no_compatibility_object_stands_in_for_the_removed_one(self):
        """The service-level modules define no class or function that converts event dictionaries into candidate records."""
        for name, path in modules().items():
            for node in ast.walk(parse(path)):
                if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                    self.assertFalse(any(word in node.name.lower() for word in ("legacy", "compat", "from_dict", "from_event")), f"{name}.{node.name}")


class StandardRecordsAreClosed(unittest.TestCase):
    def test_the_records_have_exactly_the_documented_fields(self):
        self.assertEqual([f.name for f in dataclasses.fields(producer.SourceLead)],
                         ["url", "kind", "decision", "reasons", "score", "found_on", "link_text", "events", "fetched", "schema"])
        self.assertEqual([f.name for f in dataclasses.fields(producer.ProducerResult)], ["candidates", "leads", "notes", "schema"])
        self.assertEqual([f.name for f in dataclasses.fields(producer.Provenance)], ["source_name", "listing_url", "retrieved_at", "found_on"])
        self.assertEqual([f.name for f in dataclasses.fields(producer.Verification)], ["status", "evidence", "missing", "note", "verified"])

    def test_the_stored_document_is_the_records_standard_fields_and_nothing_else(self):
        stored = producer.document(producer.EventCandidate(local_id="a", title="t", starts_at="2030-01-01T00:00:00+00:00", state={"anything": 1}))
        standard = {f.name for f in dataclasses.fields(producer.EventCandidate)} - {"state"}
        self.assertEqual(set(stored), standard)
        self.assertNotIn("anything", repr(stored))


class ProducerStateStaysWithTheProducer(unittest.TestCase):
    ALLOWED = {"producer", "repositories", "schema"}

    def test_no_processing_module_reads_a_producers_working_state(self):
        for name, path in modules().items():
            if name in self.ALLOWED or PRODUCER_MODULES(name):
                continue
            for node in ast.walk(parse(path)):
                if isinstance(node, ast.Attribute) and node.attr == "state":
                    self.fail(f"{name} reads .state (line {node.lineno})")
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and "state_json" in node.value:
                    self.fail(f"{name} reads state_json (line {node.lineno})")

    def test_the_only_readers_of_state_hand_it_back_to_the_producer_unread(self):
        with open(os.path.join(SERVICE, "repositories.py"), encoding="utf-8") as handle:
            text = handle.read()
        self.assertEqual(text.count("state_json"), 5, "stored beside the record, written on every run, returned as `previous`: nothing else")


class NoSourceKnowledgeDownstream(unittest.TestCase):
    def test_processing_deduplication_and_persistence_name_no_source(self):
        for name in DOWNSTREAM:
            with open(modules()[name], encoding="utf-8") as handle:
                text = handle.read()
            for word in SOURCE_WORDS:
                if word in text.replace("localist-images", ""):
                    self.fail(f"{name} knows about '{word}'")


if __name__ == "__main__":
    unittest.main()
