# Wake models for the firmware

`hey_tars.tflite` is the 8-bit streaming build of `models/generic/hey_tars.tflite` (the same training run, #155:
`cdistill4-loud-11170412`, the average of its last five snapshots), which is what ESPHome's `micro_wake_word` runs:
int8 features in, a uint8 probability out, three 10 ms feature slices a step. `hey_tars.json` is its manifest
(ESPHome's v2 format): a cutoff of 0.47 over a window of one step, so it fires on one score of 0.47 or more, as the
Pi does with `[wake] threshold = 0.47` ([on a microcontroller](../../docs/wake-word.md#on-a-microcontroller)).

Licence: CC BY-NC-SA 4.0, as every model in `models/` (`models/LICENSE.md`).
