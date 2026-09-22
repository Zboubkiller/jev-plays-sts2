#!/usr/bin/env python3
"""
Jev plays Slay the Spire 2.

Polls the STS2MCP mod's local API for game state, asks a decision backend
(Jev by default, or an LLM chat API if configured, see DECISION_BACKEND
below) which action to take, and executes it back through STS2MCP. Handles
combat, map navigation, card rewards, rest sites, shops, treasure, events,
and relic/hand selection. Anything unrecognized is left alone (not sent a
blind action) so a bad state can't crash the mod's HTTP server.

Setup:
  1. Pick a decision backend and set its API key, see the DECISION_BACKEND
     section below and the README for details. Jev is the default because
     it's what this was built and tuned around (fast, cheap, classification
     only), but OpenAI, Anthropic, and Gemini chat models work too.
  2. Install the STS2MCP mod (see README.md / mods/ folder in this repo)
     into Slay the Spire 2's mods/ directory and enable mods in-game.
  3. Launch the game, then run: python bridge.py
  4. Optional: open http://localhost:8934/ as an OBS Browser Source to
     show the bot's live decisions on stream.

This is a fast, cheap hack project, not a tuned Slay the Spire bot. It uses
generic deckbuilding heuristics plus a community tier list snapshot, expect
it to make bad calls sometimes. See the "Tuning" section in the README for
where to change its behavior. PRs welcome.
"""
import json
import os
import re
import threading
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STS2_BASE = "http://localhost:15526/api/v1/singleplayer"

# --- Decision backend selection ---
# DECISION_BACKEND picks what answers "what's the best move": "jev" (default,
# TypeSafe's classification-only model), "openai" (any OpenAI-compatible chat
# completions endpoint), "anthropic" (Claude via the Messages API), or
# "gemini" (Google's Generative Language API). All four implement the same
# interface: given a state description and a list of named options, return
# which option key was chosen.
BACKEND = os.environ.get("DECISION_BACKEND", "jev").lower()

JEV_API_URL = "https://api.typesafe.ai/v1/systemone"
JEV_API_KEY = os.environ.get("JEV_API_KEY")

OPENAI_API_URL = os.environ.get("OPENAI_API_URL", "https://api.openai.com/v1/chat/completions")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")

if BACKEND == "jev" and not JEV_API_KEY:
    raise SystemExit(
        "DECISION_BACKEND is 'jev' (the default) but JEV_API_KEY is not set.\n"
        "Get a key from https://typesafe.ai and set it, e.g.:\n"
        '  PowerShell:  $env:JEV_API_KEY = "your-key-here"\n'
        '  bash/zsh:    export JEV_API_KEY="your-key-here"\n'
        "Or set DECISION_BACKEND to openai, anthropic, or gemini to use an LLM instead, see README."
    )
elif BACKEND == "openai" and not OPENAI_API_KEY:
    raise SystemExit("DECISION_BACKEND=openai but OPENAI_API_KEY is not set.")
elif BACKEND == "anthropic" and not ANTHROPIC_API_KEY:
    raise SystemExit("DECISION_BACKEND=anthropic but ANTHROPIC_API_KEY is not set.")
elif BACKEND == "gemini" and not GEMINI_API_KEY:
    raise SystemExit("DECISION_BACKEND=gemini but GEMINI_API_KEY is not set.")
elif BACKEND not in ("jev", "openai", "anthropic", "gemini"):
    raise SystemExit(f"Unknown DECISION_BACKEND '{BACKEND}', expected 'jev', 'openai', 'anthropic', or 'gemini'.")
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "3.0"))
OVERLAY_PORT = int(os.environ.get("OVERLAY_PORT", "8934"))
CURRENT_SCREEN = [None]  # mutable single-item list so ask_decision can read the latest value

# Shared state read by the overlay HTTP server, written after every Jev call.
_overlay_lock = threading.Lock()
_overlay_state = {
    "screen": None,
    "choice": None,
    "confidence": None,
    "probabilities": {},
    "options": {},
    "state_text": "",
    "timestamp": None,
    "decision_count": 0,
}


def update_overlay(screen, choice, confidence, probabilities, options, state_text):
    with _overlay_lock:
        _overlay_state["screen"] = screen
        _overlay_state["choice"] = choice
        _overlay_state["confidence"] = confidence
        _overlay_state["probabilities"] = probabilities
        _overlay_state["options"] = options
        _overlay_state["state_text"] = state_text
        _overlay_state["timestamp"] = time.time()
        _overlay_state["decision_count"] += 1


