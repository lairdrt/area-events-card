"""The Discovery Service: finds event material and keeps it in its own SQLite database (discovery.db).

    contracts.py     records and protocols shared with consumers (transport independent, schema "discovery.v1")
    urls.py          canonical URL identity
    schema.py        the database schema as ordered migrations
    store.py         DiscoveryStore: the database file, its connections and transactions
    repositories.py  explicit SQL per entity: sources, pages, candidates, evidence, jobs, changes, consumers
    fetching.py      RecordingFetcher: the policy-bound fetch client an adapter uses, recording every page it reads
    producer.py      the standard producer-to-processing interface: EventCandidate, SourceLead, ProducerResult and the one validation boundary
    producers/       the only code that knows a source's format; each producer answers with standard records (wired in app.py, imported nowhere else)
    service.py       DiscoveryService: register sources, schedule and run jobs, store results under a lease
    reader.py        LocalDiscoveryReader: the read contract other components use (changes, snapshot, queries)
    app.py           the composition root that creates and connects the objects
    cli.py           run / status / inspect

Nothing here is a module-level mutable object: every piece of state belongs to an instance created by `app.build`.
"""
