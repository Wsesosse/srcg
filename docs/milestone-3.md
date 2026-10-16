# SRCG Milestone 3: PyTorch batch-aware Reserved Memory

Milestone 3 เชื่อม training loop เข้ากับ Reserved Memory จริงครบเส้นทาง:

```text
DataLoader มอบ batch indices ให้ main process หรือ worker
        ↓ ReservedBatchDataset อ่าน metadata และ estimate extra peak
worker ขอ RESERVE_RAM ก่อนเรียก Dataset.__getitem__
        ↓ Unix-domain socket
srcg serve ตรวจ global Reserved Memory + host headroom
        ↓ เขียน job memory.max สำเร็จก่อน
GRANTED(token, effective_limit)
        ↓
load/collate batch → worker SHRINK token → ส่งมากับ prefetched batch
        ↓ forward/backward → optimizer.step → ลบ reference
RELEASE_RAM(token)
        ↓
daemon อ่าน memory.current แล้ว RELEASED หรือ PENDING
```

ผู้ใช้ยังตั้งค่าการ train แบบปกติ เช่น batch size, epoch, optimizer, learning
rate และ workers เท่านั้น ขนาดที่ขอยืมคำนวณจาก metadata ของ batch อัตโนมัติ
`--policy-debug` มีไว้ดูแผน ไม่จำเป็นต้องเปิดเพื่อใช้ reservation

## สิ่งที่ implement แล้ว

- `srcg serve`: foreground reservation service ที่แชร์ accounting ระหว่าง jobs
- Unix-domain-socket protocol: `REGISTER`, `RESERVE`, `SHRINK`, `CANCEL`, `RELEASE`,
  `RETRY`, `RECLAIM` และ peer credentials
- ตรวจว่า Python peer อยู่ใน cgroup ของ job ที่อ้างก่อน reserve/release
- `srcg run --manager-socket`: register base quota และส่ง socket/job ID ผ่าน
  environment โดยไม่ปน telemetry กับ stdout ของ workload
- `srcg_torch.ReservationClient`: WAIT แบบ retry ด้วย request ID เดิม; default
  รอจน capacity ว่าง (`timeout=0`) และตั้ง deadline ได้เมื่อ policy ต้องการ
- pending admission ใช้ FIFO ข้ามทุก jobs; request ใหม่แซง request ที่รอก่อนไม่ได้
  และ client ส่ง `CANCEL` เมื่อ timeout เพื่อไม่ทิ้งหัวคิวค้าง
- current-aware admission แยก estimated allocation ออกจาก borrowed capacity,
  ใช้พื้นที่ว่างใน effective limit ก่อน และหัก `inactive_file` ออกจาก working set
  ที่ Reserved Memory ต้องปกป้อง
- reservation token และ manager log เก็บทั้ง `estimated_bytes` กับ
  `borrowed_bytes`; token แบบ zero-borrow ไม่เขียน `memory.max` และ release ทันที
- normal mode ใช้ standard DataLoader batching/shuffle โดยตรง
- `ReservedBatchDataset.__getitems__`: ขอ token ภายใน worker ก่อน decode/collate
  และส่ง token กลับพร้อม prefetched batch
- รองรับ reservation เมื่อ `num_workers > 0`; แต่ละ in-flight batch ถือ token
  ของตัวเอง และ manager นับ remaining estimates เพื่อไม่ใช้ base headroom ซ้ำ
- worker ส่ง SHRINK หลังทิ้ง temporary decode/preprocess tensors; ก่อน optimizer
  step แรกเสร็จจะถือ startup allowance สำหรับ lazy framework allocations
- RELEASE/RETRY รักษา quota ของ token อื่นที่ยัง allocate ไม่ครบ ไม่ดึง
  `memory.max` กลับกลางการโหลด
- `PlanningBatchSampler` เหลือสำหรับ plan debug และ exact checkpoint cursor
- release หลัง optimizer step และหลังลบ batch references; daemon เป็นผู้ตัดสินว่า
  ลด `memory.max` ได้จริงหรือยัง
- telemetry: grant/release/wait/admission time, throughput, load/train latency,
  cgroup peak, process RSS และ CUDA allocated/reserved peak
