# V3 boot animation

The initial HTML contains a canvas using the existing v2 bullet-grid renderer: identical dot displacement, influence radius, growth and easing. Its registry, palette and renderer load synchronously before boot.js starts the same seven-second circular phantom input. Both real and phantom pointers affect the grid during boot only.

While body.is-booting is set, all app surfaces are visibility-gated (retaining layout for initialization). Readiness removes the curtain, grid and glow synchronously before clearing the gate. There is no fade overlapping the app and no persistent background or card effects.

Authentication failure and the ten-second safety timeout also release the scene. Reduced motion skips the orbit. Shared v2 source is unchanged. Mounted UI changes require refresh, not a rebuild.
