"""Constants for the Area Events integration."""

DOMAIN = "area_events"
CONF_URL = "url"
CONF_TOKEN = "token"

WS_EVENTS = "area_events/events"
CARD_URL = "/area_events/area-events-card.js"
PHOTOS_KEY = f"{DOMAIN}_photos"  # hass.data: the validated category-photograph index, or None

# How long the hub keeps refreshing a profile nobody has asked for. A card asks at least every five minutes,
# so a profile unasked for twice that long has no card any more (a removed card, an editor preview).
ACTIVE_SECONDS = 12 * 60
# A profile without events yet is asked for every few seconds by a card that is waiting for it.
UNPROVEN_SECONDS = 2 * 60
# How often a registration is repeated, so an engine that restarted or dropped the profile hears of it again.
REGISTER_TTL_SECONDS = 15 * 60
# Poll intervals: quick while discovery is under way, relaxed otherwise.
BUSY_INTERVAL_SECONDS = 5
IDLE_INTERVAL_SECONDS = 5 * 60
