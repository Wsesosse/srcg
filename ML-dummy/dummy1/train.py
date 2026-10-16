#!/usr/bin/env python3
"""
Dummy ML Training Script for NVML Daemon Testing
==================================================
Trains a large CNN on synthetic (random) data.
Designed to consume ~6-8 GB VRAM steadily.

Usage:
    python train.py                    # Default ~6-8 GB VRAM
    python train.py --vram-target 4    # Target ~4 GB VRAM
    python train.py --vram-target 9    # Target ~9 GB VRAM
    python train.py --epochs 50        # Run 50 epochs then stop
"""

import argparse
import math
import random
import signal
import sys
import time

import torch
import torch.nn as nn
import torch.optim as optim
from torch.amp import GradScaler, autocast


# ---------------------------------------------------------------------------
# Model: Deep ResNet-like CNN (large enough to eat VRAM)
# ---------------------------------------------------------------------------
class ResBlock(nn.Module):
    def __init__(self, channels):
        super().__init__()
        self.conv1 = nn.Conv2d(channels, channels, 3, padding=1, bias=False)
        self.bn1 = nn.BatchNorm2d(channels)
        self.conv2 = nn.Conv2d(channels, channels, 3, padding=1, bias=False)
        self.bn2 = nn.BatchNorm2d(channels)
        self.relu = nn.ReLU(inplace=True)

    def forward(self, x):
        identity = x
        out = self.relu(self.bn1(self.conv1(x)))
        out = self.bn2(self.conv2(out))
        out += identity
        return self.relu(out)


class HeavyCNN(nn.Module):
    """
    A deliberately large CNN to consume VRAM.
    Parameters scale with `width` and `depth`.
    """

    def __init__(self, width=256, depth=20, num_classes=1000):
        super().__init__()
        self.stem = nn.Sequential(
            nn.Conv2d(3, width, 7, stride=2, padding=3, bias=False),
            nn.BatchNorm2d(width),
            nn.ReLU(inplace=True),
            nn.MaxPool2d(3, stride=2, padding=1),
        )
        self.blocks = nn.Sequential(*[ResBlock(width) for _ in range(depth)])
        self.pool = nn.AdaptiveAvgPool2d(1)
        self.fc = nn.Linear(width, num_classes)

    def forward(self, x):
        x = self.stem(x)
        x = self.blocks(x)
        x = self.pool(x)
        x = x.flatten(1)
        return self.fc(x)


# ---------------------------------------------------------------------------
# VRAM presets: (width, depth, batch_size, image_size)
# Approximate VRAM usage on a typical GPU
# ---------------------------------------------------------------------------
PRESETS = {
    #  target_gb: (width, depth, batch, img_size)
    2: (128, 8, 16, 128),
    3: (192, 10, 16, 128),
    4: (192, 16, 24, 128),
    5: (256, 12, 16, 160),
    6: (256, 20, 16, 160),
    7: (256, 20, 24, 192),
    8: (320, 20, 24, 192),
    9: (384, 20, 24, 192),
}


def get_config(target_gb: int):
    """Get model/data config for a target VRAM usage."""
    target_gb = max(2, min(9, target_gb))
    return PRESETS[target_gb]


