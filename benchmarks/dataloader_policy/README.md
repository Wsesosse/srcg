# Full-dataset training experiments

ตัวอย่างการครอบ training program ด้วย `srcg run` รวมทั้ง lifecycle, rerun, signal
และ OOM อยู่ที่ [../../docs/run-scenarios.md](../../docs/run-scenarios.md)

`train.py` เป็น training program หลักที่ใช้ข้อมูลจริงสองแบบ ไม่ใช่ smoke runner:

- `coco`: อ่าน COCO `val2017` JPEG ที่ขนาดเดิม, resize และ train ResNet-50
- `lung`: อ่าน Task06_Lung CT และ segmentation mask ทั้ง NIfTI volume แล้ว
  resize และ train 3D U-Net

ขนาด volume ของ Lung ต่างกันตามข้อมูลจริง จึงเกิด RAM spike ตามธรรมชาติ
โปรแกรมไม่สร้าง tensor ปลอมและไม่จัดลำดับ sample เพื่อบังคับ burst pattern

บทบาทในการทดลองต่างกัน: COCO เป็น steady/control workload สำหรับวัด JPEG resize
และ integration overhead (peak RSS บนเครื่องทดสอบประมาณ 1.36 GiB) ส่วน Lung เป็น
memory-stress workload สำหรับวัด orchestration ตอนหลาย jobs ชนกัน COCO ตัวเดียว
จึงไม่หนักพอใช้พิสูจน์ว่า admission ป้องกัน memory contention ได้

## ติดตั้ง dependency

```bash
.venv/bin/python -m pip install -r benchmarks/dataloader_policy/requirements.txt
```

โครงสร้าง dataset:

```text
ML-Test-Dataset/
├── val2017/*.jpg
└── Task06_Lung/
    ├── imagesTr/*.nii.gz
    └── labelsTr/*.nii.gz
```

ไฟล์ AppleDouble ที่ชื่อขึ้นต้นด้วย `._` จะถูกข้าม

## Training ปกติ

คำสั่ง experiment ห้ามใส่ `--limit`, `--max-steps` หรือ `--model tiny` เพราะ
สามตัวนั้นมีไว้ตรวจ correctness เท่านั้น

COCO ทั้ง 5,000 ภาพ:

```bash
.venv/bin/python benchmarks/dataloader_policy/train.py coco \
  --epochs 1 \
  --batch-size 8 \
  --optimizer sgd \
  --learning-rate 0.01 \
  --device cuda
```

Task06_Lung ทั้ง 63 image/label volumes:

```bash
.venv/bin/python benchmarks/dataloader_policy/train.py lung \
  --epochs 1 \
  --batch-size 1 \
  --optimizer adamw \
  --learning-rate 0.0001 \
  --device cuda
```

ตัวเลือก training ปกติ:

| Option | ความหมาย |
| --- | --- |
| `--batch-size` | จำนวน sample ต่อ batch |
| `--epochs` | จำนวนรอบ training |
| `--optimizer` | `sgd`, `adam` หรือ `adamw` |
| `--learning-rate`, `--lr` | learning rate |
| `--weight-decay` | weight decay ของ optimizer |
| `--momentum` | momentum เมื่อใช้ SGD |
| `--device` | `auto`, `cpu`, `cuda` หรือ `cuda:N` |
| `--num-workers` | จำนวน DataLoader workers |
| `--pin-memory` | เปิด pinned-memory DataLoader path |
| `--shuffle` / `--no-shuffle` | เปิดหรือปิด seeded shuffle |
| `--prefetch-factor` | จำนวน batch ที่ worker แต่ละตัว prefetch |
| `--persistent-workers` | ไม่ปิด workers ระหว่าง epochs |

ค่าเริ่มต้นของ COCO คือ ResNet-50, batch 8, SGD, LR 0.01 และ resize 224×224
ส่วน Lung คือ 3D U-Net base width 32, batch 1, AdamW, LR 0.0001 และ output
volume 32×128×128 ทั้งสอง workload shuffle ตาม seed 42 โดย default

