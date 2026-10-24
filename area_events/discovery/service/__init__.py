"""The Discovery Service: finds event material and keeps it in its own SQLite database (discovery.db).

    SOURCE PRODUCERS -> STANDARD RECORDS -> EVENT PROCESSING -> DEDUPLICATION <-> PERSISTENCE (one database) -> PRESENTATION

    producers/       the only code that knows a source's format; each producer answers with standard records (wired in app.py, imported nowhere else)
    producer.py      the producer-to-processing interface: EventCandidate, SourceLead, ProducerResult and the one validation boundary
    service.py       DiscoveryService: schedules and runs producers' jobs and hands what they find to Persistence
    extraction.py    the worker that extracts an event from a stored candidate (no database)
    processing.py    event processing: the Processing contract, validation and normalization into a clean event (no database)
    deduplication/   the matching rules behind one interface: decide whether a clean event is one already known (no database); strategies are interchangeable
    persistence/     EVERYTHING that touches SQLite: the database, its schema, repositories and the port implementations; the rest of the service sees only persistence/ports.py
    control.py       the control plane's commands: validation, then Persistence
    scheduler.py     pacing and planning policy (pure); Persistence applies it
    events.py        the events contract and its query service
    contracts.py     records, errors and protocols shared by all of the above (transport independent)
    enrichment.py    media enrichment; urls.py, fetching.py, text.py: neutral helpers
    api.py, cli.py   the HTTP API and the command line
    app.py           the composition root: the one place that creates the objects, chooses the deduplication strategy and wires producers to sources

Nothing here is a module-level mutable object: every piece of state belongs to an instance created by `app.build`.
"""