OVERLAY_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Jev plays STS2</title>
<style>
  html,body{margin:0; padding:0; background:transparent; font-family:'Consolas','IBM Plex Mono',monospace; color:#fff;}
  #box{
    position:fixed; bottom:24px; left:24px; max-width:560px;
    background:rgba(10,10,14,0.82); border:1px solid #ffb800; border-radius:8px;
    padding:16px 20px; box-shadow:0 4px 24px rgba(0,0,0,0.5);
  }
  #screen{font-size:12px; color:#ffb800; text-transform:uppercase; letter-spacing:0.05em; margin-bottom:6px;}
  #choice{font-size:22px; font-weight:700; margin-bottom:4px;}
  #conf{font-size:14px; color:#4ddb8f; margin-bottom:10px;}
  .opt{font-size:12px; color:#aaa; padding:2px 0;}
  .opt.picked{color:#4ddb8f; font-weight:700;}
  #footer{font-size:10px; color:#666; margin-top:10px;}
</style></head>
<body>
<div id="box">
  <div id="screen">-</div>
  <div id="choice">Waiting for first decision...</div>
  <div id="conf"></div>
  <div id="opts"></div>
  <div id="footer"></div>
</div>
<script>
async function poll() {
  try {
    const r = await fetch('/status');
    const d = await r.json();
    document.getElementById('screen').textContent = 'JEV DECISION - ' + (d.screen || '?');
    document.getElementById('choice').textContent = d.choice || '-';
    document.getElementById('conf').textContent = d.confidence != null ? ('confidence: ' + Math.round(d.confidence*100) + '%') : '';
    const opts = document.getElementById('opts');
    opts.innerHTML = '';
    const probs = d.probabilities || {};
    Object.keys(probs).sort((a,b)=>probs[b]-probs[a]).slice(0,5).forEach(k => {
      const row = document.createElement('div');
      row.className = 'opt' + (k === d.choice ? ' picked' : '');
      row.textContent = (d.options && d.options[k] ? d.options[k] : k) + '  ' + Math.round(probs[k]*100) + '%';
      opts.appendChild(row);
    });
    document.getElementById('footer').textContent = 'decision #' + d.decision_count;
  } catch (e) {}
}
setInterval(poll, 1000);
poll();
</script>
</body></html>"""


class OverlayHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # keep the terminal clean

    def do_GET(self):
        if self.path == "/status":
            with _overlay_lock:
                body = json.dumps(_overlay_state).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
        else:
            body = OVERLAY_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)


def start_overlay_server():
    server = ThreadingHTTPServer(("127.0.0.1", OVERLAY_PORT), OverlayHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"Overlay running at http://localhost:{OVERLAY_PORT}/  (add as an OBS Browser Source)")


def http_get(url):
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def http_post(url, body, headers=None):
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.read().decode()[:300]}"}


def get_state():
    return http_get(STS2_BASE)


def do_action(action, **params):
    body = {"action": action, **params}
    result = http_post(STS2_BASE, body)
    if isinstance(result, dict) and result.get("error"):
        print(f"  [action error] {action}({params}) -> {result['error']}")
    return result


def ask_decision(state_text, options, instructions):
    """Ask whichever backend is configured which option to take.
    options: dict of {key: description}. Returns the chosen key.
    Dispatches to Jev, OpenAI, or Anthropic based on DECISION_BACKEND."""
    if BACKEND == "openai":
        choice, conf, probs = _ask_openai(state_text, options, instructions)
    elif BACKEND == "anthropic":
        choice, conf, probs = _ask_anthropic(state_text, options, instructions)
    elif BACKEND == "gemini":
        choice, conf, probs = _ask_gemini(state_text, options, instructions)
    else:
        choice, conf, probs = _ask_jev(state_text, options, instructions)
    update_overlay(CURRENT_SCREEN[0], choice, conf, probs, options, state_text)
    return choice


def _ask_jev(state_text, options, instructions):
    questions = {
        "action": {
            "type": "choice",
            "instructions": instructions,
            "criteria": options
        }
    }
    result = http_post(
        JEV_API_URL,
        {"state": state_text, "model": "jev-latest", "questions": questions},
        headers={"Authorization": f"Bearer {JEV_API_KEY}"}
    )
    if "error" in result:
        print(f"  [jev error] {result['error']}, falling back to first option")
        choice = next(iter(options))
        return choice, None, {}
    choice = result["answers"]["action"]["choice"]
    probs = result["answers"]["action"].get("probabilities", {})
    conf = probs.get(choice)
    print(f"  [jev] chose '{choice}' (p={conf})")
    return choice, conf, probs


def _format_options_block(options):
    return "\n".join(f"- {key}: {desc}" for key, desc in options.items())


def _extract_choice(raw_text, options):
    """LLM chat backends reply in free text, not a validated enum, so match it back
    against the known option keys: exact match first, then substring, then give up
    and fall back to the first option rather than crash on a bad response."""
    text = (raw_text or "").strip()
    if text in options:
        return text
    for key in options:
        if key.lower() in text.lower():
            return key
    print(f"  [warning] could not match backend response '{text[:80]}' to any option, using the first one")
    return next(iter(options))


def _decision_system_prompt(instructions, options):
    return (
        instructions
        + "\n\nOptions (pick exactly one):\n"
        + _format_options_block(options)
        + "\n\nReply with ONLY the option key from the list above, nothing else. No punctuation, "
        "no explanation, just the key exactly as written (e.g. 'play_0' or 'end_turn')."
    )


def _ask_openai(state_text, options, instructions):
    body = {
        "model": OPENAI_MODEL,
        "temperature": 0,
        "max_tokens": 20,
        "messages": [
            {"role": "system", "content": _decision_system_prompt(instructions, options)},
            {"role": "user", "content": state_text}
        ]
    }
    result = http_post(OPENAI_API_URL, body, headers={"Authorization": f"Bearer {OPENAI_API_KEY}"})
    if "error" in result:
        print(f"  [openai error] {result['error']}, falling back to first option")
        return next(iter(options)), None, {}
    raw = result["choices"][0]["message"]["content"]
    choice = _extract_choice(raw, options)
    print(f"  [openai:{OPENAI_MODEL}] chose '{choice}'")
    return choice, 1.0, {choice: 1.0}


def _ask_anthropic(state_text, options, instructions):
    body = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 20,
        "system": _decision_system_prompt(instructions, options),
        "messages": [{"role": "user", "content": state_text}]
    }
    result = http_post(
        ANTHROPIC_API_URL, body,
        headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01"}
    )
    if "error" in result:
        print(f"  [anthropic error] {result['error']}, falling back to first option")
        return next(iter(options)), None, {}
    raw = "".join(block.get("text", "") for block in result.get("content", []))
    choice = _extract_choice(raw, options)
    print(f"  [anthropic:{ANTHROPIC_MODEL}] chose '{choice}'")
    return choice, 1.0, {choice: 1.0}


def _ask_gemini(state_text, options, instructions):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    body = {
        "system_instruction": {"parts": [{"text": _decision_system_prompt(instructions, options)}]},
        "contents": [{"role": "user", "parts": [{"text": state_text}]}],
        "generationConfig": {"temperature": 0, "maxOutputTokens": 20}
    }
    result = http_post(url, body)
    if "error" in result:
        print(f"  [gemini error] {result['error']}, falling back to first option")
        return next(iter(options)), None, {}
    try:
        raw = result["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        print(f"  [gemini error] unexpected response shape: {json.dumps(result)[:200]}, falling back to first option")
        return next(iter(options)), None, {}
    choice = _extract_choice(raw, options)
    print(f"  [gemini:{GEMINI_MODEL}] chose '{choice}'")
    return choice, 1.0, {choice: 1.0}


def deck_summary(player):
    """Build a compact 'what's in the deck' line from hand + all piles, since the API
    doesn't expose a single 'full deck' field - but hand+draw+discard+exhaust covers it."""
    names = []
    for pile_key in ("hand", "draw_pile", "discard_pile", "exhaust_pile"):
        for c in player.get(pile_key, []) or []:
            n = c.get("name")
            if n:
                names.append(n)
    if not names:
        return "unknown"
    counts = {}
    for n in names:
        counts[n] = counts.get(n, 0) + 1
    return ", ".join(f"{n}x{c}" if c > 1 else n for n, c in counts.items())


def relics_summary(player):
    relics = player.get("relics", [])
    if not relics:
        return "none"
    return "; ".join(f"{r.get('name')} ({r.get('description', '')})" for r in relics)


# Community tier rankings (sts2companion.com), merged across all 5 characters.
# Used to give Jev an actual power-level signal instead of guessing from card text alone.
_TIER_DATA = {
    "S": ["Barricade", "Brand", "Break", "Burning Pact", "Cascade", "Conflagration", "Corruption", "Crimson Mantle",
          "Dark Embrace", "Demon Form", "Demonic Shield", "Drum of Battle", "Evil Eye", "Feel No Pain", "Fiend Fire",
          "Flame Barrier", "Howl from Beyond", "Impervious", "Offering", "Pact's End", "Rupture", "Second Wind",
          "Spite", "Stoke", "Taunt", "Tear Asunder", "Thrash", "Vicious", "Whirlwind",
          "Abrasive", "Acrobatics", "Bullet Time", "Calculated Gamble", "Corrosive Wave", "Hand Trick", "Haze",
          "Leg Sweep", "Malaise", "Murder", "Nightmare", "Reflex", "Shadow Step", "Skewer", "Sneaky",
          "Storm of Steel", "Suppress", "Tactician", "Tools of the Trade",
          "Biased Cognition", "Bulk Up", "Consuming Shadow", "Coolant", "Defragment", "Echo Form", "Fight Through",
          "Flak Cannon", "Fusion", "Glacier", "Hyperbeam", "Ice Lance", "Ignition", "Meteor Strike", "Modded",
          "Multi-Cast", "Null", "Overclock", "Quadcast", "Rainbow", "Refract", "Shadow Shield", "Shatter", "Voltaic",
          "Bone Shards", "Capture Spirit", "Cleanse", "Demesne", "Devour Life", "Dirge", "Eidolon", "Eradicate",
          "Fetch", "Glimpse Beyond", "Melancholy", "Necro Mastery", "Protector", "Rattle", "Reanimate", "Sacrifice",
          "Seance", "Severance", "Spirit of Ash", "Spur", "Squeeze",
          "Bombardment", "Comet", "Heavenly Drill", "I Am Invincible", "Tyranny"],
    "A": ["Ashen Strike", "Blood Wall", "Colossus", "Dominate", "Feed", "Fight Me!", "Forgotten Ritual", "Hellraiser",
          "Inferno", "Inflame", "Mangle", "Not Yet", "Rage", "Shrug It Off", "Stone Armor", "True Grit", "Uppercut",
          "Adrenaline", "Afterimage", "Assassinate", "Blur", "Bouncing Flask", "Bubble Bubble", "Dagger Throw",
          "Dash", "Envenom", "Escape Plan", "Grand Finale", "Hidden Daggers", "Master Planner", "Memento Mori",
          "Mirage", "Noxious Fumes", "Outbreak", "Prepared", "Ricochet", "Serpent Form", "Snakebite", "Untouchable",
          "Wraith Form",
          "Boost Away", "Boot Sequence", "Capacitor", "Chill", "Compile Driver", "Coolheaded", "Creative AI",
          "Darkness", "Genetic Algorithm", "Iteration", "Lightning Rod", "Machine Learning", "Reboot",
          "Rocket Punch", "Scrape", "Storm", "Sunder", "Synchronize", "Tempest", "Thunder", "Trash to Treasure",
          "Banshee's Cry", "Calcify", "Delay", "End of Days", "Grave Warden", "High Five", "Legion of Bone",
          "Neurosurge", "Pagestorm", "Parse", "Pull Aggro", "Putrefy", "Reave", "Right Hand Hand", "Sic 'Em",
          "Snap", "Soul Storm", "Undeath",
          "Arsenal", "Big Bang", "Bulwark", "Decisions, Decisions", "Dying Star", "Gamma Blast", "GUARDS!!!",
          "Kingly Kick", "Meteor Shower", "Neutron Aegis", "Reflect", "Seven Stars", "Stardust", "Void Form"],
    "B": ["Aggression", "Armaments", "Battle Trance", "Bludgeon", "Body Slam", "Bully", "Cinder", "Cruelty",
          "Dismantle", "Havoc", "Hemokinesis", "Iron Wave", "One-Two Punch", "Primal Force", "Setup Strike",
          "Sword Boomerang", "Tank", "Twin Strike",
          "Accelerant", "Backflip", "Blade of Ink", "Burst", "Deadly Poison", "Echoing Slash", "Expose",
          "Flick-Flack", "Follow Through", "Footwork", "Knife Trap", "Phantom Blades", "Poisoned Stab", "Scare",
          "Shadowmeld", "Speedster", "Survivor", "The Hunt", "Well-Laid Plans",
          "Adaptive Strike", "All for One", "Ball Lightning", "Barrage", "Cold Snap", "Compact", "Focused Strike",
          "FTL", "Glasswork", "Helix Drill", "Hotfix", "Scavenge", "Signal Boost", "Skim", "Smokestack", "Spinner",
          "Supercritical", "TURBO",
          "Afterlife", "Bury", "Call of the Void", "Danse Macabre", "Death's Door", "Debilitate", "Defy",
          "Enfeebling Touch", "Fear", "Flatten", "Forbidden Grimoire", "Hang", "Invoke", "Lethality", "Misery",
          "Oblivion", "Poke", "Pull from Below", "Reap", "Reaper Form", "Sculpting Strike", "Shared Fate", "Shroud",
          "The Scythe", "Time's Up", "Transfigure",
          "Beat into Shape", "Bundle of Joy", "CHARGE!!", "Child of the Stars", "Convergence",
          "Crash Landing", "Foregone Conclusion", "Glimmer", "Glitterstream", "Heirloom Hammer", "Kingly Punch",
          "Knockout Blow", "Know Thy Place", "Make It So", "Manifest Authority", "Monologue", "Pale Blue Dot",
          "Particle Wall", "Prophesize", "Resonance", "Royalties", "Seeking Edge", "The Sealed Throne", "The Smith"],
    "C": ["Bloodletting", "Breakthrough", "Expect a Fight", "Infernal Blade", "Juggernaut", "Juggling",
          "Molten Fist", "Pillage", "Pommel Strike", "Pyre", "Rampage", "Stomp", "Thunderclap", "Tremble",
          "Unmovable", "Unrelenting",
          "Accuracy", "Anticipate", "Backstab", "Cloak and Dagger", "Dagger Spray", "Deflect", "Dodge and Roll",
          "Expertise", "Fan of Knives", "Finisher", "Flanking", "Flechettes", "Infinite Blades", "Pinpoint",
          "Pounce", "Precise Cut", "Predator", "Strangle", "Sucker Punch", "Tracking", "Up My Sleeve",
          "Beam Cell", "Buffer", "Chaos", "Charge Battery", "Double Energy", "Dualcast", "Energy Surge",
          "Go for the Eyes", "Gunk Up", "Hailstorm", "Hologram", "Leap", "Loop", "Subroutine", "Sweeping Beam",
          "Synthesis", "Tesla Coil", "Uproar", "White Noise", "Zap",
          "Bodyguard", "Borrowed Time", "Countdown", "Death March", "Deathbringer", "Defile", "Dredge",
          "Friendship", "Haunt", "Negative Pulse", "No Escape", "Scourge", "Sentry Mode", "Sow", "Unleash",
          "Veilpiercer",
          "Alignment", "Astral Pulse", "BEGONE!", "Black Hole", "Celestial Might", "Cloak of Stars", "Conqueror",
          "Cosmic Indifference", "Devastate", "Falling Star", "Furnace", "Gather Light", "Genesis", "Glow",
          "Guiding Star", "Hammer Time", "Hegemony", "Largesse", "Lunar Blast", "Monarch's Gaze", "Parry",
          "Patter", "Photon Cut", "Quasar", "Radiate", "Royal Gamble", "Shining Strike", "Spoils of Battle",
          "Summon Forth", "Supermassive", "Sword Sage", "Terraforming"],
    "D": ["Anger", "Bash", "Defend", "Headbutt", "Perfected Strike", "Stampede", "Strike",
          "Blade Dance", "Leading Strike", "Neutralize", "Piercing Wail", "Slice",
          "Claw", "Feral", "Momentum Strike",
          "Blight Strike", "Drain Power", "Graveblast", "Sleight of Flesh", "Wisp",
          "Collision Course", "Crescent Spear", "Crush Under", "Hidden Cache", "Orbit", "Refine Blade",
          "Solar Strike", "Spectrum Shift", "Venerate", "Wrought in War"],
}
CARD_TIERS = {name.lower(): tier for tier, names in _TIER_DATA.items() for name in names}


def card_tier(name):
    return CARD_TIERS.get((name or "").lower(), "?")


ARCHETYPE_GUIDE = (
    "Slay the Spire 2 character archetype reference (from the community wiki, use whichever matches the "
    "current character's mechanics):\n"
    "- Ironclad (Le Soldat de Fer): scales with Strength, Block, Exhaust, and Self-Damage. Archetypes: "
    "Strength Breaker (stack Strength + multi-hit attacks), Block Engine (Barricade + heavy block cards for a "
    "durable shell), Exhaust Payoff (exhaust enablers like Dark Embrace/Feel No Pain rewarding deck thinning), "
    "Burning Attrition (self-damage powers like Rupture/Inferno for sustained damage-over-time).\n"
    "- The Silent: scales with Poison, Shivs, and Discard. Archetypes: Poison Control (stack poison sources), "
    "Shiv Tempo (generate+scale many small attacks), Discard Engine (discard outlets + payoffs), Weakness Lock "
    "(debuff uptime + block).\n"
    "- The Defect: scales with Orbs (Lightning/Frost) and Focus. Archetypes: Lightning Tempo (fast repeated "
    "damage), Frost Focus (orbs into block via Focus), Power Chain (stacking persistent powers), Zero-Cost Cycle "
    "(cheap repeatable cards + draw loops).\n"
    "- The Necrobinder: scales with the Osty ally, Souls, and Summons. Archetypes: Minion Swarm (keep summons "
    "alive and multiply impact), Bone Economy (balance resource generators/spenders), Corpse Control (removal + "
    "mitigation), Dark Pact Scaling (risky setup for big late-fight payoff).\n"
    "- The Regent: scales with Sovereign Star generation/spending and Forging. Archetypes: Blood Tempo (aggressive "
    "burst damage), Dagger Waltz (cheap repeatable attacks + volume), Royal Retainers (enabler/retainer pairs), "
    "Execute Control (weakness/block/setup into a finisher).\n"
    "General principle for all characters: commit to ONE archetype based on what's already in the deck/relics "
    "rather than taking generically 'good' cards that don't fit; a focused deck beats a diluted one."
)


def estimate_incoming_damage(enemies):
    """Best-effort numeric damage estimate parsed from intent label text, since the API
    doesn't expose a raw damage number - only a formatted display string like 'Attack 12'
    or 'Attack 6 (x2)'. This turns 'should I block' into an actual number comparison
    instead of leaving Jev to guess from vague text."""
    total = 0
    per_enemy = []
    for e in enemies:
        enemy_total = 0
        for intent in e.get("intents") or []:
            if "attack" not in (intent.get("type") or "").lower():
                continue
            label = intent.get("label") or ""
            nums = [int(n) for n in re.findall(r"\d+", label)]
            if not nums:
                continue
            mult_match = re.search(r"[x×]\s*(\d+)", label, re.IGNORECASE)
            if mult_match and len(nums) >= 2:
                dmg, hits = nums[0], int(mult_match.group(1))
                enemy_total += dmg * hits
            else:
                enemy_total += sum(nums)
        if enemy_total:
            per_enemy.append(f"{e.get('entity_id')}: ~{enemy_total}")
        total += enemy_total
    return total, per_enemy


def handle_combat(state):
    player = state["player"]
    battle = state["battle"]
    hp, max_hp = player["hp"], player["max_hp"]
    energy = player.get("energy", 0)
    hand = player.get("hand", [])
    enemies = battle.get("enemies", [])

    if not battle.get("is_play_phase", True):
        print("  enemy turn, waiting...")
        return

    lines = [f"Combat. Character: {player.get('character', '?')}. Player HP {hp}/{max_hp}, block {player.get('block', 0)}, energy {energy}."]
    lines.append(f"Relics: {relics_summary(player)}")
    lines.append(f"Full deck (hand+draw+discard+exhaust): {deck_summary(player)}")
    lines.append("Hand:")
    options = {}
    for card in hand:
        idx = card.get("index")
        name = card.get("name", "?")
        cost = card.get("cost", "?")
        desc = card.get("description", "")
        playable = card.get("can_play", True)
        lines.append(f"  [{idx}] {name} (cost {cost}, playable={playable}): {desc}")
        if playable:
            options[f"play_{idx}"] = f"Play '{name}' (index {idx})"

    potions = player.get("potions", [])
    usable_potions = [p for p in potions if p.get("can_use_in_combat")]
    if usable_potions:
        lines.append("Usable potions:")
        for p in usable_potions:
            slot = p.get("slot")
            pname = p.get("name", "?")
            pdesc = p.get("description", "")
            lines.append(f"  potion slot {slot}: {pname}: {pdesc}")
            options[f"potion_{slot}"] = f"Use potion '{pname}' (slot {slot})"

    lines.append("Enemies:")
    for e in enemies:
        intents = e.get("intents") or []
        intent_desc = "; ".join(i.get("label") or i.get("type", "?") for i in intents) or "unknown"
        lines.append(f"  {e.get('entity_id')} ({e.get('name')}): HP {e.get('hp')}/{e.get('max_hp')}, block {e.get('block', 0)}, intent: {intent_desc}")

    incoming, per_enemy = estimate_incoming_damage(enemies)
    block = player.get("block", 0)
    unblocked = max(0, incoming - block)
    lines.append(f"Estimated total incoming attack damage this turn: ~{incoming} ({'; '.join(per_enemy) if per_enemy else 'no attacks telegraphed'}).")
    lines.append(f"Current block: {block}. Unblocked damage if no more block is gained: ~{unblocked} (player has {hp} HP).")

    options["end_turn"] = "End the turn, playing no more cards"

    state_text = "\n".join(lines)
    instructions = (
        ARCHETYPE_GUIDE + "\n\n"
        "What is the single best next action in this Slay the Spire 2 combat turn? Apply these priorities in order:\n"
        "1. SURVIVAL FIRST: if unblocked incoming damage this turn would take the player below ~25% max HP, "
        "strongly prefer playing block/defensive cards now over damage cards, unless the fight can be won this turn.\n"
        "2. LETHAL: if total damage output this turn can kill an enemy (or all enemies), prioritize that over blocking.\n"
        "3. EFFICIENCY: prefer cards with the best value-per-energy for the current situation (block per energy when "
        "defending, damage per energy when attacking); avoid ending the turn with unused energy if a useful card is still playable.\n"
        "4. SYNERGY: prefer cards that combo with the player's current relics and deck (e.g. strength/block scaling, "
        "status effects, card draw) over generic vanilla cards when the outcome is otherwise close.\n"
        "5. Only end the turn early if no remaining playable card meaningfully helps (e.g. energy too low, or the "
        "position is already safe and stable).\n"
        "6. POTIONS: use a combat potion when it clearly swings the fight (e.g. a damage/block potion when HP is low "
        "or facing lethal, a buff potion for a big damage turn against a dangerous enemy). Don't hoard potions "
        "indefinitely - an unused potion helps nobody, but don't waste a strong potion on a trivial fight either."
    )
    choice = ask_decision(state_text, options, instructions)

    if choice == "end_turn":
        do_action("end_turn")
        return

    if choice.startswith("potion_"):
        slot = int(choice.split("_")[1])
        potion = next((p for p in usable_potions if p.get("slot") == slot), None)
        if potion and potion.get("target_type") == "AnyEnemy" and enemies:
            do_action("use_potion", slot=slot, target=enemies[0].get("entity_id"))
        else:
            do_action("use_potion", slot=slot)
        return

    idx = int(choice.split("_")[1])
    card = next((c for c in hand if c.get("index") == idx), None)
    if card and card.get("target_type") == "AnyEnemy" and enemies:
        target = enemies[0].get("entity_id")
        do_action("play_card", card_index=idx, target=target)
    else:
        do_action("play_card", card_index=idx)


def handle_event(state):
    ev = state.get("event", {})
    options_list = ev.get("options", [])
    enabled = [o for o in options_list if not o.get("is_locked", False)]
    if not enabled:
        print("  no unlocked event options, waiting...")
        return

    lines = [f"Event: {ev.get('event_name')}. {ev.get('body', '')}"]
    options = {}
    for opt in enabled:
        idx = opt["index"]
        title = opt.get("title", "?")
        desc = opt.get("description", "")
        relic = f" (grants relic: {opt.get('relic_name')})" if opt.get("relic_name") else ""
        lines.append(f"  [{idx}] {title}: {desc}{relic}")
        options[f"opt_{idx}"] = f"{title} (index {idx})"

    state_text = "\n".join(lines)
    choice = ask_decision(state_text, options, "Which event option should be chosen? Favor options that are safe and beneficial long-term over risky gambles, unless HP is high and the upside is clearly worth it.")
    idx = int(choice.split("_")[1])
    do_action("choose_event_option", index=idx)


def handle_rest_site(state):
    rs = state.get("rest_site", {})
    options_list = rs.get("options", [])
    enabled = [o for o in options_list if o.get("is_enabled", True)]
    if not enabled:
        if rs.get("can_proceed"):
            print("  rest site option already used, proceeding to leave...")
            do_action("proceed")
        else:
            print("  no enabled rest site options, waiting...")
        return

    player = state.get("player", {})
    lines = [f"Rest site. Player HP {player.get('hp')}/{player.get('max_hp')}."]
    options = {}
    for opt in enabled:
        idx = opt["index"]
        name = opt.get("name", "?")
        desc = opt.get("description", "")
        lines.append(f"  [{idx}] {name}: {desc}")
        options[f"opt_{idx}"] = f"{name} (index {idx})"

    state_text = "\n".join(lines)
    choice = ask_decision(state_text, options, "Which rest site option should be chosen? Rest to heal HP if below ~70% max HP and no urgent upgrade is available; otherwise upgrade a card or use other options.")
    idx = int(choice.split("_")[1])
    do_action("choose_rest_option", index=idx)


def handle_card_reward(state):
    cr = state.get("card_reward", {})
    cards = cr.get("cards", [])
    can_skip = cr.get("can_skip", False)
    player = state.get("player", {})

    lines = [f"Card reward. Character: {player.get('character', '?')}. Choose a card to add to your deck, or skip."]
    lines.append(f"Current relics: {relics_summary(player)}")
    lines.append(f"Current deck: {deck_summary(player)}")
    options = {}
    for card in cards:
        idx = card.get("index")
        name = card.get("name", "?")
        desc = card.get("description", "")
        tier = card_tier(name)
        lines.append(f"  [{idx}] {name} (community tier: {tier}): {desc}")
        options[f"card_{idx}"] = f"Take '{name}' (index {idx}, tier {tier})"
    if can_skip:
        options["skip"] = "Skip this card reward, take nothing"

    state_text = "\n".join(lines)
    instructions = (
        ARCHETYPE_GUIDE + "\n\n"
        "Each card above is tagged with a community tier (S=best, D=worst) from published tier lists - treat this as "
        "a strong prior on raw power level, but still weigh it against synergy with the current deck/relics; a lower "
        "tier card that's a perfect fit for the current archetype can beat a higher tier card that doesn't fit.\n\n"
        "Which card reward should be picked, given the current relics and deck listed above? Apply these deckbuilding "
        "priorities:\n"
        "1. SYNERGY: strongly favor a card that scales with or is enabled by an existing relic or deck theme already "
        "shown above (e.g. a block-scaling card if block relics/cards are already present, a strength/power card if "
        "strength-scaling is already present, a status/debuff card if the deck already applies debuffs).\n"
        "2. DECK THINNING: a small deck of efficient cards is usually stronger than a bloated deck of mediocre ones - "
        "skip narrow situational cards or clear downgrades from what's already in the deck.\n"
        "3. CURVE: prefer cards that are strong for their energy cost (efficient removal, block, or damage) over "
        "expensive cards unless the deck already has strong energy generation to support them.\n"
        "4. Avoid picking a card that duplicates a weak effect already overrepresented in the deck.\n"
        "Skip the reward entirely if none of the options clearly improve the deck."
    )
    choice = ask_decision(state_text, options, instructions)

    if choice == "skip":
        do_action("skip_card_reward")
    else:
        idx = int(choice.split("_")[1])
        do_action("select_card_reward", card_index=idx)


def handle_rewards(state):
    rw = state.get("rewards", {})
    items = rw.get("items", [])
    if not items:
        print("  no reward items left, proceeding")
        do_action("proceed")
        return

    lines = ["Combat rewards available to claim."]
    options = {}
    for item in items:
        idx = item.get("index")
        rtype = item.get("type", "?")
        desc = item.get("description", "")
        lines.append(f"  [{idx}] {rtype}: {desc}")
        options[f"claim_{idx}"] = f"Claim {rtype} (index {idx})"

    state_text = "\n".join(lines)
    choice = ask_decision(state_text, options, "Which reward should be claimed next? All rewards are normally worth claiming, just pick any unclaimed one.")
    idx = int(choice.split("_")[1])
    do_action("claim_reward", index=idx)


def handle_shop(state):
    shop = state.get("shop", {})
    items = shop.get("items", [])
    affordable = [it for it in items if it.get("is_stocked", True) and it.get("can_afford", False)]

    lines = [f"Shop. Gold: {state.get('player', {}).get('gold', '?')}."]
    options = {}
    for it in affordable:
        idx = it["index"]
        cat = it.get("category", "?")
        price = it.get("price", "?")
        name = it.get("card_name") or it.get("relic_name") or it.get("potion_name") or cat
        desc = it.get("card_description") or it.get("relic_description") or it.get("potion_description") or ""
        tier_note = f", community tier: {card_tier(name)}" if cat == "card" else ""
        lines.append(f"  [{idx}] {cat}: {name} ({price} gold{tier_note}): {desc}")
        options[f"buy_{idx}"] = f"Buy {name} (index {idx}, {price} gold)"
    options["leave"] = "Leave the shop without buying anything"

    state_text = "\n".join(lines)
    choice = ask_decision(state_text, options, "Should anything be bought at this shop, and if so what? Only buy things that are clearly worth the gold; leave if nothing affordable is good value.")

    if choice == "leave":
        do_action("proceed")
    else:
        idx = int(choice.split("_")[1])
        do_action("shop_purchase", index=idx)


def handle_treasure(state):
    tr = state.get("treasure", {})
    relics = tr.get("relics", [])
    if not relics:
        if tr.get("can_proceed"):
            print("  treasure claimed, proceeding to leave...")
            do_action("proceed")
        else:
            print(f"  {tr.get('message', 'treasure room, no relics shown yet')}, waiting...")
        return

    lines = ["Treasure room. Choose a relic."]
    options = {}
    for r in relics:
        idx = r["index"]
        name = r.get("name", "?")
        desc = r.get("description", "")
        lines.append(f"  [{idx}] {name} ({r.get('rarity', '?')}): {desc}")
        options[f"relic_{idx}"] = f"Take {name} (index {idx})"

    state_text = "\n".join(lines)
    choice = ask_decision(state_text, options, "Which relic should be taken from this treasure chest?")
    idx = int(choice.split("_")[1])
    do_action("claim_treasure_relic", index=idx)


def handle_relic_select(state):
    rs = state.get("relic_select", {})
    relics = rs.get("relics", [])
    can_skip = rs.get("can_skip", False)
    if not relics:
        print("  no relics offered yet, waiting...")
        return

    lines = ["Relic selection."]
    options = {}
    for r in relics:
        idx = r["index"]
        name = r.get("name", "?")
        desc = r.get("description", "")
        lines.append(f"  [{idx}] {name} ({r.get('rarity', '?')}): {desc}")
        options[f"relic_{idx}"] = f"Take {name} (index {idx})"
    if can_skip:
        options["skip"] = "Skip, take no relic"

    state_text = "\n".join(lines)
    choice = ask_decision(state_text, options, "Which relic should be picked here?")
    if choice == "skip":
        do_action("skip_relic_selection")
    else:
        idx = int(choice.split("_")[1])
        do_action("select_relic", index=idx)


def handle_hand_select(state):
    hs = state.get("hand_select", {})
    cards = hs.get("cards", [])
    prompt = hs.get("prompt", "Select a card from your hand.")
    if not cards:
        print("  no selectable cards yet, waiting...")
        return

    lines = [f"In-combat hand selection: {prompt}"]
    options = {}
    for c in cards:
        idx = c.get("index")
        name = c.get("name", "?")
        desc = c.get("description", "")
        lines.append(f"  [{idx}] {name}: {desc}")
        options[f"card_{idx}"] = f"Select '{name}' (index {idx})"

    state_text = "\n".join(lines)
    choice = ask_decision(state_text, options, f"Given the prompt '{prompt}', which card should be selected?")
    idx = int(choice.split("_")[1])
    do_action("combat_select_card", card_index=idx)
    do_action("combat_confirm_selection")


def handle_game_over(state):
    print("  run ended, returning to main menu (a new run will start automatically from there)...")
    do_action("menu_select", option="main_menu")
    time.sleep(1.0)


def handle_card_select(state):
    cs = state.get("card_select", {})
    if cs.get("preview_showing"):
        print("  selection made, confirming...")
        do_action("confirm_selection")
        return

    cards = cs.get("cards", [])
    screen_type = cs.get("screen_type", "select")
    prompt = cs.get("prompt", "")
    if not cards:
        print("  no cards to select yet, waiting...")
        return

    lines = [f"Card select screen ({screen_type}). {prompt}"]
    options = {}
    for c in cards:
        idx = c.get("index")
        name = c.get("name", "?")
        desc = c.get("description", "")
        lines.append(f"  [{idx}] {name}: {desc}")
        options[f"card_{idx}"] = f"Select '{name}' (index {idx})"

    state_text = "\n".join(lines)
    choice = ask_decision(state_text, options, f"On this '{screen_type}' card screen, which card should be selected? {prompt}")
    idx = int(choice.split("_")[1])
    do_action("select_card", index=idx)


def handle_map(state):
    map_state = state.get("map", {})
    next_options = map_state.get("next_options", [])
    if not next_options:
        print("  no travelable nodes yet, waiting...")
        return

    lines = ["Map screen. Choose the next node to travel to."]
    options = {}
    for opt in next_options:
        idx = opt["index"]
        room_type = opt.get("type", "?")
        leads_to = opt.get("leads_to", [])
        lookahead = ", leads to: " + ", ".join(c.get("type", "?") for c in leads_to) if leads_to else ""
        lines.append(f"  [{idx}] {room_type}{lookahead}")
        options[f"node_{idx}"] = f"Go to {room_type} node (index {idx}){lookahead}"

    player = state.get("player", {})
    lines.append(f"Player HP: {player.get('hp')}/{player.get('max_hp')}")
    lines.append(f"Relics: {relics_summary(player)}")

    state_text = "\n".join(lines)
    choice = ask_decision(
        state_text, options,
        "Which map node should be chosen next? Prefer rest sites when HP is low, "
        "take shops/treasure when healthy, avoid elites at low HP, and generally favor "
        "paths that lead toward more rest/shop opportunities."
    )
    idx = int(choice.split("_")[1])
    do_action("choose_map_node", index=idx)


def normalize_option(o, i):
    """Menu options can be plain strings or dicts with name/enabled - normalize to dict."""
    if isinstance(o, str):
        return {"name": o, "enabled": True}
    return o if isinstance(o, dict) else {"name": str(o), "enabled": True}


def handle_menu(state):
    raw_options = state.get("options", [])
    options_list = [normalize_option(o, i) for i, o in enumerate(raw_options)]
    enabled = [o for o in options_list if o.get("enabled", True)]
    if not enabled:
        print(f"  no enabled menu options, waiting... raw options={raw_options} full_state={json.dumps(state)[:600]}")
        return

    options = {f"opt_{i}": o.get("name", f"option_{i}") for i, o in enumerate(enabled)}
    state_text = f"Menu screen: {state.get('menu_screen')}. Message: {state.get('message', '')}\nOptions: " + ", ".join(options.values())
    choice = ask_decision(
        state_text, options,
        "Which menu option should be selected to progress toward starting or continuing a Slay the Spire 2 run? "
        "If a tutorial or info popup is active, dismiss it (advance/proceed). If a game mode or character choice "
        "is offered, pick a sensible default (standard mode, any available character)."
    )
    idx = int(choice.split("_")[1])
    chosen_name = enabled[idx].get("name")
    do_action("menu_select", option=chosen_name)
    time.sleep(1.0)


STATE_HANDLERS = {
    "map": handle_map,
    "event": handle_event,
    "rest_site": handle_rest_site,
    "card_reward": handle_card_reward,
    "rewards": handle_rewards,
    "shop": handle_shop,
    "treasure": handle_treasure,
    "relic_select": handle_relic_select,
    "hand_select": handle_hand_select,
    "card_select": handle_card_select,
    "game_over": handle_game_over,
}


def main_loop():
    print("Starting Jev <-> Slay the Spire 2 bridge. Ctrl+C to stop.")
    start_overlay_server()
    last_error = None
    while True:
        try:
            state = get_state()
            state_type = state.get("state_type", "unknown")

            in_combat = "battle" in state and "hand" in state.get("player", {})
            if in_combat:
                CURRENT_SCREEN[0] = f"combat:{state_type}"
                print(f"[combat:{state_type}] round {state.get('battle', {}).get('round')}")
                handle_combat(state)
            elif state_type in STATE_HANDLERS:
                CURRENT_SCREEN[0] = state_type
                print(f"[{state_type}]")
                STATE_HANDLERS[state_type](state)
            elif state_type == "menu":
                CURRENT_SCREEN[0] = "menu"
                print(f"[menu] {state.get('menu_screen')}")
                handle_menu(state)
            else:
                # Do NOT blindly send actions on unrecognized/transitional states (e.g. "unknown"
                # right after a run starts, before the player object is fully initialized) -
                # sending an invalid action here has been observed to leave the mod's HTTP
                # server permanently erroring afterward. Just wait for the state to settle.
                print(f"[{state_type}] no handler, waiting (not sending any action)...")

            last_error = None
        except Exception as e:
            if str(e) != last_error:
                print(f"[error] {e}")
                last_error = str(e)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main_loop()
