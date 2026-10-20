const areaEventsCardLoadTimestamp = Date.now();

import(
  `/local/area-events-card/area-events-card.js?ts=${areaEventsCardLoadTimestamp}`
)
  .catch((error) => {
    console.error("Failed to load area events card:", error);
  });