- stale-account reconciliation เมื่อ `srcg run` รอบใหม่พบ account จาก supervisor
  เดิม แต่จะไม่ reclaim job cgroup ที่ยัง populated

โหมดภายใน default เป็น `auto`: `num_workers=0` ใช้ legacy token lifetime;
`num_workers>0` ใช้ worker-side SHRINK ผู้ใช้ไม่ต้องเลือก phase mode เอง

## Budget ที่ใช้ใน full concurrent experiment

ตัวเลขนี้เป็น calibration ของรอบทดสอบ ไม่ใช่ default ที่เหมาะกับทุกเครื่อง:

```text
Transient test scope = 6 GiB

Host headroom         = อยู่นอก SRCG parent
SRCG parent           = 5 GiB
├── Lung job A base   = 1660 MiB
├── Lung job B base   = 1660 MiB
└── Reserved Memory   = 1800 MiB
```

รอบ no-admission ใช้ parent 5 GiB เดียวกันและให้ child ceiling เท่ากับ parent
เพื่อไม่ให้มี per-job limit มากำหนดผล ส่วนรอบ SRCG เพิ่ม/ลดเฉพาะ estimate ของ
NIfTI batch จริง

## ผลทดลองจริงบนเครื่องนี้

Environment: Arch Linux kernel 7.2.2, Python 3.14.7, PyTorch 2.13.0+cu130,
NVIDIA driver 610.57.04, RTX 4050 Laptop GPU 6141 MiB และ seed 42 รอบ
full-workload ใช้ DataLoader defaults (`num_workers=0`, pin memory ปิด) และ
swap disabled ภายใน transient cgroup รายละเอียด
ครบอยู่ที่ `artifacts/milestone3/environment.json`

default workload ปัจจุบันเป็น ResNet-50 สำหรับ COCO และ 3D U-Net base 32 สำหรับ
Lung Tiny models, `--limit` และ `--max-steps` สงวนไว้สำหรับ correctness test
และห้ามนำผลไปปนกับ experiment

| Workload / mode | ข้อมูลที่รัน | Throughput | เวลา | Process max RSS | CUDA reserved peak | Grants/WAIT |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| COCO normal | 5000 images / 625 steps | 91.762 img/s | 54.489 s | 1359.77 MiB | 1062 MiB | 0/0 |
| Lung normal | 63 volumes / 63 steps | 0.859 vol/s | 73.323 s | 2783.85 MiB | 1850 MiB | 0/0 |
| Lung SRCG | 63 volumes / 63 steps | 0.869 vol/s | 72.489 s | 2785.95 MiB | 1850 MiB | 63/0 |

single Lung normal/SRCG ให้ final model hash เดียวกัน SRCG grant/release สำเร็จ
63/63 และ admission รวม 29.278 ms

### Full concurrent experiment

Task06_Lung สอง jobs รันพร้อมกันด้วย dataset/model/config เดียวกัน:

| Run | Completed | Makespan | Exact parent peak | WAIT รวม |
| --- | ---: | ---: | ---: | ---: |
| Normal `repro-normal-1` | 2 jobs, 126 steps | 80.672 s | 4.227 GiB | 0 |
| Normal `repro-normal-2` | 2 jobs, 126 steps | 79.830 s | 4.227 GiB | 0 |
| SRCG FIFO trial 1 | 2 jobs, 126 steps | 103.610 s | 3.223 GiB | 29.898 s |
| SRCG FIFO trial 2 | 2 jobs, 126 steps | 99.902 s | 3.225 GiB | 29.848 s |

ผลนี้ไม่ใช่ผลบวกที่เลือกมาเฉพาะด้าน: ที่ parent 5 GiB no-admission ไม่ OOM
อยู่แล้ว ค่าเฉลี่ยสองรอบต่อ mode แสดงว่า SRCG หลังแก้ FIFO ลด peak 23.73% แต่ทำให้
makespan เพิ่ม 26.80% จึงชี้ว่า estimator/admission ปัจจุบัน conservative
เกินไปสำหรับ capacity นี้ ต้องเพิ่ม normal repeats และทำ capacity/concurrency
sweep ก่อนใช้เป็น thesis conclusion

