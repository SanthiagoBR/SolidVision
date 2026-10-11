"""The embedded PostgreSQL cluster: an adapter that owns its lifecycle (RFC-033).

Locates the vendored binaries, creates the cluster on first run, starts it
or adopts one already up, chooses the port, recovers from a dirty shutdown,
prepares the database and stops it. It decides neither who calls it -- the
launcher of RFC-036 -- nor how `Settings` works outside a checkout, which is
RFC-034.

There is deliberately no port in the Domain (RFC-033 section 4.1): no use
case starts a database. The consumer is a composition root -- today the CLI
in `__main__`, later the launcher -- and, for the same reason, no class here
reads `settings`. They take paths in their constructors, and only `__main__`
reads the `Settings`.

Nothing in this package imports `app.infrastructure.persistence.engine`:
importing it would build an engine pointed at the `.env` before the cluster
exists.
"""

from __future__ import annotations
