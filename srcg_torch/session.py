"""Cooperative PyTorch safe points and state restoration."""

from __future__ import annotations

from dataclasses import dataclass
import random
import signal
from types import FrameType
from typing import Any, Callable

import torch

from .checkpoint import AtomicCheckpointStore, CheckpointRecord
from .client import ReservationClient


@dataclass(frozen=True)
class RestoreResult:
    restored: bool
    global_step: int
    record: CheckpointRecord | None = None


class CheckpointSession:
    """Own signal flags and run checkpoint work only at a training safe point."""

    format_version = 1
    handled_signals = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)

    def __init__(
        self,
        store: AtomicCheckpointStore | None,
        every_steps: int = 0,
        checkpoint_client: ReservationClient | None = None,
    ) -> None:
        if every_steps < 0:
            raise ValueError("checkpoint interval cannot be negative")
        if every_steps and store is None:
            raise ValueError("periodic checkpoints require a checkpoint directory")
        self.store = store
        self.every_steps = every_steps
        self.checkpoint_client = checkpoint_client
        self.stop_signal: int | None = None
        self._previous_handlers: dict[int, Any] = {}

    def __enter__(self) -> "CheckpointSession":
        for number in self.handled_signals:
            self._previous_handlers[number] = signal.getsignal(number)
            signal.signal(number, self._request_stop)
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        for number, previous in self._previous_handlers.items():
            signal.signal(number, previous)
        self._previous_handlers.clear()

    def _request_stop(self, number: int, _frame: FrameType | None) -> None:
        # Signal handlers only set a flag.  torch.save/fsync happen after the
        # optimizer step where model and optimizer state are consistent.
        if self.stop_signal is None:
            self.stop_signal = number

    def request_stop_for_test(self, number: int = signal.SIGTERM) -> None:
        self.stop_signal = number

    def checkpoint_due(self, step: int) -> bool:
        return bool(self.every_steps and step > 0 and step % self.every_steps == 0)

    def save_at_safe_point(
        self,
        step: int,
        payload_factory: Callable[[], dict[str, Any]],
        *,
        force: bool = False,
    ) -> CheckpointRecord | None:
        if self.store is None or not (force or self.checkpoint_due(step) or self.stop_signal):
            return None
        if self.checkpoint_client is None:
            payload = payload_factory()
            payload["srcg_checkpoint_version"] = self.format_version
            payload["global_step"] = step
            return self.store.save(payload, step)
        with self.checkpoint_client.checkpoint():
            payload = payload_factory()
            payload["srcg_checkpoint_version"] = self.format_version
            payload["global_step"] = step
            return self.store.save(payload, step)


def capture_rng_state() -> dict[str, Any]:
    state: dict[str, Any] = {
        "python": random.getstate(),
        "torch_cpu": torch.get_rng_state(),
    }
    try:
        import numpy as np

        state["numpy"] = np.random.get_state()
    except ImportError:
        pass
    if torch.cuda.is_available():
        state["torch_cuda"] = torch.cuda.get_rng_state_all()
    return state


def restore_rng_state(state: dict[str, Any]) -> None:
    random.setstate(state["python"])
    # Checkpoints may be loaded with map_location="cuda" so model and
    # optimizer tensors are ready for use. That mapping also moves the saved
    # CPU generator state, but torch.set_rng_state requires a CPU ByteTensor.
    torch.set_rng_state(state["torch_cpu"].cpu())
    if "numpy" in state:
        import numpy as np

        np.random.set_state(state["numpy"])
    if "torch_cuda" in state and torch.cuda.is_available():
        torch.cuda.set_rng_state_all(
            [generator_state.cpu() for generator_state in state["torch_cuda"]]
        )