full run ก่อนแก้ (`repro-srcg-1`) เจอ starvation: job หนึ่ง timeout ที่ step 31
เพราะ job อีกตัวคืนแล้วขอยืมซ้ำแซงผู้รอ รอบนั้นนำไปสู่ FIFO queue + `CANCEL`
สอง full trials หลังแก้จบครบ 126/126 steps, grant/release 126/126 ต่อรอบ และไม่มี
timeout/OOM จึงถือว่า starvation regression ถูกแก้แล้ว

raw data และรายละเอียดอยู่ที่ `artifacts/milestone3/real-workloads/RESULTS.md`

### Phase 1: แยกสาเหตุของ conservative admission

เพิ่ม `--memory-trace` สำหรับ experiment เท่านั้น มันบันทึก `memory.current`
พร้อม `memory.stat` ที่ boundary ของ batch โดยไม่เปลี่ยน FIFO, quota, token หรือ
training object lifetime ค่า `anon` หมายถึง heap/tensor ของ process ส่วน `file`
หมายถึง filesystem cache ที่ kernel charge ให้ cgroup

full Lung trace รอบ `phase1-memory-trace-2` พบทั้งสอง job มี `anon` เท่ากันประมาณ
915.96 MiB แต่ `file` ต่างกัน 12.08 กับ 690.16 MiB งานที่ถือ file charge คืน
token 615 MiB ไม่ผ่าน safe-release; pool จึงขาด 32.5 MiB สำหรับ request ถัดไป
และทั้งสองงาน timeout ที่ 28/63 steps โดยไม่มี OOM รอบนี้เป็น liveness diagnostic
ที่ valid แต่ไม่ใช่ performance result

หลักฐานนี้ทำให้ Phase 2 เลือกทดลอง current-aware borrow ก่อน `SHRINK`: manager
จะประเมินเฉพาะส่วนของ estimated allocation ที่เกิน headroom ใน effective limit
ปัจจุบัน ห้ามให้ Python คำนวณเองและยังไม่เปลี่ยน code ใน Phase 1

### Phase 2: current-aware และ inactive-file-aware admission

Phase 2 ใช้สูตร:

```text
protected current = memory.current - inactive_file
borrow needed      = max(0, protected current + estimate - effective limit)
```

`inactive_file` คือ file-backed pages บน inactive list ของ kernel ไม่ใช่ tensor
และไม่ใช่ RAM ที่ SRCG ลบทิ้งเอง มันยังอยู่ใต้ hard `memory.max`; เมื่อ working set
ใหม่ต้องการพื้นที่ kernel เป็นผู้ reclaim cache ตามปกติ Host headroom ยังคงตรวจ
estimate เต็มก้อน ไม่ใช่ borrow ที่ลดแล้ว

รุ่นแรกที่หักเพียง headroom แต่ยังใช้ total `memory.current` fail ที่ 28/63 +
21/63 steps เพราะ pending tokens สะสม artifact อยู่ใน `phase2-current-aware-1/`
หลังหัก `inactive_file` full trace จบ 126/126 และ no-trace paired trials ให้ผล:

| Mode | Trials ที่จบ | Mean makespan | Mean exact parent peak | WAIT/PENDING/OOM |
| --- | ---: | ---: | ---: | ---: |
| Normal | 2/2 | 93.103 s | 5.000 GiB | 0/0/0 |
| SRCG Phase 2 | 2/2 | 91.640 s | 4.599 GiB | 0/0/0 |

ในสอง trials นี้ Phase 2 ลด parent peak 8.01% และ makespan ต่างจาก normal -1.57%
แต่ sample size ยังเล็กและ normal แตะ parent limit 5 GiB ทั้งสองรอบ จึงใช้เป็น
regression result ได้ แต่ยังห้ามอ้างเป็นข้อสรุป performance ของ thesis

### Phase 3: multi-worker token lifetime และ safe RELEASE

การทดลอง `num_workers=2`, `prefetch_factor=2` พบ races ที่ `memory.current`
snapshot อย่างเดียวมองไม่เห็น: workers หลายตัว reserve ก่อน allocation ปรากฏ,
worker อาจคืน quota ขณะ worker อื่นยังโหลด และ optimizer step แรกสร้าง host state
แบบ lazy Phase 3 จึงเพิ่ม:

