"""Application-wide constants."""

# Actor ID for system-initiated actions (e.g., Clerk webhook processing) in audit logs.
SYSTEM_ACTOR_ID = "__system__"

# Marketplace seller whose published listings populate the chat sidebar gallery.
# admin@isol8.co publishes through the same /marketplace/listings flow as any
# other seller — there is no special "is_official" flag. The sidebar's
# /featured-listings endpoint resolves this email to the seller's Clerk
# user_id at startup (via core.services.curator) and filters by that.
ISOL8_CURATOR_EMAIL = "admin@isol8.co"
