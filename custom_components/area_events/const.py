"""Constants for the Area Events integration."""

DOMAIN = "area_events"
CONF_URL = "url"
CONF_TOKEN = "token"

WS_EVENTS = "area_events/events"
CARD_URL = "/area_events/area-events-card.js"

# How long the hub keeps refreshing a profile nobody has asked for.
ACTIVE_SECONDS = 30 * 60
# How often a registration is repeated, so an engine that restarted or dropped the profile hears of it again.
REGISTER_TTL_SECONDS = 15 * 60
# Poll intervals: quick while discovery is under way, relaxed otherwise.
BUSY_INTERVAL_SECONDS = 15
IDLE_INTERVAL_SECONDS = 5 * 60
