**Call this first — it's read-only and changes nothing.** It returns the menu of
models you can switch onto, each badged with what it can do — **sees images**,
**makes images**, **premium** tier — plus which model is **running now**, which is the
**default**, and the **effort levels** you can set.

Use it to switch by **capability, not by name**:

- The user **named a model** → you have the id already; pass it to `set_model('<id>')`
  (the menu also lets you confirm it's actually available).
- You're deciding **on your own** → read the menu and pick by need: something with
  **makes_images** for image generation, **sees_images** for sustained image work, or
  just call `use_premium_model()` for the premium tier. You never have to know the id.

If a capability isn't on the menu (`premium_available` / `image_out_available` is
false), it isn't configured — tell the user plainly rather than guessing or pretending.
