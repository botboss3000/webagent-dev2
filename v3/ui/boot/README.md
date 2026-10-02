# V3 boot animation

The initial HTML contains a CSS dot grid and perspective floor, with no asynchronous background renderer. The seven-second circular boot input in boot.js adapts v2 and drives the boot-only glow.

While body.is-booting is set, all app surfaces are visibility-gated (retaining layout for initialization). Readiness removes the curtain, grid, floor and glow synchronously before clearing the gate. There is no fade overlapping the app and no persistent background or card effects.

Authentication failure and the ten-second safety timeout also release the scene. Reduced motion skips the orbit. Shared v2 source is unchanged. Mounted UI changes require refresh, not a rebuild.
