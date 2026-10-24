"""Source producers: the only code that knows a source's format. Each module reads one kind of source through the fetcher it is given and answers with
`producer.ProducerResult` of standard `EventCandidate` and `SourceLead` records. Nothing outside this package, except the composition root (`service/app.py`), imports it."""
