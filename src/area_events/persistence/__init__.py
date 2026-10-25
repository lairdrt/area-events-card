"""Persistence: the one SQLite database and everything that touches it. Nothing outside this package opens a connection, runs SQL or holds a transaction; the rest of the
service talks to it only through the narrow ports in `ports.py`. This module is deliberately empty so that importing a port does not load a database implementation."""
