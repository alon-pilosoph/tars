from pathlib import Path
from typing import Protocol

from .audio import Microphone


class Trigger(Protocol):
    def wait(self, mic: Microphone) -> None:
        """Block until the user wants to talk."""


class WakeWordTrigger:
    def __init__(self, model: str, threshold: float):
        """`model` is a pretrained openWakeWord name ("hey_jarvis") or a path to a custom .onnx file."""
        import openwakeword.utils
        from openwakeword.model import Model

        if model.endswith(".onnx"):
            path = Path(model)
            if not path.exists():
                raise SystemExit(f"Wake-word model {model!r} not found.")
            name = path.stem
        else:
            openwakeword.utils.download_models([model])  # No-op once cached.
            name = model
        self._model = Model(wakeword_models=[model], inference_framework="onnx")
        self._threshold = threshold
        self.phrase = name.replace("_", " ")

    def score(self, block) -> float:
        return max(self._model.predict(block).values())

    def wait(self, mic: Microphone) -> None:
        mic.clear()
        while self.score(mic.read()) < self._threshold:
            pass
        # Scores stay high for a few frames after a hit; reset so we don't re-trigger.
        self._model.reset()


class PushToTalkTrigger:
    def wait(self, mic: Microphone) -> None:
        input("Press Enter, then speak... ")
        mic.clear()
