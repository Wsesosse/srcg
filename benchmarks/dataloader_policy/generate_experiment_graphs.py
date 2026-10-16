#!/usr/bin/env python3
"""Generate dependency-free SVG graphs from every SRCG experiment artifact.

The script deliberately keeps an inventory of failed, historical, calibration,
and invalid runs.  The smaller canonical chart uses only explicitly selected
like-for-like scenarios so development failures are not presented as final
performance evidence.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from html import escape
import json
import math
from pathlib import Path
import shlex
import statistics
import textwrap
from typing import Any, Iterable


GIB = 1024**3
MIB = 1024**2

COLORS = {
    "baseline": "#4C78A8",
    "normal": "#4C78A8",
    "sequential": "#E3A62F",
    "srcg": "#2A9D6F",
    "fixed": "#7A6F9B",
    "unknown": "#7F8C8D",
    "failed": "#D1495B",
    "invalid": "#9AA0A6",
    "grid": "#D9DEE3",
    "ink": "#1F2933",
    "muted": "#66737F",
    "paper": "#FFFFFF",
}


# Every domain-specific label used by the generated graphs/report must be
# defined here before a reader is expected to interpret a result.
GLOSSARY: list[tuple[str, str]] = [
    ("framework", "ชุดโปรแกรมและ API ที่ช่วยควบคุมวิธีรันงาน; SRCG ไม่ได้แทน model หรือ training algorithm ของ PyTorch"),
    ("PyTorch", "library สำหรับสร้าง model, โหลด data, คำนวณ gradient และปรับ parameters ระหว่าง training"),
    ("SRCG", "framework ที่ครอบ PyTorch เพื่อควบคุม host RAM ด้วย cgroup และหน่วงการสร้าง batch เมื่อ RAM ไม่พอ"),
    ("resource", "สิ่งที่มี capacity จำกัดและโปรแกรมใช้ เช่น CPU time, host RAM หรือ GPU memory"),
    ("process", "โปรแกรมหนึ่ง instance ที่ Linux กำลังรันและมี process ID ของตัวเอง"),
    ("process tree", "main process และ child processes ทั้งหมดที่มันสร้างต่อกันเป็นลำดับ"),
    ("CPU", "หน่วยประมวลผลหลักของเครื่อง; preprocessing และ DataLoader มักทำงานฝั่งนี้"),
    ("GPU", "หน่วยประมวลผลแบบขนานที่ใช้เร่ง model training และมี VRAM ของตัวเอง"),
    ("RAM", "หน่วยความจำหลักที่ CPU และ Linux processes ใช้; ข้อมูลหายเมื่อปิดเครื่อง"),
    ("CUDA", "ระบบ software ของ NVIDIA ที่ PyTorch ใช้คำนวณบน GPU และจัดการ GPU memory"),
    ("tensor", "array หลายมิติที่ PyTorch ใช้เก็บ input, parameter, activation และ gradient"),
    ("allocator", "ส่วนของ runtime ที่ขอ คืน และ cache memory ให้ tensors หรือ objects"),
    ("cache", "memory ที่ยังถือไว้เพื่อใช้ซ้ำให้เร็วขึ้น แม้ข้อมูลเดิมอาจไม่ถูกใช้งานแล้ว"),
    ("dataset", "ชุด samples ทั้งหมดที่ใช้ใน workload เช่นภาพ COCO หรือ CT volumes ของ Lung"),
    ("sample", "ข้อมูลหนึ่งรายการจาก dataset เช่นภาพหนึ่งภาพหรือ CT volume หนึ่งชุด"),
    ("training", "กระบวนการให้ model คำนวณ loss, gradient และปรับ parameters ซ้ำหลาย steps"),
    ("training step", "หนึ่งรอบที่ใช้ batch ทำ forward, คำนวณ loss/backward และให้ optimizer ปรับ parameters"),
    ("epoch", "การที่ training ใช้ samples ตามแผนจนครบ dataset หนึ่งรอบ"),
    ("optimizer", "algorithm ที่ใช้ gradient ปรับ model parameters เช่น SGD หรือ AdamW"),
    ("gradient", "ค่าที่บอกทิศทางและขนาดการเปลี่ยน parameter เพื่อลด loss"),
    ("activation", "tensor ชั่วคราวที่ layer ของ model สร้างระหว่าง forward/backward"),
    ("loss", "ตัวเลขที่วัดความผิดพลาดของ prediction และเป็นเป้าหมายให้ optimizer ลดลง"),
    ("PyTorch baseline / baseline", "training เดิมที่ไม่ใช้ SRCG reservation; อาจยังอยู่ใน cgroup เพื่อให้วัด RAM ภายใต้ boundary เดียวกัน"),
    ("mode", "วิธีจัด resource ใน scenario เช่น baseline, sequential, fixed quota หรือ SRCG"),
    ("fixed quota / fixed", "mode ที่ memory limit คงที่และ job ยืม Reserved Memory ไม่ได้"),
    ("not_logged / unknown", "artifact รุ่นนั้นไม่ได้บันทึกค่าดังกล่าว จึงไม่เดาหรือแทนด้วยศูนย์"),
    ("workload", "งานทดลองหนึ่งชนิด ซึ่งรวม dataset, model, preprocessing และ training configuration"),
    ("configuration", "ชุดค่าที่กำหนดวิธีรัน เช่น batch size, epochs, optimizer, workers และ memory limit"),
    ("architecture", "โครงสร้าง layers และการเชื่อมต่อภายใน model ซึ่งกำหนดชนิดและจำนวน parameters"),
    ("model", "โครงข่ายประสาทที่รับ input แล้วคำนวณ prediction เช่น ResNet-50 หรือ 3D U-Net"),
    ("model parameter", "ค่าตัวเลขที่ model เรียนรู้ระหว่าง training เช่น weight และ bias"),
    ("model size", "ในรายงานนี้หมายถึงจำนวน model parameters ไม่ใช่ RAM ทั้งหมดที่ training ใช้"),
    ("FP32 weight bytes", "พื้นที่ของ parameters อย่างเดียวเมื่อแต่ละค่าเป็นเลข 32-bit หรือ 4 bytes; ไม่รวม gradient/optimizer/activation"),
    ("ResNet-50", "โมเดล convolution 2D ที่ใช้กับ COCO ใน full-model run"),
    ("3D U-Net", "โมเดล segmentation 3D ที่ใช้กับ Task06_Lung ใน full-model run"),
    ("COCO val2017", "ชุดภาพ JPEG 2D ที่ใช้ทดสอบ decode/resize และ training pipeline"),
    ("JPEG", "รูปแบบไฟล์ภาพ 2D แบบบีบอัด; ต้อง decode ก่อนจึงได้ pixel array"),
    ("Task06_Lung", "ชุด CT และ mask 3D ที่ใช้ทดสอบ RAM spike จากการโหลด volume ทั้งก้อน"),
    ("CT", "Computed Tomography: ภาพทางการแพทย์สามมิติที่เก็บค่าความหนาแน่นของเนื้อเยื่อ"),
    ("segmentation mask", "array ที่ระบุ class ของแต่ละ pixel/voxel เพื่อใช้เป็น target ของ segmentation model"),
    ("pixel / voxel", "pixel คือจุดข้อมูลภาพ 2D; voxel คือจุดข้อมูลปริมาตร 3D"),
    ("RAM spike / peak", "ช่วงสั้นที่การใช้ RAM เพิ่มสูงกว่าปกติ; peak คือค่าสูงสุดที่วัดได้ตลอดช่วง"),
    ("NIfTI", "รูปแบบไฟล์ medical image 3D; การแตกไฟล์ .nii.gz อาจสร้าง array ใน RAM ขนาดใหญ่"),
    ("decode", "อ่านไฟล์บีบอัดแล้วสร้างข้อมูล pixels/voxels จริงใน RAM"),
    ("resize", "เปลี่ยนขนาด image/volume; ระหว่างทำอาจมี input และ output อยู่ใน RAM พร้อมกัน"),
    ("batch", "กลุ่ม samples ที่ DataLoader ส่งให้ model ประมวลผลหนึ่ง training step"),
    ("DataLoader worker", "process ที่ PyTorch ใช้โหลดและ preprocess data; workers หลายตัวอาจใช้ RAM พร้อมกัน"),
    ("prefetch", "การเตรียม batches ล่วงหน้าก่อน model ขอใช้ ซึ่งเพิ่มจำนวน batches ที่ค้างใน RAM"),
    ("job", "process tree ของ training หนึ่งงาน รวม main process และ DataLoader workers"),
    ("concurrent", "หลาย jobs ทำงานในช่วงเวลาเดียวกัน"),
    ("sequential", "รัน job แรกให้จบก่อนจึงเริ่ม job ถัดไป"),
    ("scenario", "ชุดเงื่อนไขทดลองหนึ่งแบบ เช่น RAM limit, จำนวน jobs, mode และ workload"),
    ("trial (n=)", "การทำ scenario ซ้ำหนึ่งรอบ; n คือจำนวนรอบที่นำมารวม"),
    ("attempt", "การเริ่ม process หนึ่งครั้งใน recovery experiment; งานเดียวอาจมีหลาย attempts"),
    ("artifact", "ไฟล์หลักฐานจากการรัน เช่น log, summary, memory.events หรือ checkpoint"),
    ("valid", "รอบที่ไม่มี INVALID.txt และผ่านเงื่อนไขที่กำหนดสำหรับกราฟนั้น"),
    ("invalid", "รอบที่พบปัญหาการทดลองหรือ implementation จึงเก็บหลักฐานไว้แต่ห้ามใช้สรุปผล"),
    ("canonical comparison", "ชุดผลหลักที่คัดเฉพาะ modes ซึ่ง workload และเงื่อนไขสำคัญเทียบกันได้"),
    ("smoke test", "รอบสั้นเพื่อเช็กว่า code path รันได้ ไม่ใช่ผล performance ของ workload เต็ม"),
    ("calibration", "รอบทดลองหาค่าหรือ boundary ก่อนเก็บผลจริง ไม่ใช่ thesis timing result"),
    ("optimization history", "รอบระหว่างพัฒนาเพื่อหาสาเหตุและแก้ failure; ไม่ใช่ independent final trials"),
    ("cgroup", "กลไก Linux ที่รวม processes เป็นกลุ่มเพื่อวัดและจำกัด resource เช่น RAM"),
    ("parent cgroup", "cgroup ที่ครอบ training jobs ของ SRCG และกำหนดเพดาน RAM รวมของ jobs เหล่านั้น"),
    ("outer scope", "cgroup ระดับบนที่ครอบทั้ง SRCG parent, manager และ process จำลองที่อยู่นอก SRCG"),
    ("headroom", "capacity ที่กันไว้ไม่ให้ training ใช้ เพื่อเหลือให้ OS หรือ process นอก SRCG; ไม่ใช่ RAM ที่ allocate ไว้ล่วงหน้า"),
    ("Reserved Memory", "quota ส่วนกลางที่ SRCG ให้ jobs ยืมชั่วคราว; เป็นสิทธิ์เพิ่ม memory.max ไม่ใช่การจอง physical RAM"),
    ("reservation", "การที่ manager อนุมัติสิทธิ์ให้ job เพิ่ม memory.max ชั่วคราวก่อนสร้าง batch"),
    ("reservation token", "รหัสอ้างอิงสิทธิ์ที่อนุมัติ เพื่อให้ release/shrink สิทธิ์เดิมได้โดยไม่คืน quota ซ้ำ"),
    ("host RAM", "หน่วยความจำหลักของเครื่องที่ Linux และ CPU processes ใช้"),
    ("VRAM / GPU memory", "หน่วยความจำบน GPU; cgroup memory.max ไม่ได้จำกัด resource นี้"),
    ("CUDA allocated", "VRAM ที่ PyTorch tensors ใช้งานจริงตาม CUDA allocator ณ peak"),
    ("CUDA reserved", "VRAM ที่ PyTorch CUDA allocator ถือไว้เพื่อ reuse; รวมพื้นที่ว่างใน cache จึงมักมากกว่า allocated"),
    ("RSS", "Resident Set Size: RAM ที่ process มี resident อยู่โดยประมาณ; ไม่เท่ากับยอดทั้ง cgroup"),
    ("memory limit / boundary", "เพดาน RAM ที่ cgroup อนุญาต; allocation ที่ชนเพดานอาจถูก reclaim หรือ OOM-kill"),
    ("parent peak RAM", "ค่าสูงสุดของ memory.peak ใน parent cgroup ตลอด scenario รวมทุก training jobs ใต้ parent"),
    ("cgroup memory peak", "ค่าสูงสุดของ RAM ที่ cgroup ของ training log นั้นถูก charge"),
    ("OOM", "Out Of Memory: ระบบไม่สามารถตอบ allocation ภายใต้ capacity/limit ปัจจุบัน"),
    ("OOM-kill", "kernel ส่ง SIGKILL เพื่อหยุด process และคืน RAM"),
    ("signal / SIGKILL", "signal คือข้อความควบคุม process จากระบบ; SIGKILL หยุด process ทันทีและ process ดักเพื่อ save ไม่ได้"),
    ("exit status", "ตัวเลขที่ process ส่งเมื่อจบ; 0 หมายถึงสำเร็จตาม convention ส่วนค่าที่ไม่ใช่ 0 หมายถึง error/termination"),
    ("completed job", "training ทำครบ steps/samples ตาม workload และ process จบด้วย exit status 0"),
    ("makespan", "เวลาจากเริ่ม scenario จน process ที่ scenario รออยู่ทั้งหมดจบ ไม่ใช่เวลาของ job เดี่ยวเสมอ"),
    ("time-to-failure", "เวลาตั้งแต่เริ่มจนงานล้ม; ห้ามตีความว่าสั้นกว่าแล้วเร็วกว่า เพราะไม่ได้ผลลัพธ์สำเร็จ"),
    ("time-to-result", "เวลาจนได้ผล training ที่สมบูรณ์; ใช้เปรียบเทียบ performance ได้เมื่อทุก mode จบงานเดียวกัน"),
    ("elapsed time", "เวลาที่ training log หนึ่งตัวใช้ตั้งแต่เริ่มถึงจบการรันนั้น"),
    ("throughput", "ปริมาณงานที่จบต่อเวลา เช่น samples/second หรือ steps/second"),
    ("checkpoint", "ไฟล์สถานะ model/optimizer/ตำแหน่ง data ที่เขียนเสร็จแล้ว เพื่อ resume ภายหลัง"),
    ("resume", "เริ่ม training process ใหม่แล้วโหลด checkpoint เพื่อทำต่อจากสถานะที่บันทึกไว้"),
    ("hash / SHA-256", "ค่าลายนิ้วมือของข้อมูล; hash ตรงกันใช้ยืนยันว่า bytes ของ final model ตรงกัน"),
    ("performance", "เวลาและอัตราการทำงานเมื่อได้ผลลัพธ์ที่ถูกต้อง เช่น time-to-result และ throughput"),
    ("safety result", "ผลที่ตอบว่างานจบหรือถูก OOM-kill ภายใต้ resource pressure ไม่ใช่คำตอบเรื่องความเร็วเพียงอย่างเดียว"),
    ("static scheduling", "กำหนด limit หรือลำดับรันไว้ล่วงหน้าโดยไม่ปรับตาม RAM usage ระหว่างทำงาน"),
    ("randomized order", "สลับลำดับว่า mode ใดรันก่อนในแต่ละ trial เพื่อลดผลจาก cache/อุณหภูมิ/สภาพเครื่อง"),
    ("mean", "ค่าเฉลี่ยเลขคณิตของ trials ที่เลือก"),
    ("median", "ค่ากลางหลังเรียงตัวเลข ช่วยลดผลของค่าที่สูงหรือต่ำผิดปกติ"),
    ("log scale", "แกนที่ระยะเท่ากันหมายถึงการคูณ เช่น 1→10 เท่ากับ 10→100 ไม่ใช่การบวกเท่ากัน"),
    ("JSONL", "ไฟล์ข้อความที่แต่ละบรรทัดเป็น JSON event หนึ่งรายการจาก training"),
    ("CSV", "ไฟล์ตาราง comma-separated สำหรับเปิดด้วย spreadsheet หรือวิเคราะห์ต่อ"),
    ("MiB / GiB", "หน่วยข้อมูลฐาน 1024: 1 MiB = 1,048,576 bytes และ 1 GiB = 1,073,741,824 bytes"),
    ("raw artifact", "ไฟล์หลักฐานต้นทางที่ยังไม่ถูกรวมค่า เช่น JSONL log หรือ memory.events"),
    ("performance_summary", "JSON event ท้าย training ที่รวมเวลา, peak memory, throughput และ final model hash"),
]


@dataclass
class TrainingRecord:
    path: Path
    relative_path: str
    run_directory: Path
    invalid: bool
    workload: str
    model: str
    model_parameters: int | None
    mode: str
    num_workers: int | None
    batch_size: int | None
    processed_steps: int | None
    processed_samples: int | None
    elapsed_seconds: float | None
    cgroup_peak_bytes: int | None
    peak_rss_mib: float | None
    cuda_peak_allocated_mib: float | None
    cuda_peak_reserved_mib: float | None
    samples_per_second: float | None
    final_model_sha256: str | None
    training_end_reason: str | None
    non_json_lines: int


@dataclass
class SummaryRecord:
    path: Path
    relative_path: str
    run_directory: Path
    invalid: bool
    category: str
    mode: str
    completed_jobs: int
    total_jobs: int
    statuses: str
    makespan_seconds: float | None
    parent_peak_bytes: int | None
    scope_peak_bytes: int | None
    oom_kills: int | None
    num_workers: int | None
    parent_memory: str
    model: str
    model_parameters: int | None


def read_json_objects(path: Path) -> tuple[list[dict[str, Any]], int]:
    objects: list[dict[str, Any]] = []
    non_json_lines = 0
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return objects, non_json_lines
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            non_json_lines += 1
            continue
        if isinstance(value, dict):
            objects.append(value)
    return objects, non_json_lines


def invalid_reason(path: Path, artifacts_root: Path) -> bool:
    current = path if path.is_dir() else path.parent
    while True:
        if (current / "INVALID.txt").exists():
            return True
        if current == artifacts_root or current.parent == current:
            return False
        current = current.parent


def first_event(events: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    return next((event for event in events if event.get("event") == name), None)


def last_event(events: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    return next(
        (event for event in reversed(events) if event.get("event") == name), None
    )


def infer_mode(path: Path, dataset_ready: dict[str, Any] | None,
               training_start: dict[str, Any] | None) -> str:
    for event in (dataset_ready, training_start):
        if event is not None and isinstance(event.get("srcg_reservation"), bool):
            return "srcg" if event["srcg_reservation"] else "baseline"
    lowered = str(path).lower()
    if "sequential" in lowered:
        return "sequential"
    if any(word in lowered for word in ("srcg", "borrow", "reservation")):
        return "srcg"
    if "fixed" in lowered:
        return "fixed"
    if any(word in lowered for word in ("normal", "baseline", "uninterrupted")):
        return "baseline"
    return "unknown"


def parse_training(path: Path, artifacts_root: Path) -> TrainingRecord | None:
    events, non_json_lines = read_json_objects(path)
    config_event = first_event(events, "run_config")
    start = first_event(events, "training_start")
    dataset_ready = first_event(events, "dataset_ready")
    performance = last_event(events, "performance_summary")
    training_end = last_event(events, "training_end")
    if not any((config_event, start, dataset_ready, performance)):
        return None

    config = config_event.get("config", {}) if config_event else {}
    if not isinstance(config, dict):
        config = {}
    workload = str(
        config.get("workload")
        or (performance or {}).get("workload")
        or (dataset_ready or {}).get("workload")
        or "unknown"
    )
    model = str(config.get("model") or (start or {}).get("model") or "not_logged")
    model_parameters_value = (start or {}).get("model_parameters")
    model_parameters = (
        int(model_parameters_value) if model_parameters_value is not None else None
    )
    return TrainingRecord(
        path=path,
        relative_path=str(path.relative_to(artifacts_root)),
        run_directory=path.parent,
        invalid=invalid_reason(path, artifacts_root),
        workload=workload,
        model=model,
        model_parameters=model_parameters,
        mode=infer_mode(path, dataset_ready, start),
        num_workers=(
            int(config["num_workers"]) if config.get("num_workers") is not None else None
        ),
        batch_size=(
            int(config.get("batch_size", (start or {}).get("batch_size")))
            if config.get("batch_size", (start or {}).get("batch_size")) is not None
            else None
        ),
        processed_steps=(
            int(performance["processed_steps"])
            if performance and performance.get("processed_steps") is not None
            else None
        ),
        processed_samples=(
            int(performance["processed_samples"])
            if performance and performance.get("processed_samples") is not None
            else None
        ),
        elapsed_seconds=(
            float(performance["elapsed_seconds"])
            if performance and performance.get("elapsed_seconds") is not None
            else None
        ),
        cgroup_peak_bytes=(
            int(performance["cgroup_memory_peak_bytes"])
            if performance and performance.get("cgroup_memory_peak_bytes") is not None
            else None
        ),
        peak_rss_mib=(
            float(performance["peak_rss_mib"])
            if performance and performance.get("peak_rss_mib") is not None
            else None
        ),
        cuda_peak_allocated_mib=(
            float(performance["cuda_peak_allocated_mib"])
            if performance and performance.get("cuda_peak_allocated_mib") is not None
            else None
        ),
        cuda_peak_reserved_mib=(
            float(performance["cuda_peak_reserved_mib"])
            if performance and performance.get("cuda_peak_reserved_mib") is not None
            else None
        ),
        samples_per_second=(
            float(performance["samples_per_second"])
            if performance and performance.get("samples_per_second") is not None
            else None
        ),
        final_model_sha256=(
            str(performance["final_model_sha256"])
            if performance and performance.get("final_model_sha256") is not None
            else None
        ),
        training_end_reason=(
            str(training_end.get("reason")) if training_end is not None else None
        ),
        non_json_lines=non_json_lines,
    )


def parse_key_values(path: Path) -> dict[str, str]:
    try:
        first_line = next(
            line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
        )
    except (OSError, StopIteration):
        return {}
    result: dict[str, str] = {}
    for token in shlex.split(first_line):
        key, separator, value = token.partition("=")
        if separator:
            result[key] = value
    return result


def read_memory_events(path: Path) -> int | None:
    try:
        values = {
            fields[0]: int(fields[1])
            for line in path.read_text(encoding="utf-8").splitlines()
            if len(fields := line.split()) == 2
        }
    except (OSError, ValueError):
        return None
    return values.get("oom_kill")


def classify_category(relative: str) -> str:
    if "capacity-sweep" in relative:
        return "Capacity sweep"
    if "late-arrival" in relative:
        return "Late arrival"
    if "/timing/" in relative:
        return "Timing"
    if "/optimization/" in relative:
        return "Optimization history"
    if "/real-workloads/" in relative:
        return "Real-workload development"
    if "/cold-boot/" in relative:
        return "Cold boot"
    if "milestone4/calibration" in relative:
        return "M4 calibration"
    if "milestone4/recovery" in relative:
        return "M4 recovery"
    return "Other"


def nearest_training_records(
    directory: Path, training_by_directory: dict[Path, list[TrainingRecord]]
) -> list[TrainingRecord]:
    return training_by_directory.get(directory, [])


def parse_summary(
    path: Path,
    artifacts_root: Path,
    training_by_directory: dict[Path, list[TrainingRecord]],
) -> SummaryRecord | None:
    fields = parse_key_values(path)
    if not fields:
        return None
    relative = str(path.relative_to(artifacts_root))
    attempt_schema = "attempt" in fields
    if attempt_schema:
        statuses = [fields.get("exit_status", "unknown")]
        completed_jobs = int(fields.get("result") == "completed")
        total_jobs = 1
        if "started_unix_ns" in fields and "ended_unix_ns" in fields:
            makespan = (
                int(fields["ended_unix_ns"]) - int(fields["started_unix_ns"])
            ) / 1_000_000_000
        else:
            makespan = None
    else:
        statuses = [
            value
            for value in (fields.get("first_status"), fields.get("second_status"))
            if value is not None
        ]
        completed_jobs = sum(value == "0" for value in statuses)
        total_jobs = len(statuses)
        makespan = (
            int(fields["makespan_ms"]) / 1000
            if fields.get("makespan_ms") is not None
            else None
        )
    parent_peak = (
        int(fields["parent_peak_bytes"])
        if fields.get("parent_peak_bytes") is not None
        else None
    )
    scope_peak = (
        int(fields["scope_peak_bytes"])
        if fields.get("scope_peak_bytes") is not None
        else None
    )
    oom_kills = (
        int(fields["oom_kill"])
        if fields.get("oom_kill") is not None
        else read_memory_events(path.parent / "parent-memory.events")
    )
    related_training = nearest_training_records(path.parent, training_by_directory)
    known_models = sorted(
        {record.model for record in related_training if record.model != "not_logged"}
    )
    known_parameters = sorted(
        {
            record.model_parameters
            for record in related_training
            if record.model_parameters is not None
        }
    )
    mode = fields.get("mode", "unknown")
    if mode == "normal":
        mode = "baseline"
    return SummaryRecord(
        path=path,
        relative_path=relative,
        run_directory=path.parent,
        invalid=invalid_reason(path, artifacts_root),
        category=classify_category(relative),
        mode=mode,
        completed_jobs=completed_jobs,
        total_jobs=total_jobs,
        statuses=",".join(statuses),
        makespan_seconds=makespan,
        parent_peak_bytes=parent_peak,
        scope_peak_bytes=scope_peak,
        oom_kills=oom_kills,
        num_workers=(
            int(fields["num_workers"])
            if fields.get("num_workers") is not None
            else next(
                (
                    record.num_workers
                    for record in related_training
                    if record.num_workers is not None
                ),
                None,
            )
        ),
        parent_memory=fields.get("parent_memory", "not_logged"),
        model="+".join(known_models) if known_models else "not_logged",
        model_parameters=known_parameters[0] if len(known_parameters) == 1 else None,
    )


def svg_text(x: float, y: float, text: str, *, size: int = 12,
             anchor: str = "start", weight: int = 400,
             color: str = COLORS["ink"], extra: str = "") -> str:
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" '
        f'text-anchor="{anchor}" font-weight="{weight}" fill="{color}" '
        f'{extra}>{escape(text)}</text>'
    )


def rect(x: float, y: float, width: float, height: float, fill: str,
         *, opacity: float = 1.0, stroke: str = "none", radius: float = 0) -> str:
    return (
        f'<rect x="{x:.1f}" y="{y:.1f}" width="{max(0, width):.1f}" '
        f'height="{max(0, height):.1f}" fill="{fill}" opacity="{opacity:.3f}" '
        f'stroke="{stroke}" rx="{radius:.1f}"/>'
    )


def line(x1: float, y1: float, x2: float, y2: float,
         color: str = COLORS["grid"], width: float = 1,
         dash: str = "") -> str:
    dash_attribute = f' stroke-dasharray="{dash}"' if dash else ""
    return (
        f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
        f'stroke="{color}" stroke-width="{width:.1f}"{dash_attribute}/>'
    )


def svg_document(width: int, height: int, title: str, body: Iterable[str]) -> str:
    return "\n".join(
        [
            '<?xml version="1.0" encoding="UTF-8"?>',
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}" role="img" aria-label="{escape(title)}">',
            f"<title>{escape(title)}</title>",
            "<style>text{font-family:Inter,Segoe UI,Arial,sans-serif}" 
            ".mono{font-family:ui-monospace,SFMono-Regular,Consolas,monospace}</style>",
            rect(0, 0, width, height, COLORS["paper"]),
            *body,
            "</svg>",
        ]
    )


def write_svg(path: Path, width: int, height: int, title: str,
              body: Iterable[str]) -> None:
    path.write_text(svg_document(width, height, title, body), encoding="utf-8")


def graph_glossary(output: Path) -> None:
    columns = 3
    rows = math.ceil(len(GLOSSARY) / columns)
    column_width = 590
    item_height = 82
    width = 1810
    top = 175
    height = top + rows * item_height + 105
    body: list[str] = [
        svg_text(30, 42, "เริ่มอ่านตรงนี้: คำศัพท์และวิธีไม่ให้ตีความกราฟผิด", size=27, weight=700),
        svg_text(
            30,
            72,
            "ทุกคำเทคนิคที่ใช้ในกราฟถูก define ด้านล่าง กราฟผลหลักใช้ English label เดิมเพื่อให้ตรงกับชื่อ field ใน artifact",
            size=13,
            color=COLORS["muted"],
        ),
        rect(30, 92, width - 60, 58, "#FFF3E0", radius=8),
        svg_text(48, 116, "กฎสำคัญ", size=12, weight=700, color="#8A5700"),
        svg_text(
            48,
            138,
            "เวลา time-to-failure ของงานที่ล้มไม่ใช่ความเร็ว และ model size ไม่เท่ากับ RAM ทั้งหมดของ training",
            size=12,
            color="#8A5700",
        ),
    ]
    for index, (term, definition) in enumerate(GLOSSARY):
        column = index // rows
        row = index % rows
        x = 30 + column * column_width
        y = top + row * item_height
        body.append(svg_text(x, y, term, size=12, weight=700, color="#203040"))
        wrapped = textwrap.wrap(definition, width=80, break_long_words=False) or [definition]
        for line_index, text_line in enumerate(wrapped[:3]):
            body.append(
                svg_text(
                    x,
                    y + 20 + line_index * 16,
                    text_line,
                    size=10,
                    color=COLORS["muted"],
                )
            )
        body.append(line(x, y + item_height - 12, x + column_width - 25, y + item_height - 12, "#EEF1F4"))
    body.append(
        svg_text(
            30,
            height - 35,
            "Definition แบบเต็มและกฎคัดผลอยู่ใน report.md; CSV เก็บ source path ของทุกแถวเพื่อย้อนตรวจ raw artifact ได้",
            size=12,
            color=COLORS["muted"],
        )
    )
    write_svg(output, width, height, "SRCG graph glossary and reading guide", body)


def mode_color(mode: str) -> str:
    return COLORS.get(mode, COLORS["unknown"])


def short_path(relative: str, maximum: int = 76) -> str:
    value = relative.removesuffix("/summary.txt")
    if len(value) <= maximum:
        return value
    return "…" + value[-(maximum - 1):]


def graph_all_summaries(records: list[SummaryRecord], output: Path) -> None:
    ordered = sorted(records, key=lambda record: (record.category, record.relative_path))
    row_height = 28
    top = 110
    height = top + row_height * len(ordered) + 80
    width = 1580
    label_x = 18
    mode_x = 650
    outcome_x = 755
    time_x = 880
    time_width = 310
    peak_x = 1245
    peak_width = 240
    maximum_time = max(
        (record.makespan_seconds or 0 for record in ordered), default=1
    )
    maximum_peak = max(
        ((record.parent_peak_bytes or 0) / GIB for record in ordered), default=1
    )
    body: list[str] = [
        svg_text(18, 34, "All parsed scenario summaries — ผลทุก scenario ที่อ่านได้", size=24, weight=700),
        svg_text(
            18,
            58,
            "scenario = เงื่อนไขทดลองหนึ่งชุด; grey = invalid ห้ามสรุปผล; red = มี training job ล้ม ดู definition ทั้งหมดใน 00-how-to-read",
            size=12,
            color=COLORS["muted"],
        ),
        svg_text(label_x, 92, "Artifact / scenario", size=12, weight=700),
        svg_text(mode_x, 92, "Mode", size=12, weight=700),
        svg_text(outcome_x, 92, "Completed", size=12, weight=700),
        svg_text(time_x, 92, f"Makespan = เวลารวม (0–{maximum_time:.0f}s)", size=12, weight=700),
        svg_text(peak_x, 92, f"Parent peak = RAM สูงสุดรวม (0–{maximum_peak:.1f} GiB)", size=12, weight=700),
    ]
    previous_category = ""
    for index, record in enumerate(ordered):
        y = top + index * row_height
        if record.category != previous_category:
            body.append(line(12, y - 14, width - 12, y - 14, COLORS["grid"], 1.5))
            previous_category = record.category
        if record.invalid:
            body.append(rect(8, y - 14, width - 16, row_height - 1, "#F1F3F4"))
        body.append(svg_text(label_x, y + 5, short_path(record.relative_path), size=9, extra='class="mono"'))
        color = COLORS["invalid"] if record.invalid else mode_color(record.mode)
        body.append(rect(mode_x, y - 10, 82, 18, color, opacity=0.92, radius=8))
        body.append(svg_text(mode_x + 41, y + 3, record.mode, size=9, anchor="middle", weight=700, color="#FFFFFF"))
        outcome_color = (
            COLORS["invalid"]
            if record.invalid
            else COLORS["srcg"]
            if record.completed_jobs == record.total_jobs and record.total_jobs > 0
            else COLORS["failed"]
        )
        outcome = f"{record.completed_jobs}/{record.total_jobs}"
        if record.invalid:
            outcome += " invalid"
        body.append(svg_text(outcome_x, y + 5, outcome, size=10, weight=700, color=outcome_color))
        if record.makespan_seconds is not None:
            body.append(rect(time_x, y - 9, time_width, 14, "#EEF1F4", radius=4))
            body.append(
                rect(
                    time_x,
                    y - 9,
                    time_width * record.makespan_seconds / maximum_time,
                    14,
                    outcome_color,
                    opacity=0.72,
                    radius=4,
                )
            )
            body.append(svg_text(time_x + time_width + 8, y + 3, f"{record.makespan_seconds:.1f}s", size=9))
        if record.parent_peak_bytes is not None:
            peak_gib = record.parent_peak_bytes / GIB
            body.append(rect(peak_x, y - 9, peak_width, 14, "#EEF1F4", radius=4))
            body.append(
                rect(
                    peak_x,
                    y - 9,
                    peak_width * peak_gib / maximum_peak,
                    14,
                    color,
                    opacity=0.72,
                    radius=4,
                )
            )
            body.append(svg_text(peak_x + peak_width + 7, y + 3, f"{peak_gib:.2f}", size=9))
    body.append(
        svg_text(
            18,
            height - 24,
            "Failed-run makespan = time-to-failure ไม่ใช่ successful time-to-result; อ่าน 00-how-to-read ก่อนเทียบ bars",
            size=11,
            color=COLORS["muted"],
        )
    )
    write_svg(output, width, height, "All SRCG scenario experiment summaries", body)


def median(values: Iterable[float | int | None]) -> float | None:
    usable = [float(value) for value in values if value is not None]
    return statistics.median(usable) if usable else None


def graph_model_sizes(records: list[TrainingRecord], output: Path) -> None:
    valid = [
        record
        for record in records
        if not record.invalid and record.model_parameters is not None
    ]
    grouped: dict[tuple[str, int], list[TrainingRecord]] = {}
    for record in valid:
        grouped.setdefault((record.model, record.model_parameters or 0), []).append(record)
    models = sorted(grouped, key=lambda item: item[1], reverse=True)
    width, height = 1320, 650
    body: list[str] = [
        svg_text(30, 42, "Model size (จำนวน parameters) และ GPU memory ที่วัดได้", size=26, weight=700),
        svg_text(
            30,
            68,
            "parameter = ค่าที่ model เรียนรู้; GPU peaks ใช้ median ของ valid completed logs ที่บันทึก full model เดียวกัน",
            size=12,
            color=COLORS["muted"],
        ),
        svg_text(45, 115, "A. Parameters", size=17, weight=700),
        svg_text(690, 115, "B. CUDA peak memory (VRAM สูงสุด)", size=17, weight=700),
    ]
    if not models:
        body.append(svg_text(40, 160, "No model parameter records found", size=14))
        write_svg(output, width, height, "Model sizes", body)
        return
    max_parameters = max(parameters for _model, parameters in models)
    max_cuda = max(
        [
            float(value)
            for group in grouped.values()
            for record in group
            for value in (record.cuda_peak_allocated_mib, record.cuda_peak_reserved_mib)
            if value is not None
        ]
        or [1.0]
    )
    for index, (model, parameters) in enumerate(models):
        y = 175 + index * 150
        group = grouped[(model, parameters)]
        completed = [record for record in group if record.elapsed_seconds is not None]
        color = "#735CDD" if model == "resnet50" else "#D97706"
        body.append(svg_text(45, y, model, size=15, weight=700))
        body.append(rect(45, y + 18, 550, 36, "#EEF1F4", radius=5))
        body.append(rect(45, y + 18, 550 * parameters / max_parameters, 36, color, opacity=0.86, radius=5))
        fp32_mib = parameters * 4 / MIB
        body.append(
            svg_text(
                55,
                y + 42,
                f"{parameters / 1_000_000:.2f} M params  ≈ {fp32_mib:.1f} MiB FP32 weights",
                size=12,
                weight=700,
                color="#FFFFFF",
            )
        )
        allocated = median(record.cuda_peak_allocated_mib for record in completed)
        reserved = median(record.cuda_peak_reserved_mib for record in completed)
        body.append(svg_text(690, y, f"{model} (n={len(completed)} logs)", size=15, weight=700))
        for bar_index, (label, value, bar_color) in enumerate(
            (
                ("allocated", allocated, "#4C78A8"),
                ("reserved", reserved, "#E45756"),
            )
        ):
            bar_y = y + 16 + bar_index * 42
            body.append(svg_text(690, bar_y + 19, label, size=11))
            body.append(rect(770, bar_y, 430, 26, "#EEF1F4", radius=4))
            if value is not None:
                body.append(rect(770, bar_y, 430 * value / max_cuda, 26, bar_color, opacity=0.85, radius=4))
                body.append(svg_text(1210, bar_y + 18, f"{value:.1f} MiB", size=11))
    body.extend(
        [
            rect(45, height - 105, width - 90, 70, "#FFF7E6", radius=8),
            svg_text(60, height - 78, "Definition สำคัญ", size=12, weight=700, color="#8A5700"),
            svg_text(
                60,
                height - 55,
                "allocated = VRAM ที่ tensor ใช้; reserved = VRAM ที่ allocator ถือไว้ reuse; Lung host-RAM spike มาจาก NIfTI decode/resize",
                size=12,
                color="#8A5700",
            ),
        ]
    )
    write_svg(output, width, height, "SRCG model size and GPU memory", body)


def aggregate(label: str, mode: str, records: list[SummaryRecord]) -> dict[str, Any]:
    return {
        "label": label,
        "mode": mode,
        "records": records,
        "trials": len(records),
        "completed": statistics.mean(record.completed_jobs for record in records),
        "jobs": max((record.total_jobs for record in records), default=0),
        "makespan": statistics.mean(
            record.makespan_seconds for record in records if record.makespan_seconds is not None
        ),
        "peak": statistics.mean(
            record.parent_peak_bytes / GIB
            for record in records
            if record.parent_peak_bytes is not None
        ),
    }


def select_records(records: list[SummaryRecord], fragments: Iterable[str]) -> list[SummaryRecord]:
    selected: list[SummaryRecord] = []
    for record in records:
        if record.invalid:
            continue
        if any(fragment in record.relative_path for fragment in fragments):
            selected.append(record)
    return selected


def canonical_groups(records: list[SummaryRecord]) -> list[tuple[str, list[dict[str, Any]]]]:
    definitions = [
        (
            "4 GiB capacity / two Lung jobs",
            [
                ("PyTorch concurrent", "baseline", ["capacity-sweep/4g-normal-"]),
                ("PyTorch sequential", "sequential", ["capacity-sweep/sequential-normal-"]),
                ("SRCG concurrent", "srcg", ["capacity-sweep/ancestor-aware-srcg-"]),
            ],
        ),
        (
            "Late 512 MiB process / 5 GiB outer scope",
            [
                ("PyTorch concurrent", "baseline", ["late-arrival/ancestor-aware-normal-"]),
                ("SRCG + 1 GiB headroom", "srcg", ["late-arrival/ancestor-aware-srcg-"]),
            ],
        ),
        (
            "Normal pressure / 8 GiB parent / workers=2",
            [
                ("PyTorch concurrent", "baseline", ["timing/pressure-normal-1/"]),
                ("SRCG concurrent", "srcg", ["timing/pressure-srcg-1/"]),
            ],
        ),
    ]
    result: list[tuple[str, list[dict[str, Any]]]] = []
    for scenario, entries in definitions:
        aggregates: list[dict[str, Any]] = []
        for label, mode, fragments in entries:
            selected = select_records(records, fragments)
            if selected:
                aggregates.append(aggregate(label, mode, selected))
        result.append((scenario, aggregates))
    return result


def graph_canonical(records: list[SummaryRecord], output: Path) -> None:
    groups = canonical_groups(records)
    width, height = 1540, 870
    body: list[str] = [
        svg_text(30, 40, "Canonical comparison — คัดเฉพาะเงื่อนไขที่เทียบกันได้", size=26, weight=700),
        svg_text(
            30,
            66,
            "mean = ค่าเฉลี่ย trials; failed PyTorch bar = time-to-failure ห้ามอ่านว่าเร็วกว่า เพราะ training ไม่จบ",
            size=12,
            color=COLORS["muted"],
        ),
    ]
    time_x, peak_x, completion_x = 565, 965, 1280
    time_width, peak_width, completion_width = 310, 230, 170
    body.extend(
        [
            svg_text(30, 103, "Scenario / mode", size=13, weight=700),
            svg_text(time_x, 103, "Makespan = เวลารวม", size=13, weight=700),
            svg_text(peak_x, 103, "Parent peak = RAM สูงสุดรวม", size=13, weight=700),
            svg_text(completion_x, 103, "Completed = งานที่ทำครบ", size=13, weight=700),
        ]
    )
    max_time = max(item["makespan"] for _scenario, items in groups for item in items)
    max_peak = max(item["peak"] for _scenario, items in groups for item in items)
    y = 145
    for scenario, items in groups:
        body.append(rect(18, y - 25, width - 36, 34 + 58 * len(items), "#F8FAFB", radius=9))
        body.append(svg_text(30, y, scenario, size=15, weight=700))
        y += 32
        for item in items:
            color = mode_color(item["mode"])
            completion_ratio = item["completed"] / item["jobs"] if item["jobs"] else 0
            failed = completion_ratio < 1.0
            body.append(rect(42, y - 16, 13, 13, color, radius=3))
            body.append(svg_text(64, y - 5, f'{item["label"]}  (n={item["trials"]})', size=12))
            body.append(rect(time_x, y - 20, time_width, 22, "#E9EDF1", radius=4))
            body.append(
                rect(
                    time_x,
                    y - 20,
                    time_width * item["makespan"] / max_time,
                    22,
                    COLORS["failed"] if failed else color,
                    opacity=0.82,
                    radius=4,
                )
            )
            suffix = " to failure" if failed else ""
            body.append(svg_text(time_x + time_width + 8, y - 5, f'{item["makespan"]:.1f}s{suffix}', size=10))
            body.append(rect(peak_x, y - 20, peak_width, 22, "#E9EDF1", radius=4))
            body.append(rect(peak_x, y - 20, peak_width * item["peak"] / max_peak, 22, color, opacity=0.82, radius=4))
            body.append(svg_text(peak_x + peak_width + 8, y - 5, f'{item["peak"]:.2f} GiB', size=10))
            body.append(rect(completion_x, y - 20, completion_width, 22, "#E9EDF1", radius=4))
            body.append(rect(completion_x, y - 20, completion_width * completion_ratio, 22, COLORS["srcg"], opacity=0.82, radius=4))
            body.append(svg_text(completion_x + completion_width + 8, y - 5, f'{item["completed"]:.1f}/{item["jobs"]}', size=10))
            y += 50
        y += 36
    body.extend(
        [
            svg_text(30, height - 58, "Scope limitation", size=12, weight=700, color="#8A5700"),
            svg_text(
                30,
                height - 36,
                "These baselines use ordinary/conservative PyTorch settings, not every possible manually tuned static scheduler configuration.",
                size=12,
                color="#8A5700",
            ),
        ]
    )
    write_svg(output, width, height, "Canonical SRCG comparisons", body)


def graph_optimization(records: list[SummaryRecord], output: Path) -> None:
    selected = sorted(
        [record for record in records if record.category == "Optimization history"],
        key=lambda record: record.relative_path,
    )
    width = 1500
    row_height = 40
    height = 125 + row_height * len(selected) + 70
    body: list[str] = [
        svg_text(24, 38, "Milestone 3 optimization history — รอบพัฒนาที่สำเร็จและล้ม", size=25, weight=700),
        svg_text(
            24,
            64,
            "optimization history = รอบหา failure/fix ไม่ใช่ final trials; makespan ของแถวแดงคือ time-to-failure",
            size=12,
            color=COLORS["muted"],
        ),
        svg_text(24, 102, "Implementation variant", size=12, weight=700),
        svg_text(700, 102, "Outcome", size=12, weight=700),
        svg_text(825, 102, "Makespan", size=12, weight=700),
        svg_text(1195, 102, "Peak RAM", size=12, weight=700),
    ]
    max_time = max((record.makespan_seconds or 0 for record in selected), default=1)
    max_peak = max(((record.parent_peak_bytes or 0) / GIB for record in selected), default=1)
    for index, record in enumerate(selected):
        y = 130 + row_height * index
        label = Path(record.relative_path).parent.name
        body.append(svg_text(24, y + 6, label, size=11, extra='class="mono"'))
        body.append(svg_text(520, y + 6, f"workers={record.num_workers if record.num_workers is not None else '?'}", size=10, color=COLORS["muted"]))
        success = record.total_jobs > 0 and record.completed_jobs == record.total_jobs
        outcome_color = COLORS["srcg"] if success else COLORS["failed"]
        body.append(svg_text(700, y + 6, f"{record.completed_jobs}/{record.total_jobs}", size=12, weight=700, color=outcome_color))
        if record.makespan_seconds is not None:
            body.append(rect(825, y - 9, 285, 20, "#E9EDF1", radius=4))
            body.append(rect(825, y - 9, 285 * record.makespan_seconds / max_time, 20, outcome_color, opacity=0.78, radius=4))
            body.append(svg_text(1118, y + 6, f"{record.makespan_seconds:.1f}s", size=10))
        if record.parent_peak_bytes is not None:
            peak = record.parent_peak_bytes / GIB
            body.append(rect(1195, y - 9, 220, 20, "#E9EDF1", radius=4))
            body.append(rect(1195, y - 9, 220 * peak / max_peak, 20, mode_color(record.mode), opacity=0.78, radius=4))
            body.append(svg_text(1422, y + 6, f"{peak:.2f} GiB", size=10))
        body.append(line(20, y + 20, width - 20, y + 20, "#EFF2F4"))
    write_svg(output, width, height, "SRCG optimization history", body)


def graph_training_scatter(records: list[TrainingRecord], output: Path) -> None:
    completed = [
        record
        for record in records
        if not record.invalid
        and record.elapsed_seconds is not None
        and record.cgroup_peak_bytes is not None
    ]
    panels = [
        ("ResNet-50 / COCO", [record for record in completed if record.model == "resnet50"]),
        ("3D U-Net / Lung", [record for record in completed if record.model == "unet3d"]),
        ("Legacy prototype / size not logged", [record for record in completed if record.model == "not_logged"]),
    ]
    width, height = 1590, 630
    body: list[str] = [
        svg_text(28, 38, "Completed training logs: elapsed time เทียบ cgroup memory peak", size=25, weight=700),
        svg_text(
            28,
            64,
            "หนึ่งจุด = performance_summary หนึ่ง log; แกนเวลาเป็น log scale; host-scope และ smoke runs ยังอยู่จึงต้องเปิด CSV ก่อนเทียบ",
            size=12,
            color=COLORS["muted"],
        ),
    ]
    panel_width, panel_height = 470, 430
    panel_y = 125
    tick_times = [1, 2, 5, 10, 20, 50, 100, 200]
    max_time = max([record.elapsed_seconds or 1 for record in completed] or [1])
    max_peak = max([(record.cgroup_peak_bytes or 0) / GIB for record in completed] or [1])
    log_min, log_max = 0.0, math.log10(max(2, max_time))
    for panel_index, (title, group) in enumerate(panels):
        panel_x = 55 + panel_index * 515
        body.append(svg_text(panel_x, 104, f"{title} (n={len(group)})", size=15, weight=700))
        body.append(rect(panel_x, panel_y, panel_width, panel_height, "#FBFCFD", stroke=COLORS["grid"], radius=5))
        for peak_tick in range(0, math.ceil(max_peak) + 1, 2):
            y = panel_y + panel_height - panel_height * peak_tick / max_peak
            body.append(line(panel_x, y, panel_x + panel_width, y, "#E7EBEE"))
            body.append(svg_text(panel_x - 8, y + 4, str(peak_tick), size=9, anchor="end", color=COLORS["muted"]))
        for tick in tick_times:
            if tick > max_time:
                continue
            x = panel_x + panel_width * (math.log10(tick) - log_min) / (log_max - log_min)
            body.append(line(x, panel_y, x, panel_y + panel_height, "#EFF2F4"))
            body.append(svg_text(x, panel_y + panel_height + 18, str(tick), size=9, anchor="middle", color=COLORS["muted"]))
        for record in group:
            elapsed = max(1.0, record.elapsed_seconds or 1.0)
            peak = (record.cgroup_peak_bytes or 0) / GIB
            x = panel_x + panel_width * (math.log10(elapsed) - log_min) / (log_max - log_min)
            y = panel_y + panel_height - panel_height * peak / max_peak
            radius = 5.0 if record.model_parameters is None else 6.5 + min(4, record.model_parameters / 10_000_000)
            tooltip = (
                f"{record.relative_path} | {record.mode} | {elapsed:.2f}s | "
                f"{peak:.2f} GiB | steps={record.processed_steps}"
            )
            body.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{radius:.1f}" '
                f'fill="{mode_color(record.mode)}" opacity="0.62" stroke="#FFFFFF" stroke-width="1">'
                f'<title>{escape(tooltip)}</title></circle>'
            )
        body.append(svg_text(panel_x + panel_width / 2, panel_y + panel_height + 43, "elapsed seconds (log scale)", size=10, anchor="middle"))
    body.append(svg_text(18, panel_y + panel_height / 2, "cgroup memory peak (GiB)", size=11, anchor="middle", extra=f'transform="rotate(-90 18 {panel_y + panel_height / 2:.1f})"'))
    legend_x = 1140
    for index, mode in enumerate(("baseline", "sequential", "srcg", "fixed", "unknown")):
        x = legend_x + index * 82
        body.append(f'<circle cx="{x}" cy="600" r="6" fill="{mode_color(mode)}"/>')
        body.append(svg_text(x + 10, 604, mode, size=9))
    write_svg(output, width, height, "All completed SRCG training logs", body)


def write_training_csv(path: Path, records: list[TrainingRecord]) -> None:
    fields = list(TrainingRecord.__dataclass_fields__)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for record in records:
            row = record.__dict__.copy()
            row["path"] = str(record.path)
            row["run_directory"] = str(record.run_directory)
            writer.writerow(row)


def write_summary_csv(path: Path, records: list[SummaryRecord]) -> None:
    fields = list(SummaryRecord.__dataclass_fields__)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for record in records:
            row = record.__dict__.copy()
            row["path"] = str(record.path)
            row["run_directory"] = str(record.run_directory)
            writer.writerow(row)


def format_canonical_table(groups: list[tuple[str, list[dict[str, Any]]]]) -> str:
    lines = [
        "| Scenario | Mode | Trials | Completed jobs/trial | Time (s) | Parent peak (GiB) |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for scenario, items in groups:
        for item in items:
            lines.append(
                f'| {scenario} | {item["label"]} | {item["trials"]} | '
                f'{item["completed"]:.1f}/{item["jobs"]} | {item["makespan"]:.3f} | '
                f'{item["peak"]:.3f} |'
            )
    return "\n".join(lines)


def write_report(
    path: Path,
    training: list[TrainingRecord],
    summaries: list[SummaryRecord],
) -> None:
    model_sizes = sorted(
        {
            (record.model, record.model_parameters)
            for record in training
            if record.model_parameters is not None and not record.invalid
        },
        key=lambda value: value[1] or 0,
        reverse=True,
    )
    groups = canonical_groups(summaries)
    glossary_rows = chr(10).join(
        f"| `{term}` | {definition} |" for term, definition in GLOSSARY
    )
    content = f"""# รายงานกราฟผลทดลอง SRCG

