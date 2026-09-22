# Jev plays Slay the Spire 2

A bridge that lets [Jev](https://typesafe.ai), TypeSafe's classification-only
"System One" model, play Slay the Spire 2 by itself. No text generation at
all by default. Every decision (which card to play, which map node to take,
whether to buy something) is a single fast classification call, usually a
couple hundred ms and a fraction of a cent. The decision backend is
swappable too, see "Using an LLM instead of Jev" below if you want to run
this with GPT-4o-mini or Claude instead.

This was thrown together fast for a stream. It's not a tuned bot, it made it
to the first boss on its first real run and died there. It's running on
generic deckbuilding heuristics plus a snapshot of a community tier list,
not deep game knowledge. Expect bad calls sometimes. See "Tuning" below for
how to change that, PRs welcome too if you want to make it actually good.

## How it works

1. [STS2MCP](https://github.com/Gennadiyev/STS2MCP) (a Slay the Spire 2 mod)
   exposes the live game state over a local HTTP API and accepts actions
   back the same way.
2. `bridge.py` polls that API, turns the current screen (combat, map, shop,
   card reward, etc.) into a plain-text description plus a list of legal
   options, and asks the decision backend a single question: which option
   is best.
3. The answer gets translated back into an actual game action and sent to
   STS2MCP.
4. Repeat every few seconds until you stop it.

An optional built-in web server (`http://localhost:8934/`) shows the bot's
live decisions, confidence, and top alternatives. Meant to be added as an
OBS Browser Source if you're streaming this.

## Setup

### 1. Get an API key

Default backend is Jev. Sign up at [typesafe.ai](https://typesafe.ai) and
grab an API key for the System One / Jev product, then set it:

```powershell
# Windows PowerShell
$env:JEV_API_KEY = "your-key-here"
```
```bash
# macOS / Linux
export JEV_API_KEY="your-key-here"
```

Want to use OpenAI or Anthropic instead? See "Using an LLM instead of Jev"
below.

### 2. Install the mod

Copy the three files from this repo's `mods/` folder into your Slay the
Spire 2 install's `mods/` directory:

- Windows (Steam default): `C:\Program Files (x86)\Steam\steamapps\common\Slay the Spire 2\mods\`
- Your actual path may differ if you use a custom Steam library.

Only `STS2_MCP.dll` and `STS2_MCP.json` need to go in `mods/`, the license
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
act automatically, including dismissing tutorial popups, navigating menus,
and starting a new run after dying.

### 5. (Optional) Streaming overlay

Add `http://localhost:8934/` as an OBS Browser Source. Transparent
background, shows the current decision and confidence live.

## Configuration

Environment variables, all optional unless noted:

| Variable | Default | What it does |
|---|---|---|
| `DECISION_BACKEND` | `jev` | `jev`, `openai`, `anthropic`, or `gemini` |
| `JEV_API_KEY` | none | Required if `DECISION_BACKEND=jev` |
| `OPENAI_API_KEY` | none | Required if `DECISION_BACKEND=openai` |
| `OPENAI_MODEL` | `gpt-4o-mini` | Any OpenAI chat model |
| `OPENAI_API_URL` | OpenAI's endpoint | Point this at any OpenAI-compatible endpoint (local models, other providers) |
| `ANTHROPIC_API_KEY` | none | Required if `DECISION_BACKEND=anthropic` |
| `ANTHROPIC_MODEL` | `claude-haiku-4-5-20251001` | Any Claude model |
| `GEMINI_API_KEY` | none | Required if `DECISION_BACKEND=gemini` |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Any Gemini model |
| `POLL_INTERVAL` | `3.0` | Seconds between each decision (lower = faster but harder to watch) |
| `OVERLAY_PORT` | `8934` | Port for the local overlay web server |

## Using an LLM instead of Jev

Jev is the default because that's what this was built and tuned around: it's
a classification-only model, so it just returns "which option" with no
reasoning or made-up text, which is exactly the shape this bridge needs, and
it's very cheap and fast. But the same interface works with a normal chat
model if you'd rather use one, or don't have a Jev key.

To use OpenAI:
```bash
export DECISION_BACKEND=openai
export OPENAI_API_KEY="sk-..."
# optional: export OPENAI_MODEL=gpt-4o  (defaults to gpt-4o-mini)
python bridge.py
```

To use Anthropic:
```bash
export DECISION_BACKEND=anthropic
export ANTHROPIC_API_KEY="sk-ant-..."
# optional: export ANTHROPIC_MODEL=claude-sonnet-4-5  (defaults to claude-haiku-4-5)
python bridge.py
```

To use Gemini:
```bash
export DECISION_BACKEND=gemini
export GEMINI_API_KEY="AIza..."
# optional: export GEMINI_MODEL=gemini-2.5-pro  (defaults to gemini-2.5-flash)
python bridge.py
```

Under the hood, each backend gets the same state description and the same
list of legal options, and is asked to reply with just the option key. Jev
returns that natively with calibrated probabilities across every option
(shown on the overlay). The LLM backends are prompted to reply with only the
key and get matched against the option list; if a reply doesn't match
anything, the bridge logs a warning and falls back to the first option
rather than crashing. Expect LLM backends to be slower and noticeably more
expensive per decision than Jev, that tradeoff is the whole point of this
project's angle, but the code doesn't force it on you.

Want another backend (a local model, a different provider)? Add a new
`_ask_<name>` function next to `_ask_openai` / `_ask_anthropic` in
`bridge.py` following the same `(choice, confidence, probabilities)` return
shape, then add it to the dispatch in `ask_decision()`. PRs welcome.

## Tuning

Everything the bot "knows" lives in plain constants and prompt strings near
the top of `bridge.py`, nothing is hidden behind config files or fine-tuning.
To change how it plays, edit:

- **`ARCHETYPE_GUIDE`**: the per-character strategy reference (what each
  character scales with, named archetypes). Edit this if the game gets
  balance patches or new characters, or if you just disagree with it.
- **`CARD_TIERS`** / `_TIER_DATA`: a snapshot of a community tier list per
  character. This will go stale as the game changes, it's a point-in-time
  scrape, not a live feed. Replace it with your own rankings, or delete it
  and adjust `card_tier()` to always return `"?"` if you'd rather the bot
  ignore tiers entirely and lean on synergy reasoning alone.
- **The `instructions` string inside each `handle_*` function** (e.g.
  `handle_combat`, `handle_card_reward`): these are the actual decision
  criteria handed to the backend for that screen type, in plain English.
  The survival threshold in combat (currently "below ~25% max HP" triggers
  defensive play), the deckbuilding priority order, the shop buying logic,
  all of it is just text here. Change the wording and the bot's behavior
  changes with it, no retraining needed since Jev and chat models both just
  read the instructions fresh every call.
- **`estimate_incoming_damage()`**: the regex-based damage parser. It reads
  the enemy intent's display text (like "Attack 12" or "Attack 6 (x2)") to
  estimate real incoming damage for the survival check. If a monster's
  intent text doesn't match the expected pattern, this silently
  undercounts, worth checking if the bot seems to underreact to a specific
  enemy.
- **`POLL_INTERVAL`**: how often it acts. Lower it for a faster, harder to
  follow bot; raise it if you want a slower pace to talk over on stream.

None of this needs the mod rebuilt, it's all in `bridge.py` and takes effect
on the next run of the script.

## What's not handled yet

- Crystal Sphere events, fake-merchant events, and a few rare overlay types
  aren't wired up, the bridge just waits if it hits one (doesn't crash,
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
separately MIT-licensed project, see `LICENSE` and
`mods/STS2_MCP_LICENSE.txt` for full attribution.
