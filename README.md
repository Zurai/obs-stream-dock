# OBS Stream Dock

Set your stream **title and category on Twitch and YouTube at once** and read and write **both chats in one combined feed**, right from docks inside OBS Studio. No browser dashboards, no extra apps, no context switching.

> **Status:** early development. Stream info works on Twitch and YouTube; the combined chat is new and being tested.

Project page: [zurai.github.io/obs-stream-dock](https://zurai.github.io/obs-stream-dock/) · [Privacy policy](https://zurai.github.io/obs-stream-dock/privacy.html)

## Why

When you simulcast to Twitch and YouTube, updating the stream title means editing it in two dashboards. Existing all-in-one tools either moved this feature behind a subscription or route your stream through their servers. This project keeps everything local and inside OBS.

## How it works

- An **OBS Python script** starts a small local web server when OBS loads, and stops it when OBS closes.
- The UI is a **Custom Browser Dock** pointing to `http://127.0.0.1:47823/`.
- One-time login to Twitch and Google via **device code flow**. Tokens are stored locally and refresh automatically.
- **Apply** updates both platforms and shows a per-platform result (✓, ✗ or ⏳ pending).

### Twitch
- Title and category via `PATCH /helix/channels` (scope `channel:manage:broadcast`)
- Category search with autocomplete via `/helix/search/categories`

### Combined chat
- A second dock shows Twitch and YouTube chat in one feed, each message marked with its platform
- Twitch chat is read over Twitch's anonymous IRC WebSocket; sending uses `POST /helix/chat/messages` (scope `user:write:chat`)
- YouTube chat is read with `liveChatMessages.streamList`, a gRPC server stream that costs 5 quota units per connection instead of polling. It runs in a small helper process so gRPC can never take OBS down. Sending uses `liveChatMessages.insert` (50 units per message)
- Twitch emotes plus 7TV, BTTV and FFZ emotes of your channel
- **Emote picker** (☺ next to the input, grouped by channel and global, with search) and **`:` autocomplete**: type `:pog` and pick `Pog` with the arrow keys and Tab/Enter. It covers 7TV, BTTV, FFZ and every Twitch emote you may use, including subs on other channels (`GET /helix/chat/emotes/user`, scope `user:read:emotes`)
- Super Chats, memberships and gifts are highlighted; deleted messages and banned users are struck through
- The chat is meant for **you**: showing a merged chat on the Twitch stream itself conflicts with Twitch's simulcasting rules

### YouTube
- Title and optional description of the live or upcoming broadcast via `liveBroadcasts.update` (falls back to `videos.update`)
- If no broadcast exists yet, the title is kept and applied automatically once the broadcast is live (checked every 30 s while OBS is streaming)
- Works with a separate YouTube output, for example from a multistream plugin
- YouTube's API has no game field; titles are limited to 100 characters

## Requirements

- OBS Studio 32+ on Windows
- 64-bit Python 3.12 (OBS supports 3.6–3.12), plus `grpcio` and `protobuf` for the YouTube chat (`requirements.txt`)
- Your own Twitch application and Google Cloud project (see below)

## Setup

1. Install 64-bit Python 3.12 and select its folder in OBS under *Tools → Scripts → Python Settings*. For the YouTube chat, run `python -m pip install -r requirements.txt` with that Python.
2. **Twitch:** register an application in the [Twitch Developer Console](https://dev.twitch.tv/console) (client type **Public**, redirect URL `http://localhost`). Copy its client ID.
3. **YouTube:** in the [Google Cloud Console](https://console.cloud.google.com/), create a project, enable the **YouTube Data API v3**, set up the Google Auth Platform (audience *External*, add yourself as a test user or publish the app) and create an OAuth client of type **TVs and Limited Input devices**. Copy client ID and client secret.
   - While the app is in *Testing* mode, Google expires the login after 7 days.
4. Copy `config.example.json` to `config.local.json` next to the script and fill in the IDs and the secret. This file is ignored by git.
5. In OBS, add `obs_stream_dock.py` under *Tools → Scripts*.
6. Add two docks under *Docks → Custom Browser Docks*: `http://127.0.0.1:47823/` (stream info) and `http://127.0.0.1:47823/chat.html` (chat).
7. Log in with Twitch and YouTube in the stream info dock and confirm the codes in your browser.
8. The YouTube chat connects automatically when you start streaming and the broadcast is live, or with *connect* in the chat dock.

Tokens are stored in `data/` next to the script, which is also ignored by git.

## Roadmap

1. ~~Feasibility: OBS Python script serving a browser dock reliably~~
2. ~~Twitch: login, title, category search~~
3. YouTube: login, title, timing around stream start
4. Polish: title templates, theming
5. Combined chat: read and write Twitch and YouTube in one dock, with 7TV/BTTV/FFZ emotes

## License

[MIT](LICENSE)
