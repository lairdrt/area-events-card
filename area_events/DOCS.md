# Area Events

Finds local events for the **Area Events** integration and its dashboard cards. It runs on the Home Assistant host (Raspberry Pi 4 included) and needs no other machine.

## Setup

1. Set a **Token** in the Configuration tab: any random secret of at least 32 characters (for example, the output of a password generator). The app does not start without one.
2. Start the app. Its log says `Area Events engine listening`.
3. Add the **Area Events** integration (Settings > Devices & services > Add integration). It asks for:
   - the app address: `http://` + the **Hostname** shown on this app's Info tab + `:8765` (it looks like `http://abc12345-area-events:8765`);
   - the same token.
4. Add the **Area Events** card to a dashboard and set its area.

The app publishes no port; only Home Assistant and other apps on its internal network can reach it, and every request needs the token.

## What it does

Each distinct card configuration (area, radius and source websites) is a *profile*. Profiles are discovered one after another, never in parallel; cards that share a profile share its results. Results, caches and settings live in the app's persistent storage (`/data`), so a restart or update keeps them. A profile that nobody has asked for in two weeks is deleted.

The first search for a new area can take a few minutes. A failed refresh never removes events that were already found.

## Source websites

A card may name websites to read. Only some can be read: the sources built into the app (see the project documentation) and any public calendar that runs on **Localist**. Other websites are reported back to the card as unsupported and are not fetched. Without named websites the app uses the built-in sources whose coverage overlaps the card's area; today those are in the San Diego, California area and at the University of Texas at Austin, so other areas will report that no source applies until adapters are added.

## Options

| Option | Meaning |
|---|---|
| Token | Shared secret, at least 32 characters. |
| Refresh interval | Minutes between rediscoveries of an active area (15 to 1440, default 60). |
| Log level | debug, info, warning or error. |

## Troubleshooting

- *The integration cannot connect*: check the hostname on the Info tab and that the app is started.
- *The integration says the token was rejected*: the token in the app's configuration changed; use the integration's reauthentication.
- *No events for my area*: read the card's notice. It names the reason (for example, no built-in source covers the area).