COCO image directory ไม่มี annotation ดังนั้น workload นี้ใช้ deterministic
pseudo-label เพื่อให้ optimizer step ทำงานครบ แต่ห้ามใช้ accuracy เป็นผลทดลอง
ด้าน model quality ส่วน Lung ใช้ segmentation mask จริง

## SRCG policy debug แบบ opt-in

training ปกติไม่ต้องกำหนด burst pattern ถ้าต้องการตรวจ reservation boundary ค่อย
เพิ่ม `--policy-debug`:

```bash
.venv/bin/python benchmarks/dataloader_policy/train.py lung \
  --epochs 1 \
  --batch-size 1 \
  --optimizer adamw \
  --lr 0.0001 \
  --num-workers 0 \
  --policy-debug
```

`--policy-debug` ใช้ PlanningBatchSampler เพื่อแสดง plan โดยไม่เปลี่ยนลำดับข้อมูล
และยังจำกัดที่ `num_workers=0` เพื่อให้ log อ่านตามลำดับง่าย

Reserved Memory mode ไม่ได้ใช้ข้อจำกัดนี้แล้ว `ReservedBatchDataset.__getitems__`
ขอ token ภายใน DataLoader worker ก่อน decode batch จริง token เดินทางกลับมากับ
prefetched batch และ training process release หลัง optimizer step จึงรองรับ
`num_workers > 0`, prefetch และ pin-memory path

ดูแผนโดยไม่โหลดข้อมูลและไม่ train:

```bash
.venv/bin/python benchmarks/dataloader_policy/train.py lung --plan-only --no-shuffle
```

`--limit N`, `--max-steps N` และ `--model tiny` ใช้ใน automated correctness
test เท่านั้น ผลจาก flags เหล่านี้ห้ามนำไปเป็นผล experiment

## Training กับ Reserved Memory

เปิด `srcg serve` หนึ่งตัวร่วมกันสำหรับหลาย jobs แล้วเพิ่ม
`--manager-socket` ที่ `srcg run` เท่านั้น ตัว training ใช้โหมด `auto` และไม่ต้อง
ตั้ง burst pattern เอง:

```bash
sudo ./build-srcg/srcg run \
  --parent /sys/fs/cgroup/srcg \
  --manager-socket /run/srcg-manager.sock \
  --job lung-a \
  --memory 2GiB \
  --swap 0 \
  --log srcg-logs/lung-a.log \
  -- \
  /absolute/path/.venv/bin/python benchmarks/dataloader_policy/train.py lung \
  --batch-size 1 \
  --epochs 1 \
  --optimizer adamw \
  --lr 0.0001 \
  --device cuda
```

ดูผล full-dataset และ concurrent jobs จริงที่
[../../artifacts/milestone3/real-workloads/RESULTS.md](../../artifacts/milestone3/real-workloads/RESULTS.md)

## รัน concurrent experiment ซ้ำ

runner นี้เปิด Task06_Lung 3D U-Net สอง jobs พร้อมกัน ใช้ข้อมูล 63 volumes ครบ
ทั้งคู่และเก็บ training/controller/manager logs แยกไฟล์ มันสร้าง delegated
transient cgroup เองจึงไม่แตะ `/sys/fs/cgroup/srcg` ที่ตั้งใช้งานถาวรไว้:

```bash
bash benchmarks/dataloader_policy/run_full_experiment.sh normal
bash benchmarks/dataloader_policy/run_full_experiment.sh srcg
```

คำสั่งแรกคือ no-admission control คำสั่งที่สองใช้ Reserved Memory ทั้งสองใช้
parent budget 5 GiB และ training hyperparameters เดียวกัน ไม่มี subset, max-step,
tiny model หรือ synthetic burst ผลแต่ละรอบอยู่ใต้
`artifacts/milestone3/real-workloads/manual-*`

ผล reproducible ล่าสุดหลังเพิ่ม FIFO admission อยู่ใน `repro-normal-{1,2}/` และ
`repro-srcg-fifo-{1,2}/`; ทั้งสอง SRCG trials จบครบ 126/126 steps

## Historical: สอง jobs แบบ sequential versus SRCG จาก cold boot

