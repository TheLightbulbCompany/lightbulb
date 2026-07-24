"""Generation-id minting helpers for the two onboarding/provisioning fences.

See ``docs/superpowers/specs/2026-07-24-onboarding-architecture-design.md``
§2 ("Two generation ids, each minted at exactly one code point"). Two
independently-scoped ULIDs fence different classes of facts so a
container-only recreation can never invalidate onboarding truth (and vice
versa):

- ``provision_generation`` (``containers`` row, PK ``owner_id``) -- the
  container-incarnation fence. Minted exactly once, at event **G1**, inside
  ``EcsManager.create_user_service``'s row-creating write. Preserved by
  scale-from-zero, force-redeploy, restart, provider hot-swap, Bedrock
  revoke/restore, and Stripe conversion -- see INV-2.
- ``onboarding_generation`` (the ``paperclip-companies`` OWNER row --
  co-located, per the 2026-07-24 owner decision, NOT a dedicated table --
  see ``docs/superpowers/specs/2026-07-24-onboarding-architecture-design.md``
  §2's "load-bearing safety requirement") -- the company-incarnation fence.
  Minted exactly once, at event **G2**, inside
  ``paperclip_provisioning.provision_org``'s fresh-create branch -- NOT on
  reconcile-adopt of an existing ``company_id`` and NOT on
  ``provision_member`` (an org seat) -- see INV-3.

As of the ONBOARD-TABLE PR, ``bump_onboarding_generation`` is wired into
``provision_org``'s row-creating write (the SAME ``PaperclipRepo.put`` that
persists the freshly-confirmed company), and onboarding facts on that row
are written through ``PaperclipRepo``'s generation-conditioned ``UpdateItem``
methods (``claim_funnel_submission`` / ``record_onboarding_ids`` /
``revert_funnel_submission``) -- see that module for INV-4 enforcement.
"""

from ulid import ULID


def new_provision_generation() -> str:
    """Mint a fresh ``provision_generation`` ULID.

    Call this from EXACTLY ONE place: ``EcsManager.create_user_service``'s
    row-creating write (event G1). See INV-2. Every other containers-row
    write path must leave an existing ``provision_generation`` untouched.
    """
    return str(ULID())


def bump_onboarding_generation() -> str:
    """Mint a fresh ``onboarding_generation`` ULID.

    Call this from EXACTLY ONE place: ``provision_org``'s fresh-create
    branch (event G2), right after a genuinely-new Paperclip company is
    confirmed (``create_company`` actually ran -- never on reconcile-adopt
    of an existing ``company_id``) and never on a seat-only
    ``provision_member``. See INV-3.
    """
    return str(ULID())
