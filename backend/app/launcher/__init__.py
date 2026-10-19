"""The launcher: one command that starts, watches and stops the backend (RFC-036).

    python -m app.launcher                 start everything and supervise it
    python -m app.launcher --supervised    the same, and stop when stdin ends
    python -m app.launcher status          what is up, and where
    python -m app.launcher stop            ask the launcher that is up to stop

A process without a window and without `torch`, one per data root, that knows
how to start, watch and stop the other three -- the cluster, the API and the
job runner -- and nothing else: it serves no HTTP, loads no model, and imports
neither `torch`, `transformers`, `fastapi` nor `uvicorn`
(`tests/test_launcher_imports_architecture.py` keeps it so). Whoever starts it
keeps it alive: a terminal, or a window that holds its stdin (section 4.1).

A composition root, like `app.cli`. Nothing in the Domain or the Application:
supervising processes is deployment, like the life cycle of the cluster
(RFC-033 section 4.1).

    module          responsibility
    __main__        the commands; the one module that reads the `Settings`
    supervisor      the state machine: the start, ready, restarts, the stop
    children        one child: creation, stdin, the readers of its output
    instance        the lock, the state file, the stop event
    events          the protocol on stdout
    session_end     the hidden window of the logoff
"""