ส่วนนี้เป็นการทดลอง multi-job เก่า คำว่า `sequential` ในส่วนนี้หมายถึง job A จบ
แล้วจึงรัน job B จึง **ไม่ใช่** recovery baseline ที่ OOM แล้ว reboot/resume
งานเดียว การทดลอง recovery ที่ใช้นิยามหลังอยู่ในหัวข้อถัดไป

การเทียบเวลาระหว่างรันสอง jobs ทีละงานกับ SRCG ต้องไม่เอา page cache จาก run ก่อน
มาปน ใช้ wrapper นี้ทันทีหลัง reboot และเก็บได้หนึ่ง mode ต่อหนึ่ง boot:

```bash
# boot แรก: high-pressure normal PyTorch รันทีละ job
benchmarks/dataloader_policy/run_cold_boot_comparison.sh high-sequential

# reboot อีกครั้ง: high-pressure SRCG รันสอง job พร้อมกัน
benchmarks/dataloader_policy/run_cold_boot_comparison.sh high-srcg
```

wrapper ปฏิเสธ boot ที่เกิน 10 นาทีโดย default และปฏิเสธการเก็บ run ที่สองใน boot
เดียวกัน metadata รวม boot ID, boot age และ SHA-256 ของ binary/training/runner
อยู่ใน artifact ของแต่ละ run

ตรวจว่า orchestration ไม่เปลี่ยนผล training ด้วย exact gate:

```bash
.venv/bin/python benchmarks/dataloader_policy/verify_training_equivalence.py \
  --baseline BASELINE/lung-a.jsonl \
  --candidate CANDIDATE/lung-a.jsonl \
  --candidate CANDIDATE/lung-b.jsonl
```

gate ต้องเห็น training config, จำนวน samples/steps, loss ทุก step และ final model
SHA-256 ตรงกันทั้งหมด ค่า default ไม่ยอมให้ loss ต่างแม้แต่น้อย

## Single-job OOM → reboot → resume recovery experiment

นิยาม `sequential high pressure` ของการทดลองนี้คือ **training job เดียว** และ
attempts ของงานเดียวกันเรียงต่อกันข้าม boot:

```text
baseline attempt 1 → checkpoint → OOM → reboot
                   → resume attempt 2 → OOM → reboot
                   → ... → resume จน training ครบ 63 steps
```

มันไม่ได้หมายถึงรัน job A แล้ว job B ค่าที่ calibrate บนเครื่องนี้ใช้ Task06_Lung
ครบ 63 volumes, 3D U-Net base 32, batch size 1, DataLoader workers 4, prefetch 2,
checkpoint ทุก 5 steps, parent `3840 MiB` และ swap 0 ทั้งสอง modes ใช้ workload,
optimizer, seed และ checkpoint policy เดียวกัน

ภายใน SRCG ค่า `3840 MiB` ถูกแบ่งเป็น:

```text
base quota          1660 MiB
Reserved Memory     1924 MiB  (ยืมชั่วคราวตอน DataLoader สร้าง batch)
checkpoint budget    256 MiB  (กันออกจาก batch pool)
total parent limit  3840 MiB
```

checkpoint budget ปัจจุบันเป็น capacity แยกจาก batch reservation และ manager
เพิ่ม job `memory.max` ชั่วคราวก่อน save ผ่าน `CHECKPOINT_BEGIN` แล้วคืนเมื่อ
`CHECKPOINT_END` ลด limit ได้อย่างปลอดภัย รายละเอียดและผล X=0/5/10 อยู่ที่
`../../docs/periodic-checkpoint.md`

หลัง reboot เริ่ม baseline ด้วย run directory ใหม่หนึ่งครั้ง:

```bash
benchmarks/dataloader_policy/run_recovery_attempt.sh \
  baseline artifacts/milestone4/recovery/baseline-1
```

ถ้า exit 137 ให้ reboot แล้วเรียก **คำสั่งเดิมและ directory เดิม** มันจะอ่าน
`latest.json`, restore model + optimizer + sampler + random-number-generator state
แล้วทำงานต่อ script ปฏิเสธ attempt ที่สองจาก boot ID เดิมเพื่อไม่ให้เรียกซ้ำโดย
ไม่ได้ reboot ทำซ้ำจนบรรทัดสรุปแสดง `result=completed`

