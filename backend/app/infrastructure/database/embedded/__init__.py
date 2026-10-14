"""The embedded PostgreSQL cluster: an adapter that owns its lifecycle (RFC-033).

Locates the vendored binaries, creates the cluster on first run, starts it
or adopts one already up, chooses the port, recovers from a dirty shutdown,
prepares the database and stops it. It also answers, without starting
anything, where a cluster that is up can be reached: `locate()` (RFC-034
section 7.2). It does not decide who calls it -- the launcher of RFC-036.

There is deliberately no port in the Domain (RFC-033 section 4.1): no use
case starts a database. The consumer is a composition root -- today the CLI
in `__main__`, later the launcher -- and, for the same reason, no class here
reads `settings`. They take paths in their constructors, and only `__main__`
reads the `Settings`. The two paths of Alembic come from the layout, which
is the geometry of directories and not configuration (RFC-034 section 4.1).

Nothing in this package imports `app.infrastructure.persistence.engine`. The
dependency runs the other way: with the embedded source, the engine asks
this package for the port and the password at every new connection.
"""

from __future__ import annotations
