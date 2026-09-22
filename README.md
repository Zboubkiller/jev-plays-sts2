# Jev plays Slay the Spire 2

A bridge that lets [Jev](https://typesafe.ai) — TypeSafe's classification-only
"System One" model — play Slay the Spire 2 by itself. No GPT-4o, no Claude,
no text generation at all. Every decision (which card to play, which map
node to take, whether to buy something) is a single fast classification
call, usually a couple hundred ms and a fraction of a cent.

This was thrown together fast for a stream. It's not a tuned bot — it made
it to the first boss on its first real run and died there. It's running on
generic deckbuilding heuristics plus a snapshot of a community tier list,
not deep game knowledge. Expect bad calls sometimes. PRs welcome if you want
to make it actually good.

## How it works

1. [STS2MCP](https://github.com/Gennadiyev/STS2MCP) (a Slay the Spire 2 mod)
   exposes the live game state over a local HTTP API and accepts actions
   back the same way.
2. `bridge.py` polls that API, turns the current screen (combat, map, shop,
   card reward, etc.) into a plain-text description plus a list of legal
   options, and asks Jev a single `Choice` question: which option is best.
3. Jev's answer gets translated back into an actual game action and sent to
   STS2MCP.
4. Repeat every few seconds until you stop it.

An optional built-in web server (`http://localhost:8934/`) shows Jev's live
decisions, confidence, and top alternatives — meant to be added as an OBS
Browser Source if you're streaming this.

## Setup

### 1. Get a Jev API key

Sign up at [typesafe.ai](https://typesafe.ai) and grab an API key for the
System One / Jev product. Set it as an environment variable:

```powershell
# Windows PowerShell
$env:JEV_API_KEY = "your-key-here"
```
```bash
# macOS / Linux
export JEV_API_KEY="your-key-here"
```

### 2. Install the mod

Copy the three files from this repo's `mods/` folder into your Slay the
Spire 2 install's `mods/` directory:

- Windows (Steam default): `C:\Program Files (x86)\Steam\steamapps\common\Slay the Spire 2\mods\`
- Your actual path may differ if you use a custom Steam library.

Only `STS2_MCP.dll` and `STS2_MCP.json` need to go in `mods/` — the license
and patch notes files are just documentation, keep them wherever.

Launch the game, open Settings, enable mods, restart if prompted. See
`mods/PATCH_NOTES.md` for what's different about this build vs. the
upstream mod, and why.

### 3. Verify the mod is running

With the game open, visit `http://localhost:15526/api/v1/singleplayer` in a
browser. You should see JSON, not a connection error.

### 4. Run the bridge

```bash
pip install --upgrade pip  # no extra dependencies needed, stdlib only
python bridge.py
```

Start or continue a run in-game. The bridge will read the state, decide, and
act automatically — including dismissing tutorial popups, navigating menus,
and starting a new run after dying.

### 5. (Optional) Streaming overlay

Add `http://localhost:8934/` as an OBS Browser Source. Transparent
background, shows the current decision and Jev's confidence live.

## Configuration

Environment variables, all optional:

| Variable | Default | What it does |
|---|---|---|
| `JEV_API_KEY` | *(required)* | Your Jev API key |
| `POLL_INTERVAL` | `3.0` | Seconds between each decision (lower = faster but harder to watch) |
| `OVERLAY_PORT` | `8934` | Port for the local overlay web server |

## What's not handled yet

- Crystal Sphere events, fake-merchant events, and a few rare overlay types
  aren't wired up — the bridge just waits if it hits one (doesn't crash,
  won't act blindly, but also won't progress until you click through it
  yourself).
- The card/relic tier data is a point-in-time scrape of a community tier
  list site, not official, and will go stale as the game gets balance
  patches.
- Multiplayer isn't supported (singleplayer runs only).

## Why the mod DLL is prebuilt and included

The upstream [STS2MCP](https://github.com/Gennadiyev/STS2MCP) mod has had a
rolling series of game-version compatibility breaks since Slay the Spire 2
is still in active beta. The prebuilt DLL here is patched to work as of the
game version this was tested against. If it stops working after a game
update, check `mods/PATCH_NOTES.md` for how to rebuild it yourself, and
check the upstream repo's issues for the current state of compatibility.

## License

MIT for this repo's own code (`bridge.py`). The bundled mod DLL is a
separately MIT-licensed project — see `LICENSE` and `mods/STS2_MCP_LICENSE.txt`
for full attribution.
