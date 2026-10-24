# OBS Stream Info

Set your stream **title and category on Twitch and YouTube at once**, right from a dock inside OBS Studio. No browser dashboards, no extra apps, no context switching.

> **Status:** early development, nothing usable yet.

## Why

When you simulcast to Twitch and YouTube, updating the stream title means editing it in two dashboards. Existing all-in-one tools either moved this feature behind a subscription or route your stream through their servers. This project keeps everything local and inside OBS.

## How it works (planned)

- An **OBS Python script** starts a small local web server when OBS loads, and stops it when OBS closes.
- The UI is a **Custom Browser Dock** pointing to `http://localhost:<port>`.
- One-time login to Twitch and Google via **device code flow**. Tokens are stored locally and refresh automatically.
- **Apply** updates both platforms and shows a per-platform result (✓ or the error).

### Twitch
- Title and category via `PATCH /helix/channels` (scope `channel:manage:broadcast`)
- Category search with autocomplete via `/helix/search/categories`

### YouTube
- Title and description of the active or upcoming broadcast via `liveBroadcasts.update`
- Works with a separate YouTube output, for example from a multistream plugin

## Requirements (planned)

- OBS Studio 32+ on Windows
- Python 3 (64-bit, version matching your OBS build) configured under *Tools → Scripts → Python Settings*
- Your own Twitch application (public client) and Google Cloud project with the YouTube Data API v3 enabled

## Roadmap

1. Feasibility: OBS Python script serving a browser dock reliably
2. Twitch: login, title, category search
3. YouTube: login, title, timing around stream start
4. Polish: title templates, theming, optional chat with 7TV/BTTV/FFZ emotes

## License

[MIT](LICENSE)