- remaining estimate ต่อ token และรวมทุก in-flight token ตอน admission
- worker-side SHRINK หลัง collate, NIfTI/JPEG estimate ที่รวม compressed file cache
- admission margin 64 MiB + 32 MiB/worker และ startup allowance 256 MiB
- RELEASE/RETRY ที่รักษา commitments ของ token อื่น
- RELEASE คืน capacity บางส่วนที่ลดได้อย่างปลอดภัยแทนการถือ token เต็มก้อนเมื่อ
  ยังคืนทั้งหมดไม่ได้ และ RESERVE polling ตรวจ pending releases อัตโนมัติ
- WAIT แบบไม่มี deadline โดย default เพื่อให้ job รอจนอีก job จบและคืน base quota

full result ใช้ Lung สอง jobs, สอง workers/job ภายใต้ parent 5 GiB และมี
same-boot normal control ที่ไม่ต่อ reservation manager:

| Mode | Completed | Wall time | Parent peak | OOM / OOM-kill | Quality gate |
| --- | ---: | ---: | ---: | ---: | --- |
| Normal `workers2-normal-5g-1` | 69/126 steps, 1/2 jobs | 42.606 s (incomplete) | 5,368,709,120 B | 1 / 5 | survivor exact |
| SRCG `workers2-release-wait-5g-1` | 126/126 steps, 2/2 jobs | 92.956 s | 4,818,538,496 B | 0 / 0 | both exact |

SRCG ลด parent peak 550,170,624 bytes หรือ 10.25% และเปลี่ยนผลจาก job สำเร็จ
1/2 เป็น 2/2 Normal job A ถูกฆ่าที่ step 6 ส่วน job B จบ 63 steps ค่า loss และ
model hash ของ normal survivor กับ SRCG ตรงกันแบบ exact

เวลา 42.606 s ของ normal ไม่ใช่ completed makespan และห้ามใช้คำนวณ speedup เพราะ
งานหายไป 57 steps เวลาในตารางเป็น same-boot safety/regression result ไม่ใช่
sequential comparison; กติกาของ projectกำหนดว่าการเปรียบเทียบ sequential ต้อง
เริ่มหลัง reboot แยกกัน

parent 4 GiB ไม่พอสำหรับ scenario เดียวกันเมื่อ workers สองตัวเพิ่ม baseline:
ทั้งสอง jobs อาจ WAIT โดยไม่มี active token ให้คืน การอนุมัติฝืนจะ OOM ดังนั้น
configuration นี้ต้องเพิ่ม capacity, ลด workers หรือเพิ่ม job-level pause/checkpoint
orchestration; SHRINK ไม่สามารถสร้าง RAM ที่ไม่มีอยู่จริงได้

cold high-pressure test ภายหลังพบ liveness edge case เพิ่มเติม: tokens สองตัวถูก
RELEASE เป็น PENDING แล้ว workers ทั้งหมดเข้า RESERVE wait loop จึงไม่มี explicit
RETRY เกิดขึ้น pool ค้างประมาณ 1.20 GB และ jobs หยุดที่ step 8 หลังแก้ partial
release + automatic retry warm full validation จบ 126/126 steps ใน 95.434 s,
parent peak 4.422 GiB, OOM 0 และ model/loss ตรง baseline แบบ exact cold timing
ต้องเก็บใหม่หลัง reboot

attempt ถัดมาชี้ว่า outer scope 6 GiB ไม่พอสำหรับ parent 5 GiB + headroom 1 GiB
+ process overhead ของ manager/controller ไม่ใช่ข้อจำกัดของ training parent
จึงแก้ experiment scope เป็น 7 GiB โดยคง parent 5 GiB warm validation ของ topology
นี้จบ 126/126 steps ใน 86.201 s, parent peak 4.665 GiB และ OOM 0

### Safety evaluation: จุดที่ normal ล้มแต่ SRCG ทำงานจบ

