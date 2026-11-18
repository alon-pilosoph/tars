# Wake models for the firmware

`hey_tars.tflite` is the 8-bit streaming build of `models/generic/hey_tars.tflite` (the same training run, #155:
`cdistill4-loud-11170412`, the average of its last five snapshots), which is what ESPHome's `micro_wake_word` runs:
int8 features in, a uint8 probability out, three 10 ms feature slices a step. `hey_tars.json` is its manifest
(ESPHome's v2 format). The cutoff is a first guess, to be measured on the device (P5 step 2): ESPHome averages the
last `sliding_window_size` steps, where the Pi takes each block's peak, so the Pi's `[wake] threshold` doesn't carry
over as it is.

Licence: CC BY-NC-SA 4.0, as every model in `models/` (`models/LICENSE.md`).
