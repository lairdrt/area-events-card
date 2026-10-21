# Area Events

Finds local events for the **Area Events** integration and its dashboard cards. It runs on the Home Assistant host (Raspberry Pi 4 included) and needs no other machine.

## Setup

1. Start the app. Its log says `Area Events engine listening` and `announced ... to Home Assistant`.
2. In Settings > Devices & services, select the discovered **Area Events** and Submit. There is nothing to type: the app created a secret token and handed it to Home Assistant itself.
3. Add the **Area Events Card** to a dashboard and set its area.

The app publishes no port; only Home Assistant and other apps on its internal network can reach it, and every request needs the token. If the integration cannot be discovered (the app runs on another machine), set the optional **Token** option to 32 or more characters and add the integration by hand with the app's address and that token.

## What it does

Each distinct card configuration (area, radius and source websites) is a *profile*. Profiles are discovered one after another, never in parallel; cards that share a profile share its results. Results, caches, the token and settings live in the app's persistent storage (`/data`), so a restart or update keeps them. A profile that nobody has asked for in two weeks is deleted.

The first search for a new area can take a few minutes. A failed refresh never removes events that were already found.

## Source websites

A card may name websites to read. Only some can be read: the sources built into the app (see the project documentation) and any public calendar that runs on **Localist**. Other websites are reported back to the card as unsupported and are not fetched. Without named websites the app uses the built-in sources whose coverage overlaps the card's area; today those are in the San Diego, California area and at the University of Texas at Austin, so other areas will report that no source applies until adapters are added.

## Options

| Option | Meaning |
|---|---|
| Refresh interval | Minutes between rediscoveries of an active area (15 to 1440, default 60). |
| Log level | debug, info, warning or error. |
| Token (optional) | Only for connecting the integration by hand; 32 or more characters. Leave empty normally. |

## Troubleshooting

- *The integration is not offered*: restart the app so it announces itself again, and check the integration is installed.
- *The integration says the token was rejected*: restart the app; Home Assistant updates the entry by itself.
- *No events for my area*: read the card's notice. It names the reason (for example, no built-in source covers the area).
