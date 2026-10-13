"""Operation admission for workflow roles.

Roles are application roles, not approval to publish, train or change model authority. The role
always comes from the server-side Context; a client-supplied role is never read.
"""

from __future__ import annotations

from .contracts import fail

POLICY_REVISION = "science-workflow-roles.v1"
GRANTS = {
    "viewer": frozenset({"read"}),
    "reviewer": frozenset({"read", "review", "effort"}),
    "operator": frozenset({"read", "review", "effort", "register", "compute"}),
}


def require(context, grant: str) -> None:
    """Refuse an operation the context's role does not grant. Unknown roles grant nothing."""

    if grant not in GRANTS.get(context.role, frozenset()):
        fail("FORBIDDEN", "this role does not permit the operation")
