"""What the embedded cluster adapter raises (RFC-033).

Infrastructure failures, not `DomainError`s: the domain has no opinion about
`pg_ctl`. Every message says what was observed and, where there is one, what
to do -- these reach a photographer's screen through the launcher of RFC-036,
and a refusal that does not say why is a support call.
"""

from __future__ import annotations


class EmbeddedPostgresError(RuntimeError):
    """The embedded cluster could not be brought to the state asked of it."""


class RuntimeInvalidError(EmbeddedPostgresError):
    """The runtime directory is not one `scripts/vendor_postgres.py` produced.

    Raised for a missing manifest as much as for a missing binary: pointing
    the adapter at an arbitrary PostgreSQL installation -- EnterpriseDB's in
    `Program Files`, say -- fails here, explicitly, instead of working with
    another version (RFC-033 section 5.2).
    """


class ClusterRefusedError(EmbeddedPostgresError):
    """The cluster exists, and the adapter will not touch it.

    Another major version, a cluster held by another runtime, a password the
    cluster rejects, a schema newer than the code, facts that are not the
    ones RFC-033a measured. In every case the safe action is to do nothing,
    and the message says which of them it was.
    """


class StartupFailedError(EmbeddedPostgresError):
    """The server did not start, for a reason that retrying will not change.

    Carries what the server logged, which is where it says why (RFC-033
    section 8.1).
    """


class ServerNotReadyError(StartupFailedError):
    """The server is up and says it cannot take connections yet.

    The one refusal that waiting does change. `postmaster.pid` is rewritten
    when the server starts and when it stops, but **not** when it
    reinitializes after one of its own processes dies: for those seconds the
    file still says `ready`, and only a connection attempt is told "the
    database system is in recovery mode" (measured, RFC-033 section 18).
    """


class StopFailedError(EmbeddedPostgresError):
    """Neither the `fast` nor the `immediate` shutdown stopped the server.

    The adapter never kills the postmaster (RFC-033 section 8.5): deciding
    whether that is worth the orphans it leaves is the launcher's call.
    """
