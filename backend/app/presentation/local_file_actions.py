"""The two guards in front of anything that acts on the user's machine (RFC-030).

`POST /api/v1/images/{id}/reveal` starts a process on the computer the API
runs on. RFC-030 section 2.1 dropped RFC-026's objection to publishing file
paths *because the deployment is local*, and a condition a security
decision rests on has to be checked when a request arrives, not assumed
when the code is read. Hence two guards, and they are independent on
purpose:

* **Guard 1 -- configuration.** `settings.allow_local_file_actions`,
  `False` unless the operator turns it on. The operator's intention.
* **Guard 2 -- loopback.** The caller's address must be a loopback one,
  whatever the setting says. A fact about who is calling, which still
  holds for an operator who started the API on `0.0.0.0` without noticing
  -- precisely the deployment in which RFC-026's objection comes back.

Neither substitutes for the other. The setting alone does not protect an
API exposed to the network; the loopback check alone is satisfied by
anything that tunnels to localhost, and does not say the operator wanted
this at all.

Both are FastAPI dependencies declared on the route decorator, which
FastAPI resolves before the endpoint's own parameters. A refused request
therefore never constructs the use case, never opens a database session,
and never enumerates a volume.
"""

from __future__ import annotations

import ipaddress

from fastapi import HTTPException, Request, status

from app.infrastructure.config.settings import settings


def require_local_file_actions_enabled() -> None:
    """Guard 1: answer exactly as a route that does not exist would.

    404 with FastAPI's own `{"detail": "Not Found"}`, not 403. RFC-030
    section 6: with the setting off, the route does not exist rather than
    existing and refusing -- a client probing the API learns nothing from
    it that an unknown path would not tell it.
    """
    if not settings.allow_local_file_actions:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")


def require_loopback_client(request: Request) -> None:
    """Guard 2: refuse any caller whose address is not a loopback address.

    403, because at this point the route does exist -- the operator turned
    it on -- and the caller is the problem. The address is the one the ASGI
    server reports for the connection. A reverse proxy on the same machine
    makes every caller look local unless the server is told to trust its
    forwarding headers, which is one of the reasons guard 1 exists at all.

    A request with no client address at all is refused: "unknown" is not
    "local".
    """
    client = request.client
    if client is None or not is_loopback_address(client.host):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Local file actions are only available from this machine.",
        )


def is_loopback_address(host: str) -> bool:
    """Whether `host` is a literal loopback IP address, IPv4 or IPv6.

    Only literal addresses count. `localhost` is a name, and a name is
    whatever a resolver says it is; ASGI servers report the peer's address,
    so a name here means something upstream rewrote it.

    An IPv4-mapped IPv6 address -- `::ffff:127.0.0.1`, which a dual-stack
    socket reports for an IPv4 loopback caller -- is judged by the IPv4
    address inside it. `ipaddress` does not do that for `is_loopback` on
    every Python version this could run on, and getting it wrong would
    refuse a legitimate local caller.
    """
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return address.ipv4_mapped.is_loopback
    return address.is_loopback
