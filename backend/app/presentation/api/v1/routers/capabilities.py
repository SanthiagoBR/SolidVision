"""What this installation can do, behind the loopback guard (RFC-031 section 9).

The UI has to know whether to draw *"open in Explorer"*. Today it cannot:
with `allow_local_file_actions` off, `POST /images/{id}/reveal` answers
**404 with the body of a route that does not exist** (RFC-030 section 6),
so discovering the answer by trying means a button that vanishes after
the user has clicked it.

**One guard, and the asymmetry is the design.** This route carries
`require_loopback_client` and *not*
`require_local_file_actions_enabled`, which is the kind of inconsistency
somebody tidies up by adding the second one. Adding it would break the
route: with the configuration off, the route would disappear -- and
"off" is precisely the answer the UI came here to get.

The loopback guard alone is what keeps the 404 honest. Its point is that
*a client probing the API learns nothing a wrong path would not tell it*;
a route announcing the configuration to anyone would undo half of that.
So whoever is on this machine -- the local UI, the only legitimate client
-- gets the answer, and anybody else gets a 403 and learns nothing about
how this server is configured.
"""

from __future__ import annotations

import sys

from fastapi import APIRouter, Depends, status

from app.infrastructure.config.settings import settings
from app.presentation.local_file_actions import require_loopback_client
from app.presentation.schemas.capabilities_schema import CapabilitiesSchema

router = APIRouter(tags=["capabilities"])


@router.get(
    "/capabilities",
    status_code=status.HTTP_200_OK,
    response_model=CapabilitiesSchema,
    summary="What this installation supports",
    dependencies=[Depends(require_loopback_client)],
    responses={
        403: {
            "description": (
                "The caller is not on this machine. The body carries no "
                "configuration at all."
            )
        }
    },
)
def get_capabilities() -> CapabilitiesSchema:
    """Report the three things a client draws differently because of.

    `local_file_actions` mirrors `settings.allow_local_file_actions`
    exactly, including when it is `False` -- a `200` saying "no" is the
    whole reason this route exists, and is why the configuration guard is
    not on it.

    `platform` is `sys.platform`, read here rather than inferred from
    anything: only the Windows volume adapter and the Windows file
    revealer ship, and a client is entitled to know which operating
    system answered (RFC-027 section 4.1).

    `version` is `settings.project_version` -- the package version, which
    is `0.1.0`. RFC-031 section 9's example shows `0.5.0`; that is the
    sprint number, and the sprint number is not the version of anything
    that ships. The setting is reported as it stands rather than the
    project being renumbered to match an illustration.

    The bar for a fourth field is *the UI draws something different
    because of it*. Without a criterion this becomes a configuration
    dump, which is the risk RFC-031 section 15 names.
    """
    return CapabilitiesSchema(
        local_file_actions=settings.allow_local_file_actions,
        platform=sys.platform,
        version=settings.project_version,
    )
