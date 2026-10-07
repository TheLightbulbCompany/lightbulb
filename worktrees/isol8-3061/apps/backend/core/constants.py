"""Application-wide constants."""

# Actor ID for system-initiated actions (e.g., Clerk webhook processing) in audit logs.
SYSTEM_ACTOR_ID = "__system__"

# Marketplace seller whose published listings populate the chat sidebar gallery.
# admin@isol8.co publishes through the same /marketplace/listings flow as any
# other seller — there is no special "is_official" flag. The sidebar's
# /featured-listings endpoint resolves this email to the seller's Clerk
# user_id at startup (via core.services.curator) and filters by that.
# DO NOT RENAME: resolved against the live Clerk user list at startup — this is
# an existing account's address, not a label.
ISOL8_CURATOR_EMAIL = "admin@isol8.co"

# The channel providers the platform knows about. Surfaced by /platform/info and
# used to classify a marketplace listing's requirements. Three strings, no runtime
# behaviour — it outlived the OpenClaw config generator that used to own it.
CHANNEL_PROVIDER_IDS = ("telegram", "discord", "slack")