หลังผู้ใช้กำหนด thesis objective ให้ชัด การผ่าน/ไม่ผ่านวัดจาก completed jobs และ
OOM kills เป็นหลัก เวลาเป็นต้นทุนที่ยอมรับได้ถ้ามันแลกกับผล training ที่เสร็จจริง

ที่ training parent 4 GiB สอง full Lung jobs แบบ normal ถูก cgroup OOM-kill ทั้ง
สองงานใน 2/2 trials แต่ SRCG ทำครบ 126/126 steps และไม่มี OOM ใน 2/2 trials
ค่าเฉลี่ย SRCG makespan 91.578 s, parent peak 3.601 GiB และ WAIT รวม 11.821 s

เมื่อเพิ่ม process นอก SRCG parent ซึ่งขอ 512 MiB หลังเริ่ม train 5 วินาที normal
ที่ตั้ง headroom 0 ถูก OOM-kill ทั้งสอง training jobs ใน 2/2 trials ส่วน SRCG ที่มี
parent 4 GiB และ ancestor-aware headroom 1 GiB ใต้ outer limit 5 GiB ทำสำเร็จครบ
ทั้ง training สองงานและ process ใหม่ใน 2/2 trials โดยไม่มี OOM ค่าเฉลี่ย makespan
111.674 s, outer peak 3.699 GiB และ WAIT รวม 52.814 s

flow, นิยาม metric, raw artifact paths และคำสั่งรันซ้ำอยู่ที่
[safety-evaluation.md](safety-evaluation.md)

## Oversized-request-before-load test

ตั้งแต่ Phase 2 estimate ที่ใหญ่กว่า Reserved Memory ยัง grant ได้ถ้า existing
job headroom ทำให้ `borrow needed` พอดีกับ pool ระบบตอบ `DENIED
reason=estimated_allocation_exceeds_job_capacity` ทันทีเฉพาะเมื่อ estimate ใหญ่กว่า
`base quota + Reserved Memory ทั้งก้อน` ซึ่งไม่มีทางรองรับได้แม้ current เป็นศูนย์
ส่วน request ที่มีโอกาส fit แต่ capacity กำลังถูกใช้จะ WAIT โดยยังไม่ load ข้อมูล

```text
DENIED reason=estimated_allocation_exceeds_job_capacity
```

artifact ใน `artifacts/milestone3/wait-before-load/` เป็นพฤติกรรมก่อนเพิ่ม FIFO
และก่อน Phase 2 จึงเก็บไว้เป็น historical behavior; invariant ที่ยังเหมือนเดิมคือ
WAIT/DENIED ไม่ถูกแปลเป็น permission ให้โหลด volume ต่อ

## วิธีรันบน parent ปกติ

Initialize parent ครั้งเดียว:

```bash
sudo ./build-srcg/srcg init \
  --parent /sys/fs/cgroup/srcg \
  --parent-memory 5GiB \
  --parent-swap 0 \
  --host-headroom 1GiB
```

Terminal แรกเปิด shared manager:

```bash
sudo ./build-srcg/srcg serve \
  --parent /sys/fs/cgroup/srcg \
  --socket /run/srcg-manager.sock \
  --reserved-memory 2GiB \
  --checkpoint-budget 512MiB \
  --host-headroom 1GiB \
  --log srcg-logs/reservations.log
```

Terminal ที่สองรัน training โดยไม่ต้องป้อน burst pattern:

```fish
set py (pwd)/.venv/bin/python

sudo ./build-srcg/srcg run \
  --parent /sys/fs/cgroup/srcg \
  --manager-socket /run/srcg-manager.sock \
  --job lung-a \
  --memory 2GiB \
  --swap 0 \
  --log srcg-logs/lung-a.log \
  -- \
  $py benchmarks/dataloader_policy/train.py lung \
  --batch-size 1 \
  --epochs 10 \
  --optimizer adamw \
  --lr 0.0001 \
  --device cuda
```

เมื่อ `--manager-socket` ถูกส่งให้ `srcg run`, `train.py` โหมด `auto` จะเปิด
reservation เอง ใช้ `--srcg-reservation off` เมื่อต้องการรัน comparison แบบ
fixed quota และใช้ `--policy-debug` เมื่อต้องการเห็น `batch_plan`