> อ่านหัวข้อ **คำศัพท์และ Definition** ก่อนดูตัวเลขหรือกราฟ ทุกคำเทคนิคบนกราฟ
> ใช้ความหมายตามตารางนี้ ไม่ใช่ความหมายที่ผู้อ่านต้องเดาเอง

## 1. คำศัพท์และ Definition

| คำที่ใช้ | Definition ในรายงานนี้ |
|---|---|
{glossary_rows}

## 2. ขอบเขตข้อมูลที่อ่านได้

ตัวสร้างกราฟอ่าน raw files ใต้ `artifacts/` และพบ:

- `{len(summaries)}` scenario/attempt summaries
- `{sum(record.invalid for record in summaries)}` explicitly invalid summaries
- `{len(training)}` parseable training logs
- `{sum(record.elapsed_seconds is not None for record in training)}` logs with `performance_summary`
- `{sum(record.invalid for record in training)}` training logs below an `INVALID.txt`

## 3. กราฟ

1. [คำศัพท์และวิธีอ่านกราฟ](00-how-to-read.svg)
2. [Model size and GPU memory](01-model-size-and-gpu-memory.svg)
3. [Canonical comparable scenarios](02-canonical-comparisons.svg)
4. [Every scenario summary](03-all-scenario-summaries.svg)
5. [Milestone 3 optimization history](04-optimization-history.svg)
6. [Every completed training log](05-all-training-jobs.svg)

