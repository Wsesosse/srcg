#!/usr/bin/env python3
"""Full COCO and Task06_Lung experiments with optional SRCG admission.

The default path uses full datasets, standard PyTorch DataLoader batching, and
non-toy models.  When SRCG manager IPC is present, each DataLoader worker asks
for RAM before it decodes the batch assigned to it; the user still supplies
only ordinary training arguments.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import gzip
import hashlib
import json
import math
import multiprocessing
import os
from pathlib import Path
import random
import resource
import struct
import sys
import time
from typing import Callable, Iterator, Sequence

import torch
from torch import nn
from torch.nn import functional as F
from torch.utils.data import DataLoader, Dataset, Sampler, default_collate


MIB = 1024 * 1024
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from srcg_torch import (
    AtomicCheckpointStore,
    CheckpointSession,
    ReservationClient,
    ReservationToken,
)
from srcg_torch.session import capture_rng_state, restore_rng_state


@dataclass(frozen=True)
class SamplePlan:
    index: int
    sample_id: str
    source_shape: tuple[int, ...]
    load_peak_bytes: int
    resident_bytes: int


@dataclass(frozen=True)
class BatchPlan:
    batch_id: int
    indices: tuple[int, ...]
    sample_ids: tuple[str, ...]
    estimated_extra_bytes: int


@dataclass
class ReservedBatch:
    """A collated DataLoader batch carrying its worker-acquired RAM token."""

    inputs: torch.Tensor | None
    targets: torch.Tensor | None
    token: ReservationToken
    sample_ids: tuple[str, ...]
    source_shapes: tuple[tuple[int, ...], ...]
    load_ms: float
    retry_released: int
    retry_pending: int
    estimated_resident_bytes: int
    estimated_resident_with_safety_bytes: int
    shrink_decision: str | None
    shrink_borrowed_bytes: int
    shrink_returned_bytes: int
    shrink_ms: float
    memory_trace: dict[str, object] | None

    def pin_memory(self) -> "ReservedBatch":
        if self.inputs is None or self.targets is None:
            raise RuntimeError("cannot pin a consumed reserved batch")
        self.inputs = self.inputs.pin_memory()
        self.targets = self.targets.pin_memory()
        return self

    def take_tensors(self) -> tuple[torch.Tensor, torch.Tensor]:
        """Transfer ownership of CPU tensors to the training loop.

        Clearing these references is required before early RELEASE; otherwise
        the transport object itself retains the complete CPU batch until the
        optimizer step finishes.
        """
        if self.inputs is None or self.targets is None:
            raise RuntimeError("reserved batch tensors were already consumed")
        inputs, targets = self.inputs, self.targets
        self.inputs = None
        self.targets = None
        return inputs, targets


class ReservedBatchDataset(Dataset[tuple[torch.Tensor, torch.Tensor]]):
    """Reserve in the DataLoader worker immediately before materializing a batch.

    PyTorch calls ``__getitems__`` once per auto-collated batch.  Keeping the
    reservation at this boundary allows ordinary multi-process prefetch: a
    worker that receives WAIT blocks before the underlying dataset decodes a
    JPEG or NIfTI volume. The token travels with the collated batch, shrinks
    after temporary preprocessing allocations disappear, and is released
    after the CPU-to-device transfer rather than after the optimizer step.
    """

    def __init__(
        self,
        dataset: Dataset[tuple[torch.Tensor, torch.Tensor]],
        plans: Sequence[SamplePlan],
        *,
        safety_factor: float,
        timeout_ms: int,
        retry_ms: int,
        release_safety_bytes: int,
        admission_safety_bytes: int | None = None,
        phase_mode: str = "legacy",
        memory_trace: bool = False,
    ) -> None:
        self.dataset = dataset
        self.plans = plans
        self.safety_factor = safety_factor
        self.timeout_ms = timeout_ms
        self.retry_ms = retry_ms
        self.release_safety_bytes = release_safety_bytes
        self.admission_safety_bytes = (
            release_safety_bytes
            if admission_safety_bytes is None
            else admission_safety_bytes
        )
        self.phase_mode = phase_mode
        self.memory_trace = memory_trace
        # The first optimizer step may lazily allocate host-side framework
        # state after a worker sampled memory.current. This value is shared by
        # Linux DataLoader workers so only startup batches retain extra quota.
        self._consumer_warm = multiprocessing.Value("b", False, lock=False)
        self._client_pid: int | None = None
        self._client: ReservationClient | None = None

    def __len__(self) -> int:
        return len(self.dataset)

    def mark_consumer_warm(self) -> None:
        self._consumer_warm.value = True

    def _client_for_process(self) -> ReservationClient:
        process_id = os.getpid()
        if self._client is None or self._client_pid != process_id:
            self._client = ReservationClient.from_environment(
                timeout_ms=self.timeout_ms,
                retry_ms=self.retry_ms,
                release_safety_bytes=self.release_safety_bytes,
                admission_safety_bytes=self.admission_safety_bytes,
            )
            self._client_pid = process_id
        if self._client is None:
            raise RuntimeError("SRCG reservation environment disappeared in DataLoader worker")
        return self._client

    def __getitem__(self, index: int) -> tuple[torch.Tensor, torch.Tensor]:
        # DataLoader 2.x uses __getitems__ for auto-collated map datasets.  This
        # fallback preserves Dataset semantics but intentionally rejects a path
        # that would reserve one sample instead of one complete batch.
        raise RuntimeError(
            "ReservedBatchDataset requires batched DataLoader access through __getitems__"
        )

    def __getitems__(self, indices: list[int]) -> list[ReservedBatch]:
        if not indices:
            raise RuntimeError("DataLoader requested an empty batch")
        plans = [self.plans[index] for index in indices]
        requested_bytes = estimate_batch_bytes(plans, self.safety_factor)
        estimated_resident_bytes = sum(plan.resident_bytes for plan in plans)
        trace: dict[str, object] | None = {} if self.memory_trace else None
        if trace is not None:
            trace["before_reserve"] = read_cgroup_memory_point()
        client = self._client_for_process()
        retry_released, retry_pending = client.retry_pending()
        token = client.reserve(requested_bytes)
        if trace is not None:
            trace["after_grant"] = read_cgroup_memory_point()
            trace["load_started_monotonic_ms"] = monotonic_milliseconds()
        started = time.perf_counter()
        shrink_decision: str | None = None
        shrink_borrowed_bytes = token.borrowed_bytes
        shrink_returned_bytes = 0
        shrink_ms = 0.0
        try:
            samples = [self.dataset[index] for index in indices]
            inputs, targets = default_collate(samples)
            if trace is not None:
                # This point is deliberately recorded before __getitems__
                # returns.  The local ``samples`` list still exists, exactly
                # as it does without tracing; instrumentation must not change
                # object lifetime or DataLoader behaviour.
                trace["after_collate_before_return"] = read_cgroup_memory_point()
            # Collation has copied each decoded sample into the resident batch
            # tensors. Drop the temporary sample tensors in this worker before
            # telling SRCG that the decode/preprocess peak has ended. Doing
            # this here is essential with DataLoader prefetch: waiting until
            # the main process receives the batch leaves full-size tokens in
            # the transport queue and can block every worker.
            del samples
            if self.phase_mode in {"shrink-only", "phase-aware"}:
                startup_allowance_bytes = (
                    0 if self._consumer_warm.value else 256 * MIB
                )
                shrink_started = time.perf_counter()
                shrink_result = client.shrink(
                    token,
                    math.ceil(estimated_resident_bytes * self.safety_factor)
                    + startup_allowance_bytes,
                )
                shrink_ms = (time.perf_counter() - shrink_started) * 1000.0
                shrink_decision = shrink_result.decision
                shrink_borrowed_bytes = shrink_result.borrowed_bytes
                shrink_returned_bytes = shrink_result.returned_bytes
                if shrink_result.decision == "DENIED":
                    raise RuntimeError(f"SRCG shrink denied: {shrink_result.reason}")
                if shrink_result.decision not in {
                    "SHRUNK",
                    "UNCHANGED",
                    "ALREADY_RELEASED",
                }:
                    raise RuntimeError(
                        f"unexpected SRCG shrink response: {shrink_result.decision}"
                    )
                if trace is not None:
                    trace["after_worker_shrink"] = read_cgroup_memory_point()
        except BaseException:
            client.release(token)
            raise
        return [
            ReservedBatch(
                inputs=inputs,
                targets=targets,
                token=token,
                sample_ids=tuple(plan.sample_id for plan in plans),
                source_shapes=tuple(plan.source_shape for plan in plans),
                load_ms=(time.perf_counter() - started) * 1000.0,
                retry_released=retry_released,
                retry_pending=retry_pending,
                estimated_resident_bytes=estimated_resident_bytes,
                estimated_resident_with_safety_bytes=math.ceil(
                    estimated_resident_bytes * self.safety_factor
                ),
                shrink_decision=shrink_decision,
                shrink_borrowed_bytes=shrink_borrowed_bytes,
                shrink_returned_bytes=shrink_returned_bytes,
                shrink_ms=shrink_ms,
                memory_trace=trace,
            )
        ]


def unwrap_reserved_batch(items: list[ReservedBatch]) -> ReservedBatch:
    if len(items) != 1 or not isinstance(items[0], ReservedBatch):
        raise RuntimeError("invalid reserved DataLoader batch")
    return items[0]


@dataclass
class ReservationStats:
    grants: int = 0
    wait_events: int = 0
    wait_ms: float = 0.0
    admission_ms: float = 0.0
    released: int = 0
    pending_releases: int = 0
    retried_releases: int = 0
    shrink_attempts: int = 0
    shrink_successes: int = 0
    shrink_unchanged: int = 0
    shrink_returned_bytes: int = 0
    shrink_ms: float = 0.0
    early_release_attempts: int = 0
    early_release_ms: float = 0.0

    def observe(self, batch: ReservedBatch) -> None:
        self.grants += 1
        self.wait_events += batch.token.wait_count
        self.wait_ms += batch.token.wait_ms
        self.admission_ms += batch.token.admission_ms
        self.retried_releases += batch.retry_released
        self.pending_releases += batch.retry_pending
        if batch.shrink_decision is not None:
            self.observe_shrink(
                batch.shrink_decision,
                batch.shrink_returned_bytes,
                batch.shrink_ms,
            )

    def observe_shrink(
        self, decision: str, returned_bytes: int, elapsed_ms: float
    ) -> None:
        self.shrink_attempts += 1
        self.shrink_ms += elapsed_ms
        if decision == "SHRUNK":
            self.shrink_successes += 1
            self.shrink_returned_bytes += returned_bytes
        elif decision in {"UNCHANGED", "ALREADY_RELEASED"}:
            self.shrink_unchanged += 1


def emit(event: str, **fields: object) -> None:
    print(json.dumps({"event": event, **fields}, sort_keys=True), flush=True)


def emit_run_config(args: argparse.Namespace) -> None:
    config: dict[str, object] = {}
    for key, value in vars(args).items():
        if isinstance(value, Path):
            config[key] = str(value)
        elif isinstance(value, tuple):
            config[key] = list(value)
        else:
            config[key] = value
    emit("run_config", config=config)


def read_rss_bytes() -> int:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except OSError:
        pass
    return 0


def monotonic_milliseconds() -> int:
    """Timestamp on the same monotonic clock used by SRCG controller logs."""
    return time.monotonic_ns() // 1_000_000


def read_cgroup_memory_point() -> dict[str, object]:
    """One timestamped cgroup reading for opt-in per-batch diagnostics.

    ``memory.current`` alone cannot distinguish process/tensor memory from the
    filesystem cache charged to the cgroup.  Keep the small set of
    ``memory.stat`` counters needed for that diagnosis alongside each phase.
    """
    snapshot = read_cgroup_memory()
    selected_stat_names = {
        "anon",
        "file",
        "shmem",
        "kernel",
        "kernel_stack",
        "inactive_file",
        "active_file",
        "pagetables",
        "slab",
        "slab_reclaimable",
        "sock",
    }
    stat_bytes: dict[str, int] = {}
    cgroup_path = snapshot["cgroup_path"]
    if isinstance(cgroup_path, str):
        try:
            for line in (Path(cgroup_path) / "memory.stat").read_text(
                encoding="utf-8"
            ).splitlines():
                name, value = line.split(maxsplit=1)
                if name in selected_stat_names:
                    stat_bytes[name] = int(value)
        except (OSError, ValueError):
            stat_bytes = {}
    return {
        "monotonic_time_ms": monotonic_milliseconds(),
        "current_bytes": snapshot["cgroup_memory_current_bytes"],
        "peak_bytes": snapshot["cgroup_memory_peak_bytes"],
        "limit_bytes": snapshot["cgroup_memory_max_bytes"],
        "stat_bytes": stat_bytes,
    }


def jpeg_dimensions(path: Path) -> tuple[int, int, int]:
    """Read (height, width, components) without decoding JPEG pixels."""
    start_of_frame = {
        0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
        0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
    }
    with path.open("rb") as stream:
        if stream.read(2) != b"\xff\xd8":
            raise ValueError(f"not a JPEG file: {path}")

        while True:
            byte = stream.read(1)
            if not byte:
                break
            if byte != b"\xff":
                continue
            while byte == b"\xff":
                byte = stream.read(1)
            if not byte:
                break
            marker = byte[0]
            if marker in {0x01, *range(0xD0, 0xD9)}:
                continue
            raw_length = stream.read(2)
            if len(raw_length) != 2:
                break
            length = struct.unpack(">H", raw_length)[0]
            if length < 2:
                raise ValueError(f"invalid JPEG segment in {path}")
            if marker in start_of_frame:
                payload = stream.read(6)
                if len(payload) != 6:
                    break
                _, height, width, components = struct.unpack(">BHHB", payload)
                return height, width, components
            stream.seek(length - 2, 1)

    raise ValueError(f"JPEG dimensions not found: {path}")


def nifti_shape(path: Path) -> tuple[int, ...]:
    """Read a NIfTI-1 shape from its header without materializing the volume."""
    opener = gzip.open if path.name.endswith(".gz") else open
    with opener(path, "rb") as stream:
        header = stream.read(56)
    if len(header) < 56:
        raise ValueError(f"truncated NIfTI header: {path}")

    if struct.unpack("<i", header[:4])[0] == 348:
        endian = "<"
    elif struct.unpack(">i", header[:4])[0] == 348:
        endian = ">"
    else:
        raise ValueError(f"unsupported NIfTI-1 header: {path}")

    dimensions = struct.unpack(f"{endian}8h", header[40:56])
    rank = dimensions[0]
    if rank < 1 or rank > 7:
        raise ValueError(f"invalid NIfTI rank {rank}: {path}")
    shape = tuple(int(value) for value in dimensions[1 : rank + 1])
    if any(value <= 0 for value in shape):
        raise ValueError(f"invalid NIfTI shape {shape}: {path}")
    return shape


class CocoResizeDataset(Dataset[tuple[torch.Tensor, torch.Tensor]]):
    """COCO val2017 JPEG decode -> resize workload.

    The numeric image id is used as a deterministic pseudo-label.  Accuracy is
    deliberately meaningless; this workload measures the loading path.
    """

    def __init__(
        self,
        root: Path,
        resize: tuple[int, int],
        num_classes: int,
        limit: int | None,
    ) -> None:
        self.root = root
        self.resize = resize
        self.num_classes = num_classes
        paths = sorted(
            path for path in root.iterdir()
            if path.is_file()
            and path.suffix.lower() in {".jpg", ".jpeg"}
            and not path.name.startswith("._")
        )
        self.paths = paths[:limit] if limit is not None else paths
        if not self.paths:
            raise ValueError(f"no JPEG files found under {root}")
        self.plans = tuple(self._make_plan(index, path) for index, path in enumerate(self.paths))

    def _make_plan(self, index: int, path: Path) -> SamplePlan:
        height, width, _ = jpeg_dimensions(path)
        target_height, target_width = self.resize
        compressed_input = path.stat().st_size
        decoded_rgb = height * width * 3
        resized_rgb = target_height * target_width * 3
        output_float = target_height * target_width * 3 * 4
        return SamplePlan(
            index=index,
            sample_id=path.stem,
            source_shape=(height, width, 3),
            # Reading the JPEG also charges its filesystem-cache pages to the
            # job cgroup until the kernel can reclaim them.
            load_peak_bytes=(
                compressed_input + decoded_rgb + resized_rgb + output_float
            ),
            resident_bytes=output_float + 8,
        )

    def __len__(self) -> int:
        return len(self.paths)

    def __getitem__(self, index: int) -> tuple[torch.Tensor, torch.Tensor]:
        try:
            from PIL import Image
        except ImportError as exc:
            raise RuntimeError("COCO workload requires Pillow; install requirements.txt") from exc

        with Image.open(self.paths[index]) as opened:
            source = opened.convert("RGB")
        target_height, target_width = self.resize
        resized = source.resize((target_width, target_height), Image.Resampling.BILINEAR)
        pixels = bytearray(resized.tobytes())
        image = (
            torch.frombuffer(pixels, dtype=torch.uint8)
            .reshape(target_height, target_width, 3)
            .permute(2, 0, 1)
            .float()
            .div_(255.0)
        )
        label = torch.tensor(int(self.paths[index].stem) % self.num_classes, dtype=torch.long)
        return image, label


class LungVolumeDataset(Dataset[tuple[torch.Tensor, torch.Tensor]]):
    """Task06_Lung full-volume NIfTI load followed by a fixed 3D resize."""

    def __init__(self, root: Path, volume_size: tuple[int, int, int], limit: int | None) -> None:
        self.root = root
        self.volume_size = volume_size
        image_root = root / "imagesTr"
        label_root = root / "labelsTr"
        paths = sorted(
            path for path in image_root.glob("*.nii.gz")
            if not path.name.startswith("._")
        )
        self.image_paths = paths[:limit] if limit is not None else paths
        self.label_paths = [label_root / path.name for path in self.image_paths]
        if not self.image_paths:
            raise ValueError(f"no NIfTI files found under {image_root}")
        missing = [path for path in self.label_paths if not path.is_file()]
        if missing:
            raise ValueError(f"missing label for {missing[0].name}")
        self.plans = tuple(
            self._make_plan(index, image_path, label_path)
            for index, (image_path, label_path) in enumerate(zip(self.image_paths, self.label_paths))
        )

    def _make_plan(self, index: int, image_path: Path, label_path: Path) -> SamplePlan:
        image_shape = nifti_shape(image_path)
        label_shape = nifti_shape(label_path)
        if image_shape != label_shape:
            raise ValueError(f"image/label shape mismatch for {image_path.name}")
        if len(image_shape) != 3:
            raise ValueError(f"Task06_Lung sample is not 3D: {image_path}")

        source_voxels = math.prod(image_shape)
        target_voxels = math.prod(self.volume_size)
        source_image_float = source_voxels * 4
        source_label_float = source_voxels * 4
        target_image_float = target_voxels * 4
        target_label_float = target_voxels * 4
        target_label_long = target_voxels * 8
        compressed_inputs = image_path.stat().st_size + label_path.stat().st_size
        return SamplePlan(
            index=index,
            sample_id=image_path.name.removesuffix(".nii.gz"),
            source_shape=(image_shape[2], image_shape[0], image_shape[1]),
            load_peak_bytes=(
                compressed_inputs
                + source_image_float
                + source_label_float
                + target_image_float
                + target_label_float
                + target_label_long
            ),
            resident_bytes=target_image_float + target_label_long,
        )

    def __len__(self) -> int:
        return len(self.image_paths)

    def __getitem__(self, index: int) -> tuple[torch.Tensor, torch.Tensor]:
        try:
            import nibabel as nib
            import numpy as np
        except ImportError as exc:
            raise RuntimeError(
                "Task06_Lung workload requires NumPy and nibabel; install requirements.txt"
            ) from exc

        image_nii = nib.load(str(self.image_paths[index]))
        label_nii = nib.load(str(self.label_paths[index]))
        image_array = image_nii.get_fdata(dtype=np.float32, caching="unchanged")
        label_array = label_nii.get_fdata(dtype=np.float32, caching="unchanged")

        np.clip(image_array, -1000.0, 1000.0, out=image_array)
        image_array += 1000.0
        image_array *= 1.0 / 2000.0

        image_source = torch.from_numpy(image_array).permute(2, 0, 1).unsqueeze(0).unsqueeze(0)
        label_source = torch.from_numpy(label_array).permute(2, 0, 1).unsqueeze(0).unsqueeze(0)
        image = F.interpolate(
            image_source,
            size=self.volume_size,
            mode="trilinear",
            align_corners=False,
        )[0]
        label_float = F.interpolate(label_source, size=self.volume_size, mode="nearest")[0, 0]
        label = label_float.round().long()
        return image, label


def estimate_batch_bytes(
    batch_plans: Sequence[SamplePlan],
    safety_factor: float,
) -> int:
    resident = 0
    peak = 0
    for plan in batch_plans:
        peak = max(peak, resident + plan.load_peak_bytes)
        resident += plan.resident_bytes
    peak = max(peak, resident * 2)  # default_collate allocates the batch tensors
    return math.ceil(peak * safety_factor)


class PlanningBatchSampler(Sampler[list[int]]):
    """Deterministic batching with committed progress for exact resume.

    DataLoader may request batches ahead of the training loop.  Iteration does
    not advance the durable cursor; ``mark_batch_consumed`` does that only
    after ``optimizer.step`` succeeds.
    """

    def __init__(
        self,
        plans: Sequence[SamplePlan],
        batch_size: int,
        shuffle: bool,
        seed: int,
        safety_factor: float,
        before_load: Callable[[BatchPlan], None] | None = None,
    ) -> None:
        self.plans = plans
        self.batch_size = batch_size
        self.shuffle = shuffle
        self.seed = seed
        self.safety_factor = safety_factor
        self.before_load = before_load
        self.epoch = 0
        self.next_batch_index = 0

    def __len__(self) -> int:
        return math.ceil(len(self.plans) / self.batch_size)

    def __iter__(self) -> Iterator[list[int]]:
        if self.shuffle:
            generator = torch.Generator().manual_seed(self.seed + self.epoch)
            order = torch.randperm(len(self.plans), generator=generator).tolist()
        else:
            order = list(range(len(self.plans)))
        offsets = range(0, len(order), self.batch_size)
        for zero_based_batch, offset in enumerate(offsets):
            if zero_based_batch < self.next_batch_index:
                continue
            indices = order[offset : offset + self.batch_size]
            plans = [self.plans[index] for index in indices]
            plan = BatchPlan(
                batch_id=zero_based_batch + 1,
                indices=tuple(indices),
                sample_ids=tuple(sample.sample_id for sample in plans),
                estimated_extra_bytes=estimate_batch_bytes(plans, self.safety_factor),
            )
            if self.before_load is not None:
                self.before_load(plan)
            yield indices

    def mark_batch_consumed(self) -> None:
        if self.next_batch_index >= len(self):
            raise RuntimeError("all batches in the sampler epoch are already committed")
        self.next_batch_index += 1
        if self.next_batch_index == len(self):
            self.epoch += 1
            self.next_batch_index = 0

    def state_dict(self) -> dict[str, int]:
        return {"epoch": self.epoch, "next_batch_index": self.next_batch_index}

    def load_state_dict(self, state: dict[str, int]) -> None:
        epoch = int(state["epoch"])
        next_batch = int(state["next_batch_index"])
        if epoch < 0 or next_batch < 0 or next_batch >= len(self):
            raise ValueError("invalid PlanningBatchSampler checkpoint state")
        self.epoch = epoch
        self.next_batch_index = next_batch


class PlanObserver:
    def __init__(
        self,
        client: ReservationClient | None = None,
        *,
        emit_plans: bool = True,
    ) -> None:
        self.client = client
        self.emit_plans = emit_plans
        self.pending: BatchPlan | None = None
        self.pending_token: ReservationToken | None = None
        self.load_started_at = 0.0
        self.grants = 0
        self.wait_events = 0
        self.wait_ms = 0.0
        self.admission_ms = 0.0
        self.released = 0
        self.pending_releases = 0

    def before_load(self, plan: BatchPlan) -> None:
        if self.pending is not None:
            raise RuntimeError("DataLoader requested another batch before the previous plan was consumed")
        self.pending = plan
        if self.emit_plans:
            emit(
                "batch_plan",
                **asdict(plan),
                estimated_extra_mib=round(plan.estimated_extra_bytes / MIB, 2),
            )
        if self.client is not None:
            released, pending = self.client.retry_pending()
            if released or pending:
                emit("reservation_retry", released=released, pending=pending)
                self.released += released
            token = self.client.reserve(plan.estimated_extra_bytes)
            self.pending_token = token
            self.grants += 1
            self.wait_events += token.wait_count
            self.wait_ms += token.wait_ms
            self.admission_ms += token.admission_ms
            emit(
                "reservation_granted",
                batch_id=plan.batch_id,
                token=token.value,
                requested_bytes=token.requested_bytes,
                borrowed_bytes=token.borrowed_bytes,
                effective_limit_bytes=token.effective_limit_bytes,
                wait_count=token.wait_count,
                wait_ms=round(token.wait_ms, 3),
                admission_ms=round(token.admission_ms, 3),
            )
        self.load_started_at = time.monotonic()

    def consume(self) -> tuple[BatchPlan, float]:
        if self.pending is None:
            raise RuntimeError("batch was loaded without a preceding plan")
        plan = self.pending
        load_ms = (time.monotonic() - self.load_started_at) * 1000.0
        self.pending = None
        return plan, load_ms

    def release(self) -> None:
        token = self.pending_token
        self.pending_token = None
        if token is None or self.client is None:
            return
        result = self.client.release(token)
        if result.decision in {"RELEASED", "ALREADY_RELEASED"}:
            self.released += 1
        elif result.decision == "PENDING":
            self.pending_releases += 1
        elif result.decision == "DENIED":
            raise RuntimeError(f"SRCG release denied: {result.reason}")
        emit(
            "reservation_release",
            token=token.value,
            decision=result.decision,
            reason=result.reason,
            effective_limit_bytes=result.effective_limit_bytes,
            reserved_memory_available_bytes=result.reserved_memory_available_bytes,
        )


class TinyClassifier2D(nn.Module):
    """Small model retained only for automated correctness tests."""

    def __init__(self, num_classes: int) -> None:
        super().__init__()
        self.features = nn.Sequential(
            nn.Conv2d(3, 16, kernel_size=3, stride=2, padding=1),
            nn.ReLU(inplace=True),
            nn.Conv2d(16, 32, kernel_size=3, stride=2, padding=1),
            nn.ReLU(inplace=True),
            nn.AdaptiveAvgPool2d(1),
        )
        self.classifier = nn.Linear(32, num_classes)

    def forward(self, inputs: torch.Tensor) -> torch.Tensor:
        return self.classifier(self.features(inputs).flatten(1))


class TinySegmenter3D(nn.Module):
    """Small model retained only for automated correctness tests."""

    def __init__(self) -> None:
        super().__init__()
        self.encoder = nn.Sequential(
            nn.Conv3d(1, 4, kernel_size=3, padding=1),
            nn.ReLU(inplace=True),
            nn.Conv3d(4, 8, kernel_size=3, stride=2, padding=1),
            nn.ReLU(inplace=True),
            nn.Conv3d(8, 8, kernel_size=3, padding=1),
            nn.ReLU(inplace=True),
        )
        self.head = nn.Conv3d(8, 2, kernel_size=1)

    def forward(self, inputs: torch.Tensor) -> torch.Tensor:
        logits = self.head(self.encoder(inputs))
        return F.interpolate(logits, size=inputs.shape[2:], mode="trilinear", align_corners=False)


class Bottleneck2D(nn.Module):
    expansion = 4

    def __init__(
        self,
        input_channels: int,
        channels: int,
        *,
        stride: int = 1,
    ) -> None:
        super().__init__()
        output_channels = channels * self.expansion
        self.main = nn.Sequential(
            nn.Conv2d(input_channels, channels, kernel_size=1, bias=False),
            nn.BatchNorm2d(channels),
            nn.ReLU(inplace=True),
            nn.Conv2d(
                channels,
                channels,
                kernel_size=3,
                stride=stride,
                padding=1,
                bias=False,
            ),
            nn.BatchNorm2d(channels),
            nn.ReLU(inplace=True),
            nn.Conv2d(channels, output_channels, kernel_size=1, bias=False),
            nn.BatchNorm2d(output_channels),
        )
        self.skip = (
            nn.Sequential(
                nn.Conv2d(
                    input_channels,
                    output_channels,
                    kernel_size=1,
                    stride=stride,
                    bias=False,
                ),
                nn.BatchNorm2d(output_channels),
            )
            if stride != 1 or input_channels != output_channels
            else nn.Identity()
        )

    def forward(self, inputs: torch.Tensor) -> torch.Tensor:
        return F.relu(self.main(inputs) + self.skip(inputs), inplace=True)


class ResNet50Classifier(nn.Module):
    """Self-contained ResNet-50 so the experiment does not require torchvision."""

    def __init__(self, num_classes: int) -> None:
        super().__init__()
        self.stem = nn.Sequential(
            nn.Conv2d(3, 64, kernel_size=7, stride=2, padding=3, bias=False),
            nn.BatchNorm2d(64),
            nn.ReLU(inplace=True),
            nn.MaxPool2d(kernel_size=3, stride=2, padding=1),
        )
        self.input_channels = 64
        self.layer1 = self._make_layer(64, blocks=3, stride=1)
        self.layer2 = self._make_layer(128, blocks=4, stride=2)
        self.layer3 = self._make_layer(256, blocks=6, stride=2)
        self.layer4 = self._make_layer(512, blocks=3, stride=2)
        self.classifier = nn.Linear(512 * Bottleneck2D.expansion, num_classes)
        self._initialize_weights()

    def _make_layer(self, channels: int, *, blocks: int, stride: int) -> nn.Sequential:
        layers: list[nn.Module] = [
            Bottleneck2D(self.input_channels, channels, stride=stride)
        ]
        self.input_channels = channels * Bottleneck2D.expansion
        layers.extend(
            Bottleneck2D(self.input_channels, channels) for _ in range(blocks - 1)
        )
        return nn.Sequential(*layers)

    def _initialize_weights(self) -> None:
        for module in self.modules():
            if isinstance(module, nn.Conv2d):
                nn.init.kaiming_normal_(module.weight, mode="fan_out", nonlinearity="relu")
            elif isinstance(module, (nn.BatchNorm2d, nn.GroupNorm)):
                nn.init.ones_(module.weight)
                nn.init.zeros_(module.bias)

    def forward(self, inputs: torch.Tensor) -> torch.Tensor:
        features = self.stem(inputs)
        features = self.layer1(features)
        features = self.layer2(features)
        features = self.layer3(features)
        features = self.layer4(features)
        features = F.adaptive_avg_pool2d(features, output_size=1).flatten(1)
        return self.classifier(features)


def normalization3d(channels: int) -> nn.GroupNorm:
    groups = min(8, channels)
    while channels % groups != 0:
        groups -= 1
    return nn.GroupNorm(groups, channels)


class DoubleConv3D(nn.Module):
    def __init__(self, input_channels: int, output_channels: int) -> None:
        super().__init__()
        self.layers = nn.Sequential(
            nn.Conv3d(input_channels, output_channels, kernel_size=3, padding=1, bias=False),
            normalization3d(output_channels),
            nn.ReLU(inplace=True),
            nn.Conv3d(output_channels, output_channels, kernel_size=3, padding=1, bias=False),
            normalization3d(output_channels),
            nn.ReLU(inplace=True),
        )

    def forward(self, inputs: torch.Tensor) -> torch.Tensor:
        return self.layers(inputs)


class UpBlock3D(nn.Module):
    def __init__(self, input_channels: int, skip_channels: int, output_channels: int) -> None:
        super().__init__()
        self.upsample = nn.ConvTranspose3d(
            input_channels,
            output_channels,
            kernel_size=2,
            stride=2,
        )
        self.convolutions = DoubleConv3D(output_channels + skip_channels, output_channels)

    def forward(self, inputs: torch.Tensor, skip: torch.Tensor) -> torch.Tensor:
        inputs = self.upsample(inputs)
        if inputs.shape[2:] != skip.shape[2:]:
            inputs = F.interpolate(
                inputs,
                size=skip.shape[2:],
                mode="trilinear",
                align_corners=False,
            )
        return self.convolutions(torch.cat((skip, inputs), dim=1))


class UNet3DSegmenter(nn.Module):
    """Four-level 3D U-Net suitable for the real Task06_Lung experiment."""

    def __init__(self, base_channels: int = 16) -> None:
        super().__init__()
        if base_channels < 8:
            raise ValueError("3D U-Net base channels must be at least 8")
        self.encoder1 = DoubleConv3D(1, base_channels)
        self.encoder2 = DoubleConv3D(base_channels, base_channels * 2)
        self.encoder3 = DoubleConv3D(base_channels * 2, base_channels * 4)
        self.encoder4 = DoubleConv3D(base_channels * 4, base_channels * 8)
        self.bridge = DoubleConv3D(base_channels * 8, base_channels * 16)
        self.pool = nn.MaxPool3d(kernel_size=2, stride=2)
        self.up4 = UpBlock3D(base_channels * 16, base_channels * 8, base_channels * 8)
        self.up3 = UpBlock3D(base_channels * 8, base_channels * 4, base_channels * 4)
        self.up2 = UpBlock3D(base_channels * 4, base_channels * 2, base_channels * 2)
        self.up1 = UpBlock3D(base_channels * 2, base_channels, base_channels)
        self.head = nn.Conv3d(base_channels, 2, kernel_size=1)

    def forward(self, inputs: torch.Tensor) -> torch.Tensor:
        level1 = self.encoder1(inputs)
        level2 = self.encoder2(self.pool(level1))
        level3 = self.encoder3(self.pool(level2))
        level4 = self.encoder4(self.pool(level3))
        bridge = self.bridge(self.pool(level4))
        decoded = self.up4(bridge, level4)
        decoded = self.up3(decoded, level3)
        decoded = self.up2(decoded, level2)
        decoded = self.up1(decoded, level1)
        return self.head(decoded)


def choose_device(name: str) -> torch.device:
    if name == "auto":
        return torch.device("cuda" if torch.cuda.is_available() else "cpu")
    device = torch.device(name)
    if device.type == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("CUDA was requested but is not available")
    return device


def make_dataset_and_model(args: argparse.Namespace) -> tuple[Dataset, nn.Module, nn.Module]:
    if args.workload == "coco":
        dataset = CocoResizeDataset(
            args.data_root,
            tuple(args.resize),
            args.num_classes,
            args.limit,
        )
        model = (
            ResNet50Classifier(args.num_classes)
            if args.model == "resnet50"
            else TinyClassifier2D(args.num_classes)
        )
        criterion = nn.CrossEntropyLoss()
    else:
        dataset = LungVolumeDataset(args.data_root, tuple(args.volume_size), args.limit)
        model = (
            UNet3DSegmenter(args.unet_base_channels)
            if args.model == "unet3d"
            else TinySegmenter3D()
        )
        criterion = nn.CrossEntropyLoss()
    return dataset, model, criterion


def make_optimizer(args: argparse.Namespace, model: nn.Module) -> torch.optim.Optimizer:
    parameters = model.parameters()
    if args.optimizer == "sgd":
        return torch.optim.SGD(
            parameters,
            lr=args.learning_rate,
            momentum=args.momentum,
            weight_decay=args.weight_decay,
        )
    if args.optimizer == "adam":
        return torch.optim.Adam(parameters, lr=args.learning_rate, weight_decay=args.weight_decay)
    return torch.optim.AdamW(parameters, lr=args.learning_rate, weight_decay=args.weight_decay)


def read_cgroup_memory() -> dict[str, object]:
    """Read the cgroup containing this process, including SRCG when present."""
    try:
        membership = Path("/proc/self/cgroup").read_text(encoding="utf-8")
        relative = next(
            line.split(":", 2)[2]
            for line in membership.splitlines()
            if line.startswith("0::")
        )
        root = Path("/sys/fs/cgroup") / relative.lstrip("/")

        def read_counter(name: str) -> int | None:
            try:
                value = (root / name).read_text(encoding="utf-8").strip()
                return None if value == "max" else int(value)
            except (OSError, ValueError):
                return None

        return {
            "cgroup_path": str(root),
            "cgroup_memory_current_bytes": read_counter("memory.current"),
            "cgroup_memory_peak_bytes": read_counter("memory.peak"),
            "cgroup_memory_max_bytes": read_counter("memory.max"),
        }
    except (OSError, StopIteration, ValueError):
        return {
            "cgroup_path": None,
            "cgroup_memory_current_bytes": None,
            "cgroup_memory_peak_bytes": None,
            "cgroup_memory_max_bytes": None,
        }


def percentile(values: Sequence[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1)]


def model_sha256(model: nn.Module) -> str:
    digest = hashlib.sha256()
    for name, tensor in sorted(model.state_dict().items()):
        digest.update(name.encode("utf-8"))
        digest.update(tensor.detach().cpu().contiguous().numpy().tobytes())
    return digest.hexdigest()


def checkpoint_signature(
    args: argparse.Namespace,
    dataset: Dataset,
) -> dict[str, object]:
    return {
        "workload": args.workload,
        "dataset_length": len(dataset),
        "batch_size": args.batch_size,
        "shuffle": args.shuffle,
        "seed": args.seed,
    }


def make_checkpoint_payload(
    args: argparse.Namespace,
    dataset: Dataset,
    model: nn.Module,
    optimizer: torch.optim.Optimizer,
    sampler: PlanningBatchSampler,
) -> dict[str, object]:
    return {
        "signature": checkpoint_signature(args, dataset),
        "model": model.state_dict(),
        "optimizer": optimizer.state_dict(),
        "sampler": sampler.state_dict(),
        "rng": capture_rng_state(),
    }


def restore_checkpoint(
    source: str,
    store: AtomicCheckpointStore,
    args: argparse.Namespace,
    dataset: Dataset,
    model: nn.Module,
    optimizer: torch.optim.Optimizer,
    sampler: PlanningBatchSampler,
    device: torch.device,
) -> tuple[int, float]:
    payload, record = store.load(source, map_location=device)
    if payload.get("srcg_checkpoint_version") != CheckpointSession.format_version:
        raise RuntimeError("unsupported SRCG checkpoint version")
    if payload.get("signature") != checkpoint_signature(args, dataset):
        raise RuntimeError(
            "checkpoint workload configuration differs from this run; "
            "use --resume none or restore the original batch/seed/dataset settings"
        )
    model.load_state_dict(payload["model"])
    optimizer.load_state_dict(payload["optimizer"])
    sampler.load_state_dict(payload["sampler"])
    restore_rng_state(payload["rng"])
    emit(
        "checkpoint_restored",
        path=str(record.path),
        step=record.step,
        bytes=record.size_bytes,
        load_ms=round(record.elapsed_ms, 2),
        epoch=sampler.epoch + 1,
        next_batch=sampler.next_batch_index + 1,
    )
    return int(payload["global_step"]), record.elapsed_ms


def fault_once(directory: Path, step: int) -> bool:
    marker = directory / f".fault-after-step-{step}-triggered"
    try:
        descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    else:
        os.close(descriptor)
        return True


def run(args: argparse.Namespace) -> int:
    if args.reservation_phase_mode == "auto":
        args.reservation_phase_mode = (
            "shrink-only" if args.num_workers > 0 else "legacy"
        )
    emit_run_config(args)
    policy_enabled = args.policy_debug or args.plan_only
    reservation_client = None
    if not args.plan_only and args.srcg_reservation != "off":
        reservation_client = ReservationClient.from_environment(
            timeout_ms=args.reserve_timeout_ms,
            retry_ms=args.reserve_retry_ms,
            release_safety_bytes=math.ceil(args.release_safety_mib * MIB),
        )
        if args.srcg_reservation == "on" and reservation_client is None:
            raise RuntimeError(
                "SRCG reservation was required but srcg run did not provide manager IPC"
            )
    reservation_enabled = reservation_client is not None
    release_safety_bytes = math.ceil(args.release_safety_mib * MIB)
    # Forked DataLoader workers can dirty inherited Python/PyTorch pages after
    # RESERVE samples memory.current. Keep a small automatic allowance per
    # worker; this is process overhead, not a workload option users should
    # have to tune alongside batch size and learning rate.
    admission_safety_bytes = release_safety_bytes + args.num_workers * 32 * MIB
    if args.memory_trace and not reservation_enabled:
        raise RuntimeError(
            "--memory-trace requires srcg run with an active manager socket"
        )
    if policy_enabled and args.num_workers != 0:
        raise ValueError(
            "--policy-debug/--plan-only require --num-workers 0; "
            "normal SRCG reservation supports DataLoader workers"
        )
    random.seed(args.seed)
    torch.manual_seed(args.seed)
    try:
        import numpy as np

        np.random.seed(args.seed)
    except ImportError:
        pass

    dataset, model, criterion = make_dataset_and_model(args)
    plans: Sequence[SamplePlan] = dataset.plans  # type: ignore[attr-defined]
    observer = PlanObserver(None, emit_plans=True) if policy_enabled else None
    exact_progress_enabled = args.checkpoint_dir is not None
    sampler = (
        PlanningBatchSampler(
            plans,
            args.batch_size,
            args.shuffle,
            args.seed,
            args.policy_safety_factor,
            observer.before_load if observer is not None else None,
        )
        if policy_enabled or exact_progress_enabled
        else None
    )
    batch_count = math.ceil(len(dataset) / args.batch_size)
    dataset_fields: dict[str, object] = {
        "workload": args.workload,
        "samples": len(dataset),
        "batches": batch_count,
        "mode": (
            "reserved-memory"
            if reservation_enabled
            else "policy-debug"
            if policy_enabled
            else "train"
        ),
        "shuffle": args.shuffle,
        "num_workers": args.num_workers,
        "srcg_reservation": reservation_enabled,
    }
    if policy_enabled:
        dataset_fields.update(
            minimum_plan_mib=round(min(plan.load_peak_bytes for plan in plans) / MIB, 2),
            maximum_plan_mib=round(max(plan.load_peak_bytes for plan in plans) / MIB, 2),
        )
    emit("dataset_ready", **dataset_fields)

    if args.plan_only:
        if sampler is None:
            raise RuntimeError("plan-only mode requires the planning sampler")
        for _ in sampler:
            assert observer is not None
            observer.consume()
            sampler.mark_batch_consumed()
        return 0

    device = choose_device(args.device)
    model.to(device)
    optimizer = make_optimizer(args, model)
    store = (
        AtomicCheckpointStore(args.checkpoint_dir, keep=args.checkpoint_keep)
        if args.checkpoint_dir is not None
        else None
    )
    session = CheckpointSession(
        store,
        args.checkpoint_every_steps,
        checkpoint_client=reservation_client if store is not None else None,
    )
    global_step = 0
    checkpoint_load_ms = 0.0
    resumed = False
    if args.resume != "none":
        if store is None:
            raise ValueError("--resume requires --checkpoint-dir")
        if args.resume == "auto" and not store.has_complete_checkpoint():
            emit("checkpoint_resume_skipped", reason="no_complete_checkpoint")
        else:
            if sampler is None:
                raise RuntimeError("checkpoint restore requires the planning sampler")
            global_step, checkpoint_load_ms = restore_checkpoint(
                args.resume, store, args, dataset, model, optimizer, sampler, device
            )
            resumed = True
    loader_dataset: Dataset = dataset
    reservation_stats = ReservationStats()
    if reservation_enabled:
        loader_dataset = ReservedBatchDataset(
            dataset,
            plans,
            safety_factor=args.policy_safety_factor,
            timeout_ms=args.reserve_timeout_ms,
            retry_ms=args.reserve_retry_ms,
            release_safety_bytes=release_safety_bytes,
            admission_safety_bytes=admission_safety_bytes,
            phase_mode=args.reservation_phase_mode,
            memory_trace=args.memory_trace,
        )
    loader_options: dict[str, object] = {
        "num_workers": args.num_workers,
        "pin_memory": args.pin_memory,
    }
    if sampler is not None:
        loader_options["batch_sampler"] = sampler
    else:
        loader_options.update(
            batch_size=args.batch_size,
            shuffle=args.shuffle,
            generator=torch.Generator().manual_seed(args.seed),
        )
    if reservation_enabled:
        loader_options["collate_fn"] = unwrap_reserved_batch
    if args.num_workers > 0:
        loader_options["prefetch_factor"] = args.prefetch_factor
        loader_options["persistent_workers"] = args.persistent_workers
    loader = DataLoader(
        loader_dataset,
        **loader_options,
    )
    emit(
        "training_start",
        device=str(device),
        model=args.model,
        model_parameters=sum(parameter.numel() for parameter in model.parameters()),
        epochs=args.epochs,
        optimizer=args.optimizer,
        learning_rate=args.learning_rate,
        weight_decay=args.weight_decay,
        batch_size=args.batch_size,
        resumed=resumed,
        initial_step=global_step,
        checkpoint_every_steps=args.checkpoint_every_steps,
        srcg_reservation=reservation_enabled,
        reservation_admission_safety_bytes=(
            admission_safety_bytes if reservation_enabled else 0
        ),
    )

    run_started = time.perf_counter()
    load_times: list[float] = []
    train_times: list[float] = []
    processed_samples = 0
    peak_rss_bytes = read_rss_bytes()
    checkpoint_count = 0
    checkpoint_write_ms = 0.0
    checkpoint_bytes = 0
    if device.type == "cuda":
        torch.cuda.reset_peak_memory_stats(device)

    def finish(reason: str) -> int:
        elapsed = time.perf_counter() - run_started
        cgroup = read_cgroup_memory()
        cuda_peak_allocated = 0
        cuda_peak_reserved = 0
        if device.type == "cuda":
            torch.cuda.synchronize(device)
            cuda_peak_allocated = torch.cuda.max_memory_allocated(device)
            cuda_peak_reserved = torch.cuda.max_memory_reserved(device)
        emit("training_end", steps=global_step, reason=reason)
        emit(
            "performance_summary",
            workload=args.workload,
            device=str(device),
            elapsed_seconds=round(elapsed, 4),
            processed_samples=processed_samples,
            processed_steps=len(train_times),
            samples_per_second=round(processed_samples / elapsed, 3) if elapsed else 0.0,
            steps_per_second=round(len(train_times) / elapsed, 3) if elapsed else 0.0,
            mean_load_ms=round(sum(load_times) / len(load_times), 3) if load_times else 0.0,
            p95_load_ms=round(percentile(load_times, 0.95), 3),
            mean_train_ms=round(sum(train_times) / len(train_times), 3) if train_times else 0.0,
            p95_train_ms=round(percentile(train_times, 0.95), 3),
            peak_rss_mib=round(peak_rss_bytes / MIB, 2),
            process_max_rss_mib=round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0, 2),
            cuda_peak_allocated_mib=round(cuda_peak_allocated / MIB, 2),
            cuda_peak_reserved_mib=round(cuda_peak_reserved / MIB, 2),
            checkpoint_count=checkpoint_count,
            checkpoint_write_ms=round(checkpoint_write_ms, 3),
            checkpoint_load_ms=round(checkpoint_load_ms, 3),
            checkpoint_bytes=checkpoint_bytes,
            reserve_granted=reservation_stats.grants,
            reserve_wait_events=reservation_stats.wait_events,
            reserve_wait_ms=round(reservation_stats.wait_ms, 3),
            reserve_admission_ms=round(reservation_stats.admission_ms, 3),
            reserve_released=reservation_stats.released,
            reserve_pending_releases=reservation_stats.pending_releases,
            reserve_retried_releases=reservation_stats.retried_releases,
            reserve_shrink_attempts=reservation_stats.shrink_attempts,
            reserve_shrink_successes=reservation_stats.shrink_successes,
            reserve_shrink_unchanged=reservation_stats.shrink_unchanged,
            reserve_shrink_returned_bytes=reservation_stats.shrink_returned_bytes,
            reserve_shrink_ms=round(reservation_stats.shrink_ms, 3),
            reserve_early_release_attempts=reservation_stats.early_release_attempts,
            reserve_early_release_ms=round(
                reservation_stats.early_release_ms, 3
            ),
            resumed=resumed,
            final_model_sha256=model_sha256(model),
            **cgroup,
        )
        return 0

    def checkpoint_payload() -> dict[str, object]:
        if sampler is None:
            raise RuntimeError("checkpointing requires the planning sampler")
        return make_checkpoint_payload(args, dataset, model, optimizer, sampler)

    model.train()
    standard_epoch = 1
    standard_batch_index = 0
    iterator = iter(loader)
    with session:
        while True:
            if sampler is not None:
                if sampler.epoch >= args.epochs:
                    break
                training_epoch = sampler.epoch + 1
                batch_id = sampler.next_batch_index + 1
            else:
                if standard_epoch > args.epochs:
                    break
                training_epoch = standard_epoch
                batch_id = standard_batch_index + 1

            load_started_at = time.perf_counter()
            try:
                batch = next(iterator)
            except StopIteration:
                if sampler is None:
                    standard_epoch += 1
                    standard_batch_index = 0
                if (
                    (sampler is not None and sampler.epoch >= args.epochs)
                    or (sampler is None and standard_epoch > args.epochs)
                ):
                    break
                iterator = iter(loader)
                continue
            else:
                load_ms = (time.perf_counter() - load_started_at) * 1000.0
                reserved_batch: ReservedBatch | None = None
                if reservation_enabled:
                    if not isinstance(batch, ReservedBatch):
                        raise RuntimeError("SRCG DataLoader did not return a reserved batch")
                    reserved_batch = batch
                    if args.reservation_phase_mode in {
                        "early-release",
                        "phase-aware",
                    }:
                        inputs, targets = batch.take_tensors()
                    else:
                        if batch.inputs is None or batch.targets is None:
                            raise RuntimeError("reserved batch tensors were already consumed")
                        inputs, targets = batch.inputs, batch.targets
                    load_ms = batch.load_ms
                    reservation_stats.observe(batch)
                    if args.memory_trace and batch.memory_trace is not None:
                        batch.memory_trace["batch_received"] = read_cgroup_memory_point()
                else:
                    inputs, targets = batch
                if observer is not None:
                    plan, load_ms = observer.consume()
                    batch_id = plan.batch_id
                outputs = None
                loss = None
                release_result = None
                reservation_finalized = False
                try:
                    started = time.perf_counter()
                    non_blocking = args.pin_memory and device.type == "cuda"
                    inputs = inputs.to(device, non_blocking=non_blocking)
                    targets = targets.to(device, non_blocking=non_blocking)
                    if reserved_batch is not None and reserved_batch.memory_trace is not None:
                        reserved_batch.memory_trace["after_h2d_call"] = (
                            read_cgroup_memory_point()
                        )
                        reserved_batch.memory_trace["h2d_call_is_synchronous"] = (
                            not non_blocking
                        )

                    # ReservedBatch.take_tensors() removed the transport
                    # object's references. Reassignment above drops the main
                    # loop's CPU references as soon as each transfer has been
                    # enqueued. The daemon still refuses to lower memory.max
                    # if a non-blocking pinned copy keeps those pages charged.
                    if (
                        reserved_batch is not None
                        and args.reservation_phase_mode
                        in {"early-release", "phase-aware"}
                    ):
                        if reservation_client is None:
                            raise RuntimeError("missing main-process reservation client")
                        if reserved_batch.memory_trace is not None:
                            before_release = read_cgroup_memory_point()
                            reserved_batch.memory_trace["before_early_release"] = (
                                before_release
                            )
                            reserved_batch.memory_trace["before_release"] = (
                                before_release
                            )
                        release_started = time.perf_counter()
                        release_result = reservation_client.release(
                            reserved_batch.token
                        )
                        reservation_stats.early_release_attempts += 1
                        reservation_stats.early_release_ms += (
                            time.perf_counter() - release_started
                        ) * 1000.0
                        reservation_finalized = True
                        if release_result.decision in {
                            "RELEASED",
                            "ALREADY_RELEASED",
                        }:
                            reservation_stats.released += 1
                        elif release_result.decision == "PENDING":
                            reservation_stats.pending_releases += 1
                        elif release_result.decision == "DENIED":
                            raise RuntimeError(
                                f"SRCG release denied: {release_result.reason}"
                            )
                        else:
                            raise RuntimeError(
                                "unexpected SRCG release response: "
                                f"{release_result.decision}"
                            )
                        if reserved_batch.memory_trace is not None:
                            after_release = read_cgroup_memory_point()
                            reserved_batch.memory_trace["after_early_release"] = (
                                after_release
                            )
                            reserved_batch.memory_trace["after_release"] = (
                                after_release
                            )

                    optimizer.zero_grad(set_to_none=True)
                    outputs = model(inputs)
                    loss = criterion(outputs, targets)
                    loss.backward()
                    optimizer.step()
                    if device.type == "cuda":
                        torch.cuda.synchronize(device)
                    if isinstance(loader_dataset, ReservedBatchDataset):
                        loader_dataset.mark_consumer_warm()
                    if reserved_batch is not None and reserved_batch.memory_trace is not None:
                        reserved_batch.memory_trace["after_optimizer"] = (
                            read_cgroup_memory_point()
                        )
                    train_ms = (time.perf_counter() - started) * 1000.0

                    global_step += 1
                    processed_samples += int(inputs.shape[0])
                    load_times.append(load_ms)
                    train_times.append(train_ms)
                    if sampler is not None:
                        sampler.mark_batch_consumed()
                    else:
                        standard_batch_index += 1
                    current_rss = read_rss_bytes()
                    peak_rss_bytes = max(peak_rss_bytes, current_rss)
                    emit(
                        "step_end",
                        epoch=training_epoch,
                        step=global_step,
                        batch_id=batch_id,
                        loss=round(float(loss.detach().cpu()), 6),
                        load_ms=round(load_ms, 2),
                        train_ms=round(train_ms, 2),
                        rss_mib=round(current_rss / MIB, 2),
                    )
                finally:
                    del inputs, targets, outputs, loss
                    if reserved_batch is not None and not reservation_finalized:
                        if reservation_client is None:
                            raise RuntimeError("missing main-process reservation client")
                        if reserved_batch.memory_trace is not None:
                            before_release = read_cgroup_memory_point()
                            reserved_batch.memory_trace["before_fallback_release"] = (
                                before_release
                            )
                            reserved_batch.memory_trace["before_release"] = (
                                before_release
                            )
                        release_result = reservation_client.release(
                            reserved_batch.token
                        )
                        reservation_finalized = True
                        if release_result.decision in {"RELEASED", "ALREADY_RELEASED"}:
                            reservation_stats.released += 1
                        elif release_result.decision == "PENDING":
                            reservation_stats.pending_releases += 1
                        elif release_result.decision == "DENIED":
                            raise RuntimeError(
                                f"SRCG release denied: {release_result.reason}"
                            )
                        if reserved_batch.memory_trace is not None:
                            after_release = read_cgroup_memory_point()
                            reserved_batch.memory_trace["after_fallback_release"] = (
                                after_release
                            )
                            reserved_batch.memory_trace["after_release"] = (
                                after_release
                            )
                    if (
                        reserved_batch is not None
                        and reserved_batch.memory_trace is not None
                    ):
                        emit(
                            "batch_memory_trace",
                            epoch=training_epoch,
                            step=global_step,
                            batch_id=batch_id,
                            sample_ids=reserved_batch.sample_ids,
                            source_shapes=reserved_batch.source_shapes,
                            estimated_batch_allocation_bytes=(
                                reserved_batch.token.requested_bytes
                            ),
                            estimated_resident_bytes=(
                                reserved_batch.estimated_resident_bytes
                            ),
                            estimated_resident_with_safety_bytes=(
                                reserved_batch.estimated_resident_with_safety_bytes
                            ),
                            token_effective_limit_bytes=(
                                reserved_batch.token.effective_limit_bytes
                            ),
                            token_initial_borrowed_bytes=(
                                reserved_batch.token.borrowed_bytes
                            ),
                            token_borrowed_bytes=(
                                reserved_batch.token.borrowed_bytes
                            ),
                            token_borrowed_after_shrink=(
                                reserved_batch.shrink_borrowed_bytes
                            ),
                            shrink_returned_bytes=(
                                reserved_batch.shrink_returned_bytes
                            ),
                            shrink_decision=(
                                reserved_batch.shrink_decision
                                if reserved_batch.shrink_decision is not None
                                else "not_attempted"
                            ),
                            release_decision=(
                                release_result.decision
                                if release_result is not None
                                else "not_attempted"
                            ),
                            phase_points=reserved_batch.memory_trace,
                        )

                if args.stop_after_step and global_step == args.stop_after_step:
                    session.request_stop_for_test()
                force_checkpoint = bool(
                    args.fail_after_checkpoint_step
                    and global_step == args.fail_after_checkpoint_step
                )
                record = session.save_at_safe_point(
                    global_step,
                    checkpoint_payload,
                    force=force_checkpoint,
                )
                if record is not None:
                    checkpoint_count += 1
                    checkpoint_write_ms += record.elapsed_ms
                    checkpoint_bytes += record.size_bytes
                    emit(
                        "checkpoint_committed",
                        path=str(record.path),
                        step=record.step,
                        bytes=record.size_bytes,
                        sha256=record.sha256,
                        save_ms=round(record.elapsed_ms, 2),
                        reason=(
                            "signal"
                            if session.stop_signal is not None
                            else "fault-test"
                            if force_checkpoint
                            else "periodic"
                        ),
                    )
                if session.stop_signal is not None:
                    emit("cooperative_stop", signal=session.stop_signal, step=global_step)
                    return finish("cooperative_stop")
                if (
                    force_checkpoint
                    and args.checkpoint_dir is not None
                    and fault_once(args.checkpoint_dir, global_step)
                ):
                    emit("fault_injected", step=global_step, exit_code=75)
                    return 75
                if args.max_steps and global_step >= args.max_steps:
                    return finish("max_steps")

    return finish("epochs_complete")


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def add_common_arguments(
    parser: argparse.ArgumentParser,
    *,
    batch_size: int,
    optimizer: str,
    learning_rate: float,
) -> None:
    training = parser.add_argument_group("normal training")
    training.add_argument("--batch-size", type=positive_int, default=batch_size, help="samples per batch")
    training.add_argument("--epochs", type=positive_int, default=1, help="number of training epochs")
    training.add_argument(
        "--optimizer",
        choices=("sgd", "adam", "adamw"),
        default=optimizer,
        help="optimizer algorithm",
    )
    training.add_argument(
        "--learning-rate",
        "--lr",
        type=float,
        default=learning_rate,
        help="optimizer learning rate",
    )
    training.add_argument("--weight-decay", type=float, default=1e-4, help="optimizer weight decay")
    training.add_argument("--momentum", type=float, default=0.9, help="used by SGD only")
    training.add_argument("--device", default="auto", help="auto, cpu, cuda, or cuda:N")
    training.add_argument("--seed", type=int, default=42, help="shuffle and PyTorch seed")

    loading = parser.add_argument_group("DataLoader")
    loading.add_argument("--num-workers", type=int, default=0, help="DataLoader worker processes")
    loading.add_argument(
        "--pin-memory",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="pin CPU batch memory",
    )
    loading.add_argument(
        "--shuffle",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="shuffle samples with the configured seed",
    )
    loading.add_argument("--prefetch-factor", type=positive_int, default=2, help="batches prefetched per worker")
    loading.add_argument(
        "--persistent-workers",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="keep workers alive between epochs",
    )

    checkpointing = parser.add_argument_group("SRCG cooperative checkpointing")
    checkpointing.add_argument(
        "--checkpoint-dir",
        type=Path,
        default=os.environ.get("SRCG_CHECKPOINT_DIR"),
        help="directory for atomic checkpoints; SRCG may provide it through the environment",
    )
    checkpointing.add_argument(
        "--checkpoint-every-steps",
        type=int,
        default=0,
        help="periodic checkpoint interval; 0 disables periodic saves",
    )
    checkpointing.add_argument(
        "--checkpoint-keep",
        type=positive_int,
        default=2,
        help="number of complete checkpoint files retained",
    )
    checkpointing.add_argument(
        "--resume",
        default=os.environ.get("SRCG_RESUME", "none"),
        help="none, auto (latest.json), or a checkpoint path",
    )

    reservation = parser.add_argument_group("SRCG Reserved Memory")
    reservation.add_argument(
        "--srcg-reservation",
        choices=("auto", "on", "off"),
        default="auto",
        help="auto uses IPC supplied by srcg run; on requires it; off bypasses it",
    )
    reservation.add_argument(
        "--reserve-timeout-ms",
        type=int,
        default=0,
        help="maximum WAIT time before aborting; 0 waits until capacity is available",
    )
    reservation.add_argument(
        "--reserve-retry-ms",
        type=positive_int,
        default=50,
        help="delay between WAIT retries",
    )
    reservation.add_argument(
        "--release-safety-mib",
        type=float,
        default=64.0,
        help="RAM margin used for admission and safe cgroup limit lowering",
    )
    reservation.add_argument(
        "--reservation-phase-mode",
        choices=("auto", "legacy", "shrink-only", "early-release", "phase-aware"),
        default=os.environ.get("SRCG_RESERVATION_PHASE_MODE", "auto"),
        help=argparse.SUPPRESS,
    )

    debugging = parser.add_argument_group("debugging and SRCG policy instrumentation")
    debugging.add_argument("--limit", type=positive_int, help="use only the first N sorted samples")
    debugging.add_argument("--max-steps", type=int, default=0, help="0 runs the complete epoch count")
    debugging.add_argument(
        "--policy-debug",
        action="store_true",
        help="emit the next batch RAM plan before loading it (requires num_workers=0)",
    )
    debugging.add_argument(
        "--plan-only",
        action="store_true",
        help="emit policy plans without loading samples or training",
    )
    debugging.add_argument(
        "--policy-safety-factor",
        type=float,
        default=1.25,
        help="multiplier applied to header-based RAM estimates",
    )
    debugging.add_argument(
        "--memory-trace",
        action="store_true",
        help=(
            "record cgroup RAM at reservation/load/H2D/optimizer boundaries; "
            "diagnostic only and requires active SRCG reservation"
        ),
    )
    debugging.add_argument(
        "--stop-after-step",
        type=int,
        default=0,
        help="test cooperative signal handling at the safe point after step N",
    )
    debugging.add_argument(
        "--fail-after-checkpoint-step",
        type=int,
        default=0,
        help="test recovery by exiting 75 once, after checkpointing step N",
    )


def build_parser() -> argparse.ArgumentParser:
    formatter = argparse.ArgumentDefaultsHelpFormatter
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=formatter)
    subparsers = parser.add_subparsers(dest="workload", required=True)

    coco = subparsers.add_parser(
        "coco",
        help="COCO val2017 JPEG decode and resize",
        formatter_class=formatter,
    )
    coco.add_argument(
        "--data-root",
        type=Path,
        default=REPO_ROOT / "ML-Test-Dataset" / "val2017",
        help="directory containing val2017 JPEG files",
    )
    coco.add_argument(
        "--resize",
        type=positive_int,
        nargs=2,
        metavar=("HEIGHT", "WIDTH"),
        default=(224, 224),
        help="output image size",
    )
    coco.add_argument("--num-classes", type=positive_int, default=1000, help="pseudo-label output classes")
    coco.add_argument(
        "--model",
        choices=("resnet50", "tiny"),
        default="resnet50",
        help="network architecture; tiny is reserved for correctness tests",
    )
    add_common_arguments(coco, batch_size=8, optimizer="sgd", learning_rate=0.01)

    lung = subparsers.add_parser(
        "lung",
        help="Task06_Lung full-volume 3D training",
        formatter_class=formatter,
    )
    lung.add_argument(
        "--data-root",
        type=Path,
        default=REPO_ROOT / "ML-Test-Dataset" / "Task06_Lung",
        help="Task06_Lung directory",
    )
    lung.add_argument(
        "--volume-size",
        type=positive_int,
        nargs=3,
        metavar=("DEPTH", "HEIGHT", "WIDTH"),
        default=(32, 128, 128),
        help="3D tensor size after full-volume loading",
    )
    lung.add_argument(
        "--model",
        choices=("unet3d", "tiny"),
        default="unet3d",
        help="network architecture; tiny is reserved for correctness tests",
    )
    lung.add_argument(
        "--unet-base-channels",
        type=positive_int,
        default=32,
        help="base feature width of the 3D U-Net",
    )
    add_common_arguments(lung, batch_size=1, optimizer="adamw", learning_rate=1e-4)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.policy_safety_factor < 1.0:
        print("error: --policy-safety-factor must be at least 1.0", file=sys.stderr)
        return 2
    if args.learning_rate <= 0 or args.weight_decay < 0:
        print("error: learning rate must be positive and weight decay cannot be negative", file=sys.stderr)
        return 2
    if any(
        value < 0
        for value in (
            args.num_workers,
            args.max_steps,
            args.checkpoint_every_steps,
            args.stop_after_step,
            args.fail_after_checkpoint_step,
            args.reserve_timeout_ms,
        )
    ):
        print("error: worker/step/checkpoint counts cannot be negative", file=sys.stderr)
        return 2
    if args.persistent_workers and args.num_workers == 0:
        print("error: --persistent-workers requires --num-workers greater than zero", file=sys.stderr)
        return 2
    if args.release_safety_mib < 0:
        print("error: --release-safety-mib cannot be negative", file=sys.stderr)
        return 2
    if args.memory_trace and args.srcg_reservation == "off":
        print("error: --memory-trace requires active SRCG reservation", file=sys.stderr)
        return 2
    if (
        args.checkpoint_every_steps
        or args.stop_after_step
        or args.fail_after_checkpoint_step
        or args.resume != "none"
    ) and args.checkpoint_dir is None:
        print("error: checkpoint/resume options require --checkpoint-dir", file=sys.stderr)
        return 2
    try:
        return run(args)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
