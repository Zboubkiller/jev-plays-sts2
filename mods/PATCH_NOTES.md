# What's patched in this DLL

Base: [DarkArcZ/STS2MCP](https://github.com/DarkArcZ/STS2MCP) branch `fix/v0.111-compat`
([PR #132](https://github.com/Gennadiyev/STS2MCP/pull/132) against the upstream
[Gennadiyev/STS2MCP](https://github.com/Gennadiyev/STS2MCP)), which fixes the
`CombatManager.IsPlayPhase` removal that breaks combat state reads on game
v0.111+.

That PR was written against v0.111 and itself didn't compile against a
slightly newer game build we hit (the multiplayer-lobby API moved again).
Two more one-line patches in `McpMod.StateBuilder.cs`:

- `lobby.PlayerCount` → `lobby.Run?.Players?.Count ?? 0`
- `lobby.PlayerIds.Contains(sp.NetId)` → `true`

Both are in multiplayer-lobby code this project doesn't use (singleplayer
only), so they're safe stubs, not real fixes. If you hit a build error on a
newer game version, check the upstream repo's issues first. This compat
treadmill is ongoing since the game is still in active beta.

To rebuild yourself instead of using the prebuilt DLL:
```
git clone --branch fix/v0.111-compat https://github.com/DarkArcZ/STS2MCP.git
# apply the two patches above if needed, then:
cd STS2MCP
./build.ps1 -GameDir "C:\path\to\Slay the Spire 2"
```
Needs the .NET 9 SDK.