ตารางที่โปรแกรมอื่นหรือ spreadsheet อ่านต่อได้:

- [scenario-summaries.csv](scenario-summaries.csv)
- [training-jobs.csv](training-jobs.csv)

## 4. ขนาดโมเดล

| Workload model | Parameters | FP32 weight bytes only |
|---|---:|---:|
{chr(10).join(f'| `{model}` | {parameters:,} | {parameters * 4 / MIB:.1f} MiB |' for model, parameters in model_sizes)}

ค่า FP32 ในตารางนับเฉพาะ parameter tensors ไม่รวม gradient, optimizer state,
CUDA allocator cache, activation, decoded image/volume, collated batch,
DataLoader workers และ filesystem cache ดังนั้น Task06_Lung ใช้ host RAM สูงจาก
full NIfTI decode/transform ไม่ใช่เพราะ model parameters มากกว่า

## 5. ตัวเลขจาก canonical comparison

{format_canonical_table(groups)}

## 6. กฎการอ่านและข้อจำกัด

- A failed run's makespan is **time-to-failure**, not successful time-to-result.
- `INVALID.txt` wins over any partial metric in that directory. Invalid rows are
  retained in the all-results graph and CSV, but excluded from canonical means.
- The canonical chart intentionally excludes smoke tests, partial datasets,
  recovery calibration, superseded admission algorithms, and known-invalid
  runs.