# ---------------------------------------------------------------------------
# Realistic memory spike generators
# ---------------------------------------------------------------------------
def spike_random_temp_tensors(device, width, img_size):
    """Simulate temporary large allocations (feature maps, concatenations, etc.)"""
    num_tensors = random.randint(1, 4)
    temps = []
    for _ in range(num_tensors):
        # Random tensor sizes to create irregular spikes
        h = random.randint(img_size // 2, img_size * 2)
        w = random.randint(img_size // 2, img_size * 2)
        c = random.choice([width // 2, width, width * 2])
        t = torch.randn(random.randint(1, 4), c, h, w, device=device)
        temps.append(t)
    # Hold briefly then release
    time.sleep(random.uniform(0.01, 0.05))
    del temps
    return


def spike_validation_pass(model, device, batch_size, img_size):
    """Simulate a validation pass with larger batch / no grad but extra tensors."""
    model.eval()
    # Validation often uses larger batch because no grad needed
    val_batch = random.randint(batch_size, batch_size * 3)
    with torch.no_grad():
        imgs = torch.randn(val_batch, 3, img_size, img_size, device=device)
        out = model(imgs)
        # Simulate computing metrics — extra allocations
        probs = torch.softmax(out, dim=1)
        top5 = torch.topk(probs, k=5, dim=1)
        # Simulate confusion matrix or per-class stats
        expanded = torch.zeros(val_batch, 1000, device=device)
        expanded.scatter_(1, top5.indices, top5.values)
        del imgs, out, probs, top5, expanded
    model.train()


def spike_gradient_accumulation(model, criterion, scaler, device,
                                 batch_size, img_size, amp_enabled):
    """Simulate gradient accumulation — multiple forward/backward without step."""
    accum_steps = random.randint(2, 5)
    for _ in range(accum_steps):
        bs = random.randint(batch_size // 2, batch_size)
        images = torch.randn(bs, 3, img_size, img_size, device=device)
        labels = torch.randint(0, 1000, (bs,), device=device)
        with autocast(device_type="cuda", enabled=amp_enabled):
            out = model(images)
            loss = criterion(out, labels) / accum_steps
        scaler.scale(loss).backward()
        del images, labels, out, loss
    return


def spike_lr_search_burst(model, criterion, device, batch_size, img_size, amp_enabled):
    """Simulate an LR finder / warmup burst — rapid forward passes with different sizes."""
    for _ in range(random.randint(3, 8)):
        bs = random.randint(1, batch_size * 2)
        sz = random.choice([img_size, img_size + 32, img_size - 32, img_size + 64])
        sz = max(32, sz)
        with torch.no_grad():
            imgs = torch.randn(bs, 3, sz, sz, device=device)
            _ = model(imgs)
            del imgs
    return


# ---------------------------------------------------------------------------
# Training loop
# ---------------------------------------------------------------------------
def train(args):
    if not torch.cuda.is_available():
        print("ERROR: CUDA is not available. This script requires a GPU.")
        sys.exit(1)

    device = torch.device("cuda:0")
    width, depth, batch_size, img_size = get_config(args.vram_target)

    print("=" * 60)
    print("  Dummy ML Training for NVML Daemon Testing")
    print("=" * 60)
    print(f"  GPU          : {torch.cuda.get_device_name(0)}")
    print(f"  Total VRAM   : {torch.cuda.get_device_properties(0).total_memory / 1e9:.1f} GB")
    print(f"  Target VRAM  : ~{args.vram_target} GB")
    print(f"  Model        : HeavyCNN(width={width}, depth={depth})")
    print(f"  Batch size   : {batch_size} (±variation)")
    print(f"  Image size   : {img_size}x{img_size}")
    print(f"  Epochs       : {'infinite' if args.epochs <= 0 else args.epochs}")
    print(f"  AMP          : {args.amp}")
    print(f"  Spiky memory : ON (realistic fluctuations)")
    print("=" * 60)

    # Build model
    model = HeavyCNN(width=width, depth=depth, num_classes=1000).to(device)
    param_count = sum(p.numel() for p in model.parameters())
    print(f"  Parameters   : {param_count:,}")

    optimizer = optim.AdamW(model.parameters(), lr=1e-3, weight_decay=0.01)
    criterion = nn.CrossEntropyLoss()
    scaler = GradScaler(enabled=args.amp)

    # Synthetic dataset (regenerated each epoch)
    num_batches = args.steps_per_epoch

    # Graceful shutdown
    should_stop = [False]

    def signal_handler(sig, frame):
        print("\n>>> Received signal, finishing current step...")
        should_stop[0] = True

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Print initial VRAM usage
    torch.cuda.reset_peak_memory_stats()

    epoch = 0
    global_step = 0
    start_time = time.time()

    print("\n[Training started]\n")

    while True:
        if args.epochs > 0 and epoch >= args.epochs:
            break
        if should_stop[0]:
            break

        epoch += 1
        epoch_loss = 0.0
        epoch_start = time.time()

        model.train()
        for step in range(1, num_batches + 1):
            if should_stop[0]:
                break

            # --- Variable batch size (±50%) to create natural fluctuations ---
            step_batch = random.randint(
                max(1, batch_size // 2),
                int(batch_size * 1.5),
            )

            # Generate random data on-the-fly
            images = torch.randn(step_batch, 3, img_size, img_size, device=device)
            labels = torch.randint(0, 1000, (step_batch,), device=device)

            optimizer.zero_grad(set_to_none=True)

            with autocast(device_type="cuda", enabled=args.amp):
                outputs = model(images)
                loss = criterion(outputs, labels)

            scaler.scale(loss).backward()
            scaler.step(optimizer)
            scaler.update()

            epoch_loss += loss.item()
            global_step += 1

            # --- Random memory spikes during training ---

            # ~15% chance: allocate temporary tensors (simulates data augmentation buffers)
            if random.random() < 0.15:
                spike_random_temp_tensors(device, width, img_size)

            # ~8% chance: gradient accumulation burst
            if random.random() < 0.08:
                spike_gradient_accumulation(
                    model, criterion, scaler, device,
                    batch_size, img_size, args.amp,
                )

            # ~5% chance: clear cache (causes re-allocation spike next step)
            if random.random() < 0.05:
                torch.cuda.empty_cache()

            del images, labels, outputs, loss

        # --- End of epoch: periodic heavy operations ---

        # ~30% chance: run a validation pass (big spike)
        if random.random() < 0.30:
            spike_validation_pass(model, device, batch_size, img_size)

        # ~10% chance: LR search burst
        if random.random() < 0.10:
            spike_lr_search_burst(model, criterion, device, batch_size, img_size, args.amp)

        # ~10% chance: full cache clear (dip then spike)
        if random.random() < 0.10:
            torch.cuda.empty_cache()
            time.sleep(random.uniform(0.1, 0.3))

        # Epoch stats
        epoch_time = time.time() - epoch_start
        avg_loss = epoch_loss / num_batches if num_batches > 0 else 0
        vram_used = torch.cuda.memory_allocated() / 1e9
        vram_peak = torch.cuda.max_memory_allocated() / 1e9
        vram_reserved = torch.cuda.memory_reserved() / 1e9

        print(
            f"  Epoch {epoch:4d} | "
            f"Loss: {avg_loss:.4f} | "
            f"Time: {epoch_time:.1f}s | "
            f"VRAM: {vram_used:.2f}/{vram_reserved:.2f} GB (peak {vram_peak:.2f}) | "
            f"Step: {global_step}"
        )


    # Summary
    total_time = time.time() - start_time
    print("\n" + "=" * 60)
    print(f"  Training finished after {epoch} epochs, {global_step} steps")
    print(f"  Total time   : {total_time:.1f}s")
    print(f"  Peak VRAM    : {torch.cuda.max_memory_allocated() / 1e9:.2f} GB")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="Dummy ML Training for NVML testing")
    parser.add_argument(
        "--vram-target",
        type=int,
        default=7,
        choices=range(2, 10),
        metavar="[2-9]",
        help="Target VRAM usage in GB (default: 7)",
    )
    parser.add_argument(
        "--epochs",
        type=int,
        default=0,
        help="Number of epochs (0 = infinite, default: 0)",
    )
    parser.add_argument(
        "--steps-per-epoch",
        type=int,
        default=50,
        help="Steps per epoch (default: 50)",
    )
    parser.add_argument(
        "--amp",
        action="store_true",
        default=True,
        help="Use automatic mixed precision (default: True)",
    )
    parser.add_argument(
        "--no-amp",
        action="store_false",
        dest="amp",
        help="Disable automatic mixed precision",
    )
    args = parser.parse_args()
    train(args)


if __name__ == "__main__":
    main()
