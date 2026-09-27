# The wake models' license

The trained "hey TARS" models in this folder (`generic/hey_tars.tflite` and `generic/hey_tars_check.json`) are
licensed under [Creative Commons Attribution-NonCommercial-ShareAlike 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/)
(CC BY-NC-SA 4.0), unlike the code, which is MIT (see `../LICENSE`).

Why: the wake model was trained on microWakeWord's negative features (CC BY-NC 4.0) and on clips from Piper voices
fine-tuned from research-only voices (lessac, under the Blizzard 2013 license) or built on non-commercial ones (ryan,
CC BY-NC-SA), with non-commercial background audio mixed in. openWakeWord licenses its models the same way, for the
same reasons. Every source is listed in `../ATTRIBUTION.md`.

Models trained on a household's own recordings (`personal/`, and the versions `voice-assistant --install-models`
adds under `voice_data/`) stay on that household's machines and are never committed.