- Early prototype logs did not record `model` or `model_parameters`; they appear
  as `not_logged`. The generator does not guess their architecture size.
- Direct host-scope logs can include memory charged by unrelated processes in
  their cgroup. They remain in the full inventory but are not interchangeable
  with delegated job-cgroup peaks.
- Existing PyTorch baselines use the same workload/hyperparameters and mostly
  conservative DataLoader settings. They are not proof against every manually
  optimized static scheduler or sequential policy.
- The normal-pressure timing pair has one recorded trial per mode. Safety pairs
  have two trials, but the complete evaluation still needs more repetitions and
  randomized order before thesis-level statistical claims.
"""
    path.write_text(content, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts", type=Path, default=Path("artifacts"))
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/analysis/all-experiments"),
    )
    args = parser.parse_args()
    artifacts_root = args.artifacts.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)

    training = [
        record
        for path in sorted(artifacts_root.rglob("*.jsonl"))
        if (record := parse_training(path, artifacts_root)) is not None
    ]
    training_by_directory: dict[Path, list[TrainingRecord]] = {}
    for record in training:
        training_by_directory.setdefault(record.run_directory, []).append(record)
    summaries = [
        record
        for path in sorted(artifacts_root.rglob("summary.txt"))
        if (record := parse_summary(path, artifacts_root, training_by_directory))
        is not None
    ]

    write_training_csv(output / "training-jobs.csv", training)
    write_summary_csv(output / "scenario-summaries.csv", summaries)
    graph_glossary(output / "00-how-to-read.svg")
    graph_model_sizes(training, output / "01-model-size-and-gpu-memory.svg")
    graph_canonical(summaries, output / "02-canonical-comparisons.svg")
    graph_all_summaries(summaries, output / "03-all-scenario-summaries.svg")
    graph_optimization(summaries, output / "04-optimization-history.svg")
    graph_training_scatter(training, output / "05-all-training-jobs.svg")
    write_report(output / "report.md", training, summaries)

    print(f"training_logs={len(training)}")
    print(f"scenario_summaries={len(summaries)}")
    print(f"output={output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
