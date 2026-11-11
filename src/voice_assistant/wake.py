"""What starts a conversation: a wake-word model listening all the time, or Enter for push-to-talk."""

from collections.abc import Callable
from concurrent.futures import Future
from pathlib import Path
from typing import Protocol

import numpy as np

from .audio import BLOCK_SAMPLES, Microphone

# What wait() returns when a reminder came due while it waited, instead of a wake.
DUE = "due"
# Something wait() asks on every block of audio: is a reminder due? It must be quick.
Due = Callable[[], bool]


class Trigger(Protocol):
    last_audio: np.ndarray | None  # what woke it, for speaker ID; None when there's nothing to go on
    after_name: Future[np.ndarray] | None  # what followed the name before the wake fired; None when it isn't known

    def wait(self, mic: Microphone, due: Due | None = None) -> str | None:
        """Block until the user wants to talk (a double-checked trigger says how sure it is: verify.ANSWER or ASK),
        or until `due` says a reminder is due (DUE). Push-to-talk never asks `due`."""


class WakeModel(Protocol):
    threshold: float
    phrase: str

    def score(self, block: np.ndarray) -> float: ...

    def reset(self) -> None: ...


class WakeWordTrigger:
    last_audio = after_name = None

    def __init__(self, model: str, threshold: float):
        """`model` is a pretrained openWakeWord name ("hey_jarvis") or a path to a custom .onnx file."""
        import openwakeword.utils
        from openwakeword.model import Model

        if model.endswith(".onnx"):
            path = Path(model)
            if not path.exists():
                raise FileNotFoundError(f"Wake-word model {model!r} not found.")
            name = path.stem
        else:
            openwakeword.utils.download_models([model])  # no-op once cached
            name = model
        self._model = _load(Model, model, openwakeword.utils.download_models)
        self.threshold = threshold
        self.phrase = name.replace("_", " ")

    def score(self, block: np.ndarray) -> float:
        return max(self._model.predict(block).values())

    def reset(self) -> None:
        # Scores stay high for a few frames after a hit; resetting stops a re-trigger.
        self._model.reset()

    def wait(self, mic: Microphone, due: Due | None = None) -> str | None:
        mic.clear()
        while self.score(mic.read()) < self.threshold:
            if due and due():
                return DUE
        self.reset()
        return None


class MicroWakeWordTrigger:
    """A microWakeWord streaming model (.tflite), like the ones trained for "hey TARS".

    Audio goes through the micro_speech frontend in 10 ms steps. The model scores every few steps and keeps its own
    streaming state, so a fresh interpreter is the only clean reset. A fresh one's zeroed state scores about 0.16 on
    its first block, even on silence, which a low threshold takes for a wake; so reset() primes it with faint noise.
    """

    STEP_BYTES = 160 * 2  # 10 ms of 16-bit audio
    PRIMER = np.random.default_rng(0).normal(0, 40, 3 * BLOCK_SAMPLES).astype(np.int16)
    last_audio = after_name = None

    def __init__(self, model: str, threshold: float):
        path = Path(model)
        if not path.exists():
            raise FileNotFoundError(f"Wake-word model {model!r} not found.")
        self._path = str(path)
        self.threshold = threshold
        self.phrase = path.stem.replace("_", " ")
        self.reset()

    def reset(self) -> None:
        from ai_edge_litert.interpreter import Interpreter
        from pymicro_features import MicroFrontend

        self._model = Interpreter(model_path=self._path)
        self._model.allocate_tensors()
        self._input = self._model.get_input_details()[0]
        self._output = self._model.get_output_details()[0]
        self._quantized = self._input["dtype"] == np.int8
        self._slices_per_score = self._input["shape"][1]
        self._frontend = MicroFrontend()
        self._pending = b""
        self._features = []
        self.score(self.PRIMER)

    def _run(self, chunk: np.ndarray) -> float:
        if self._quantized:
            scale, zero = self._input["quantization"]
            chunk = np.clip(np.round(chunk / scale + zero), -128, 127).astype(np.int8)
        self._model.set_tensor(self._input["index"], chunk.reshape(self._input["shape"]))
        self._model.invoke()
        out = self._model.get_tensor(self._output["index"])[0][0]
        if self._output["dtype"] != np.float32:
            scale, zero = self._output["quantization"]
            out = (float(out) - zero) * scale
        return float(out)

    def score(self, block: np.ndarray) -> float:
        """Highest score produced while consuming this block (0 until enough audio has arrived)."""
        audio = self._pending + np.asarray(block, dtype=np.int16).tobytes()
        best, i = 0.0, 0
        while i + self.STEP_BYTES <= len(audio):
            result = self._frontend.process_samples(audio[i : i + self.STEP_BYTES])
            i += result.samples_read * 2
            if result.features:
                self._features.append(result.features)
                if len(self._features) == self._slices_per_score:
                    chunk = np.array(self._features, dtype=np.float32)  # already scaled like the training features
                    best = max(best, self._run(chunk))
                    self._features = []
        self._pending = audio[i:]
        return best

    def wait(self, mic: Microphone, due: Due | None = None) -> str | None:
        mic.clear()
        while self.score(mic.read()) < self.threshold:
            if due and due():
                return DUE
        self.reset()
        return None


def wake_word_trigger(model: str, threshold: float) -> WakeWordTrigger | MicroWakeWordTrigger:
    if model.endswith(".tflite"):
        return MicroWakeWordTrigger(model, threshold)
    return WakeWordTrigger(model, threshold)


class PushToTalkTrigger:
    last_audio = after_name = None

    def wait(self, mic: Microphone, due: Due | None = None) -> None:
        input("Press Enter, then speak... ")
        mic.clear()


DOWNLOAD_TRIES = 3


def _load(make, model: str, download):
    """openWakeWord's model, made again after fetching its files afresh if one came down broken: its download keeps
    whatever arrived (a failed request's error page among them) and never fetches a file that exists, so one broken
    download would otherwise stop the wake word for good. `make`: openwakeword.model.Model; `download`: its
    download_models."""
    for attempt in range(1, DOWNLOAD_TRIES + 1):
        try:
            return make(wakeword_models=[model], inference_framework="onnx")
        except Exception as e:  # onnxruntime's errors aren't one type; a real fault is raised below
            broken = _broken_files(model)
            if attempt == DOWNLOAD_TRIES or not broken:
                raise
            print(f"(wake: {', '.join(p.name for p in broken)} came down broken ({type(e).__name__}); fetching again)")
            for path in broken:
                path.unlink(missing_ok=True)
                # openWakeWord fetches a feature model again only when its .tflite is missing, and both together.
                path.with_suffix(".tflite").unlink(missing_ok=True)
            download([] if model.endswith(".onnx") else [model])
    raise AssertionError("unreachable")


def _broken_files(model: str) -> list[Path]:
    """openWakeWord's downloaded .onnx files (its feature models, and `model` if it's one of its own) that onnxruntime
    can't read."""
    import onnxruntime
    import openwakeword

    folder = Path(openwakeword.__file__).parent / "resources" / "models"
    names = [Path(m["download_url"]).with_suffix(".onnx").name for m in openwakeword.FEATURE_MODELS.values()]
    if not model.endswith(".onnx"):
        names += [p.name for p in folder.glob(f"{model}*.onnx")]
    broken = []
    for path in (folder / n for n in names):
        if not path.exists():
            continue
        try:
            onnxruntime.InferenceSession(str(path), providers=["CPUExecutionProvider"])
        except Exception:  # noqa: BLE001 - any load failure is a broken file
            broken.append(path)
    return broken