สำหรับ training script อื่น ติดตั้ง package ด้วย
`.venv/bin/python -m pip install -e . --no-build-isolation` แล้วเชื่อมตรง boundary
ที่รู้ batch indices ก่อน load:

```python
from srcg_torch import ReservationClient

srcg = ReservationClient.from_environment()
assert srcg is not None

token = srcg.reserve(estimated_additional_bytes)  # WAIT อยู่ตรงนี้; ยังไม่ load
batch = None
try:
    batch = load_planned_batch(indices)
    train_one_batch(batch)
finally:
    del batch
    result = srcg.release(token)  # อาจเป็น RELEASED หรือ PENDING
```

ตัว client ไม่อนุมัติ resource เองและ user ไม่ต้องเห็น token ใน CLI; authority
อยู่ใน `srcg serve` ส่วน workload ต้องมี estimator/metadata ที่คำนวณ batch ถัดไปได้
ก่อนเรียก loader

## Tests และ raw logs

```bash
cmake --build build-srcg --parallel
ctest --test-dir build-srcg --output-on-failure
.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
.venv/bin/python -m unittest benchmarks.dataloader_policy.test_planning -v
```

- C++ tests: 6 targets; 4 unit targets pass ใน sandbox, socket/cgroup integration
  ถูก skip เพราะ sandbox ปิด facility นั้น
- Unix-socket C++ และ Python tests ผ่านเมื่อรันนอก sandbox
- delegated real-cgroup multi-worker tests ผ่านทั้ง COCO และ Lung
- full performance logs: `artifacts/milestone3/real-workloads/`
- Phase 1 anon/file trace: `artifacts/milestone3/real-workloads/phase1-memory-trace-2/`
- Phase 2 failed total-current trace: `artifacts/milestone3/real-workloads/phase2-current-aware-1/`
- Phase 2 successful trace: `artifacts/milestone3/real-workloads/phase2-cache-aware-1/`
- Phase 2 no-trace comparisons: `phase2-{normal,cache-aware-no-trace}-{1,2}/`
- current capacity safety: normal `4g-normal-{1,2}` และ SRCG
  `ancestor-aware-srcg-{1,2}` ใต้ `artifacts/milestone3/capacity-sweep/`
- current late-arriving safety: `artifacts/milestone3/late-arrival/ancestor-aware-{normal,srcg}-{1,2}/`
- pre-ancestor-aware historical comparison: `artifacts/milestone3/late-arrival/{unprotected-normal,protected-srcg}-{1,2}/`
- historical smoke/calibration logs: `artifacts/milestone3/m3-comparison-final/`
- full dataset plans: `artifacts/milestone3/coco-full-plan.jsonl` และ
  `artifacts/milestone3/lung-full-plan.jsonl`

## ข้อจำกัดที่ยังเหลือ

- worker ที่ตายหลังรับ grant แต่ก่อนส่ง token กลับ main process อาจทำให้ token
  ค้างจน job จบและถูก `RECLAIM`; ยังไม่มี per-worker crash reclamation
- `--policy-debug` ยังบังคับ `num_workers=0` เพื่อให้ debug plan เรียงลำดับ แต่
  normal reservation path รองรับ multi-worker/prefetch แล้ว
- service เป็น foreground process ยังไม่มี systemd unit/config file
- state ยังไม่ durable ข้าม manager crash; rerun reconcile stale account ได้เมื่อ
  manager เดิมยังอยู่ แต่ manager restart/re-journal เป็นงานถัดไป
- fixed jobs ที่ไม่ register กับ manager จะไม่อยู่ใน base-quota accounting ดังนั้น
  production parent เดียวกันต้องให้ทุก borrowing job ใช้ `--manager-socket`
- estimator เป็น header-based + safety factor ต้อง calibration หลายรอบก่อนใช้เป็น
  admission ที่เข้มงวดใน thesis experiment
- periodic checkpoint และ checkpoint-budget admission มีแล้ว ดู
  [periodic-checkpoint.md](periodic-checkpoint.md); ยังไม่ได้ทดสอบ recovery จาก
  CUDA OOM จริงหรือการกู้ข้าม reboot แบบครบคู่ จึงยังไม่ถือว่า Milestone 4 เสร็จ
