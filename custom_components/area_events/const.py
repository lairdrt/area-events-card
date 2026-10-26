"""Constants for the Area Events integration."""

DOMAIN = "area_events"
CONF_URL = "url"
CONF_READ_TOKEN = "read_token"
CONF_CA_FILE = "ca_file"               # optional: a certificate file to trust for an https address with a private certificate
CONF_CONTROL_TOKEN = "control_token"   # optional: without it Home Assistant can read events but not change shared Discovery settings
CONF_SCAN_INTERVAL = "scan_interval"   # seconds between change-feed checks: one per integration, however many cards there are

DEFAULT_SCAN_INTERVAL = 10
MIN_SCAN_INTERVAL = 5
MAX_SCAN_INTERVAL = 300

WS_AREAS = f"{DOMAIN}/areas"
WS_STATUS = f"{DOMAIN}/status"
WS_EVENTS = f"{DOMAIN}/events"          # one page, straight from the authoritative store
WS_SUBSCRIBE = f"{DOMAIN}/subscribe"    # the live view of one area, pushed when it changes
WS_EVENT = f"{DOMAIN}/event"
WS_AREA_CREATE = f"{DOMAIN}/area_create"
WS_AREA_UPDATE = f"{DOMAIN}/area_update"
WS_SETTINGS = f"{DOMAIN}/settings"
WS_PAUSE = f"{DOMAIN}/pause"
WS_RESUME = f"{DOMAIN}/resume"

WS_MANAGEMENT = f"{DOMAIN}/management"
WS_SOURCE_COMMAND = f"{DOMAIN}/source_command"

MANAGEMENT_URL = "/area_events/manager.js"
MANAGEMENT_PANEL_PATH = "area-events-manage"

CARD_URL = "/area_events/area-events-card.js"
PRESENTATION_URL = "/area_events/presentation"
PHOTOS_KEY = f"{DOMAIN}_photos"  # hass.data: the validated category-photograph index, or None
