"""Stable error/status codes for tenancy-invariant gates.

These strings appear in three load-bearing places:
  1. HTTPException detail.code values that the frontend pattern-matches on
  2. CloudWatch metric dimension `reason` values for ops dashboards
  3. Structured log lines for incident investigation

Treat them as a stable contract — renaming requires coordinated frontend
+ dashboard updates. New codes are additive.
"""

# Gate A — invite-creation refused because invitee already has an active
# personal Lightbulb AI subscription. Returned by POST /api/v1/orgs/{id}/invitations.
PERSONAL_USER_EXISTS = "personal_user_exists"

# Gate B — personal trial-checkout refused because caller has a pending
# org invitation that should be accepted instead. Returned by
# POST /api/v1/billing/trial-checkout.
PENDING_ORG_INVITATION = "pending_org_invitation"
