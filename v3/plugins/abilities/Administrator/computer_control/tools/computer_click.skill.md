`computer_click(x, y, button="left", clicks=1, interval_ms=100)` — move to the
coordinate and click. Use `clicks=2` for a real double-click; never simulate it
with two separate calls. Use right or middle click only when the observed UI
makes that intent unambiguous. Clicking is confirmation-gated in Ask/Plan modes
because it can cause external effects.