จากนั้น reboot อีกครั้งและเก็บ SRCG ใน directory คนละอัน:

```bash
benchmarks/dataloader_policy/run_recovery_attempt.sh \
  srcg artifacts/milestone4/recovery/srcg-1
```

สรุปคู่ผลด้วย:

```bash
.venv/bin/python benchmarks/dataloader_policy/summarize_recovery.py \
  artifacts/milestone4/recovery/baseline-1 \
  artifacts/milestone4/recovery/srcg-1
```

ตัวสรุปรายงานเวลาสองแบบ ห้ามใช้แทนกัน:

- `active_seconds`: ผลรวมเวลาที่ attempts รันจริง ไม่รวมเวลาปิด/เปิดเครื่อง
- `end_to_end_seconds`: จากเริ่ม attempt แรกถึงงานจบ รวม reboot และเวลาที่ผู้ใช้
  รอก่อนสั่ง attempt ถัดไป
- `between_attempt_seconds`: `end_to_end - active` จึงรวม reboot และ manual delay
- `oom_attempts`: จำนวน attempts ที่จบเพราะ cgroup OOM
- `oom_kills`: kernel counter ของจำนวน processes ที่ถูกฆ่า หนึ่ง attempt อาจฆ่า
  main process และ DataLoader workers หลายตัว ค่านี้จึงอาจมากกว่า attempts
- `lost_steps_after_latest_checkpoint`: steps ที่ทำเสร็จหลัง checkpoint ล่าสุดแต่
  ต้องทำใหม่หลัง OOM
- `exact_final_model_match`: SHA-256 ของ final model baseline และ SRCG ตรงกัน

Calibration วันที่ 2026-09-18 ยืนยันเฉพาะการเลือก pressure point ยังไม่ใช่ผลเวลา
thesis: baseline ไปถึง step 15, commit checkpoint แล้ว OOM (`exit 137`); SRCG ที่
boundary เดียวกันจบ 63/63 steps, OOM 0, parent peak 3,659,640,832 bytes และ final
model SHA-256 ตรงกับ completed ordinary-PyTorch reference แบบ exact

## Regenerate กราฟผลทดลองทั้งหมด

ตัวสร้างกราฟอ่าน raw artifacts ทุก `*.jsonl` และ `summary.txt` โดยไม่ต้องติดตั้ง
Matplotlib/Pandas มันเก็บ invalid และ development runs ไว้ใน inventory แต่ไม่นำ
ไปรวมกับ canonical means:

```bash
.venv/bin/python benchmarks/dataloader_policy/generate_experiment_graphs.py
```

ผลอยู่ใน `artifacts/analysis/all-experiments/` ประกอบด้วย SVG, CSV inventory และ
`report.md` กราฟ model size ใช้จำนวน parameters ที่ training program บันทึกจริง;
log รุ่นเก่าที่ไม่บันทึก architecture จะระบุ `not_logged` แทนการเดาขนาด

## E8 swap-capacity boundary (2026-10-20)

The [E8 report](../../artifacts/analysis/e8-swap-20261020-report.md) and
[per-run CSV](../../artifacts/analysis/e8-swap-20261020-runs.csv) compare two
concurrent Task06_Lung jobs under the same 8 GiB outer RAM, 6 GiB parent RAM,
and 0–2048 MiB outer/parent/job swap caps. The experiment preserves SRCG's
existing RAM admission and borrowing policy; it does not add swap management.

To make a new, uniquely numbered repetition from the repository root:

```bash
bash benchmarks/dataloader_policy/run_e8_swap.sh normal 512 4
bash benchmarks/dataloader_policy/run_e8_swap.sh srcg 512 4
```

The runner checks host swap before each attempt, records actual live kernel
limits and counters, and never modifies host swap. Raw output is in
`artifacts/e8-swap-20261020/` and intentionally Git-ignored. The report defines
every outcome and explains the single observed SRCG admission stall.
