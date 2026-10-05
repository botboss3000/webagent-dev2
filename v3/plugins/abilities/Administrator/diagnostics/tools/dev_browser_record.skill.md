# Record bounded PNG frames

Actions: `start`, `status`, `stop`. Capture runs only in this caller's authenticated private V3 browser and stops after 30 seconds or 30 frames, on quota failure, or when authorization is withdrawn.

The response includes actual PNG artifact metadata and `format: bounded_png_frames`, `video: false`. It does not claim an encoded video. Stop after the relevant interaction and close the browser. Screenshot capture invalidates input observations, so read again immediately before each input.
