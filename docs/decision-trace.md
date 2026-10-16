# SRCG decision trace: คำนี้มาจากไหนและมีสถานะอะไร

เอกสารนี้ไม่ใช่ glossary และไม่ใช่รายการ feature เป้าหมายของมันคือเชื่อมลำดับ

```text
ข้อกำหนดหรือปัญหา → หลักฐาน → การตัดสินใจ → สิ่งที่เปลี่ยน → สถานะปัจจุบัน
```

เพื่อไม่ให้คำใหม่ดูเหมือนโผล่มาเอง ทุกชื่อ design ใหม่ต้องมี status และ trace ใน
รูปแบบนี้ก่อนถือว่าเป็นส่วนหนึ่งของ SRCG

## ป้ายสถานะ

| ป้าย | ความหมาย |
| --- | --- |
| `[ORIGINAL PLAN]` | อยู่ใน `SRCG-Implementation plan.md` ตั้งแต่ก่อน implement |
| `[IMPLEMENTED]` | มี code และ test แล้ว |
| `[PROPOSED]` | เป็นแนวทางที่กำลังพิจารณา ยังเรียกใช้ไม่ได้ |
| `[HISTORICAL]` | เคยเกิดหรือเคยใช้ แต่ไม่ใช่ behavior ปัจจุบัน |
| `[OBSERVED]` | เป็นพฤติกรรมที่เห็นจาก artifact จริง แต่ยังไม่ใช่ feature หรือข้อสรุปทั่วไป |
| `[REJECTED]` | พิจารณาแล้วและตั้งใจไม่ใช้ |

## กฎเมื่อเพิ่มคำหรือกลไกใหม่

ก่อนเพิ่ม design keyword ใหม่ ต้องตอบให้ครบ:

1. **Definition:** คำนี้หมายถึงอะไรใน SRCG
2. **Origin:** มาจาก Implementation Plan, requirement ของผู้ใช้ หรือผลทดลองใด
3. **Problem/Evidence:** แก้ปัญหาอะไร และหลักฐานอยู่ไฟล์ไหน
4. **Decision:** เลือกทำอะไร เพราะอะไร
5. **Status:** implemented, proposed, historical หรือ rejected
6. **Trade-off:** เพิ่มความซับซ้อนหรือข้อเสียอะไร

หากยังไม่มี code ห้ามเขียนเหมือนเป็น API ที่มีแล้ว ต้องติด `[PROPOSED]` ทุกครั้ง

## D001 — Reserved Memory

**Status:** `[ORIGINAL PLAN] [IMPLEMENTED]`

**Definition:** RAM capacity ส่วนกลางภายใน SRCG parent ซึ่ง jobs ยืมชั่วคราวได้
มันเป็น accounting capacity ไม่ใช่ RAM ที่ kernel allocate ไว้ล่วงหน้า

**Origin:** Implementation Plan หัวข้อ 3 เรียก `batch_pool_capacity` และหัวข้อ 6
แยกมันจาก checkpoint budget ภายหลังเลือกชื่อ public เพียงชื่อเดียวว่า
`Reserved Memory` เพื่อไม่ให้ `batch pool` กับ `Reserved Memory` ดูเป็นคนละสิ่ง

**Decision:** source, CLI และเอกสารใหม่ใช้ `Reserved Memory`; คำว่า batch pool
ปรากฏได้เฉพาะเมื่ออ้างชื่อเดิมในแผน

**Trade-off:** เป็น hard accounting partition แบบ conservative capacity ที่ยังว่าง
จะไม่ถูก overcommit แม้การใช้ RAM จริงอาจต่ำกว่า limit

## D002 — Reservation token

**Status:** `[ORIGINAL PLAN] [IMPLEMENTED]`

**Definition:** identifier ของสิทธิ์ยืม Reserved Memory ที่ grant แล้ว เช่น
`rsv-14` ใช้ระบุ owner, request, bytes และ state ตอน release

**Origin:** Implementation Plan หัวข้อ Reserve/Release กำหนดให้ daemon สร้าง token
และห้ามคืน capacity จนลด `memory.max` ได้อย่างปลอดภัย

**Decision:** duplicate request คืน token เดิมและ duplicate release ไม่คืน capacity
ซ้ำ

**Trade-off:** token ที่ client ทำหายยังถือ capacity จนมี crash reclamation

## D003 — `WAIT` และ retry

**Status:** `[ORIGINAL PLAN] [IMPLEMENTED]`

**Definition:** request ถูกต้องแต่ยัง grant ไม่ได้ client ต้องไม่โหลด batch และส่ง
request ID เดิมมาลองใหม่

**Origin:** Implementation Plan หัวข้อ 5 กำหนด WAIT/RETRY เมื่อ reserve ไม่พอ

**Decision:** Python retry ทุก 50 ms โดย default; `reserve_timeout_ms=0` หมายถึง
รอจน capacity ว่างโดยไม่มี deadline ผู้ดูแลตั้งค่ามากกว่า 0 ได้เมื่อต้องการให้
operational policy ยกเลิก request หลังเวลาที่กำหนด

**Evidence:** การทดลอง `workers2-release-aware-5g-1` ไม่มี OOM แต่ job A timeout
หลัง 30 s ขณะ job B ยังทำงาน เมื่อ B จบ manager คืน base quota ได้ จึงไม่ควรแปลง
resource contention ปกติเป็น training failure ด้วย deadline คงที่

**Trade-off:** polling สร้าง IPC requests จำนวนมากและยังไม่มี notification/event
wakeup; configuration ที่ไม่มีทาง fit อาจรอไม่จบ จึงยังต้องเพิ่ม unschedulable-
capacity diagnosis

## D004 — Pending release

**Status:** `[ORIGINAL PLAN] [IMPLEMENTED]`

**Definition:** client ขอคืน token แล้ว แต่ `memory.current + safety_margin` ยังสูง
กว่า limit ที่จะลดลง token จึงยังถือ Reserved Memory และรอ retry

**Origin:** กฎ safe release ใน Implementation Plan หัวข้อ 3

**Decision:** ไม่ลด `memory.max` แบบฝืนเพื่อบังคับคืน capacity

**Trade-off:** Python allocator หรือ page cache อาจทำให้ capacity คืนช้ากว่า batch
จบ

## D005 — FIFO pending-admission queue

**Status:** `[IMPLEMENTED]` — **ไม่ได้อยู่ใน Implementation Plan เดิม**

**Definition:** FIFO ย่อจาก **First In, First Out** หมายถึง request ที่เข้าแถวรอ
ก่อนจะได้โอกาส grant ก่อน request ที่เข้าทีหลัง

ก่อนแก้ไม่มี queue:

```text
Job A ขอ → WAIT → sleep 50 ms
Job B release แล้วขอ batch ถัดไปทันที → GRANTED
Job A ตื่นมา retry → WAIT อีก
```

ผู้ที่ส่ง retry ถึง socket ก่อนหลัง capacity ว่างเป็นผู้ชนะ จึงไม่มีการรับประกันว่า
request ที่รอนานจะได้สิทธิ์

**Origin/Evidence:** full Task06_Lung run `repro-srcg-1` พบ Job A จบเพียง step 31
แล้ว timeout ขณะที่ Job B จบ 63 steps:

- `artifacts/milestone3/real-workloads/repro-srcg-1/summary.txt`
- `artifacts/milestone3/real-workloads/repro-srcg-1/lung-a.jsonl`
- `artifacts/milestone3/real-workloads/repro-srcg-1/lung-b.jsonl`

ชื่อของ failure นี้คือ **starvation**: job ที่รอไม่ได้ resource เป็นเวลานานเพราะ
job อื่นแซงกลับมาได้ซ้ำ ๆ

**Decision:** เมื่อ request ได้ `WAIT` manager เก็บมันใน global FIFO queue
request ใหม่ห้ามแซงหัวคิว แม้มันมีขนาดเล็กและพอดีกับ capacity ที่เหลือในขณะนั้น

**Evidence after fix:** full trials สองรอบต่อไปนี้จบ 126/126 steps และ
grant/release 126/126 โดยไม่มี timeout:

- `artifacts/milestone3/real-workloads/repro-srcg-fifo-1/`
- `artifacts/milestone3/real-workloads/repro-srcg-fifo-2/`

**Trade-off:** strict FIFO ป้องกัน starvation แต่สร้าง head-of-line blocking ได้:
request ใหญ่ที่หัวคิวอาจทำให้ request เล็กด้านหลังต้องรอทั้งที่ request เล็กพอดี

## D006 — `CANCEL` pending request

**Status:** `[IMPLEMENTED]` — **เกิดตามหลัง D005 ไม่ได้อยู่ในแผนเดิม**

**Definition:** ถอน request ที่ยัง WAIT และยังไม่มี token ออกจาก FIFO queue
มันไม่ใช่การ `RELEASE` เพราะ release ใช้กับ token ที่ grant แล้วเท่านั้น

**Origin:** เมื่อ manager เริ่มจำ FIFO queue หาก client timeout แล้วไม่กลับมา retry
request ที่ถูกทิ้งอาจค้างเป็นหัวคิวและขวางทุก job

**Decision:** Python ส่ง `CANCEL job=... request=...` แบบ best effort ก่อนโยน
reservation timeout และ job reclamation ลบ pending requests ของ job นั้นทั้งหมด

**Trade-off:** ถ้า connection ขาดหลัง daemon grant แล้วแต่ก่อน clientได้รับคำตอบ
client ไม่มี token สำหรับ release กรณีนี้ยังต้องอาศัย job reclamation

## D007 — Phase-aware reservation / `SHRINK`

**Status:** `[IMPLEMENTED]` — **ไม่ได้อยู่ใน Implementation Plan เดิม**

**Definition:** ลดขนาด reservation เดิมหลัง temporary decode arrays ถูกทิ้ง แต่
ยังเก็บสิทธิ์ส่วนเล็กสำหรับ collated CPU batch จน copy ไป GPU เสร็จ

**Origin/Evidence:** full FIFO trials ลด parent peak แต่ makespan เพิ่ม 26.80%
ปัจจุบัน Lung request อยู่ระหว่าง 290–1,600 MiB และ token ทั้งก้อนถูกถือจากก่อน
NIfTI decode จน optimizer step จบ ทั้งที่ resized batch resident ประมาณ 6 MiB

**Current flow สำหรับ `num_workers>0`:**

```text
RESERVE peak
  → worker decode/resize/collate
  → worker ลบ temporary sample tensors
  → SHRINK เหลือ resident batch (+ startup allowance ก่อน step แรก)
  → main รับ batch และ train
  → RELEASE หลัง optimizer step
```

worker เป็นผู้ส่ง SHRINK ก่อน batch เข้า prefetch queue ไม่ใช่ main process เพราะ
ถ้ารอ main รับ batch token เต็มจะค้างใน queue และ workers อื่นอาจรอ capacity ทั้งหมด
`memory.max` จะลดได้ไม่ต่ำกว่า observed `memory.current + safety margin`

default `reservation-phase-mode=auto` เลือก `legacy` เมื่อ `num_workers=0` และ
`shrink-only` เมื่อ `num_workers>0`; early release หลัง H2D ยังไม่ใช่ default

**Evidence:** `workers2-release-wait-5g-1` ทำ Lung สอง jobs × 63 steps ด้วย
DataLoader สอง workers/job จบครบ ไม่มี OOM และคืน quota ผ่าน SHRINK ทั้งสอง jobs

**Trade-off:** Python/NumPy allocator และ page cache อาจยังไม่คืน RAM จริง จึงใช้
safe partial lowering; capacity อาจคืนเพียงบางส่วน และ WAIT time ยังสูงเมื่อ
physical capacity บังคับให้ jobs สลับกันเดิน

## D008 — Current-aware borrow amount

**Status:** `[IMPLEMENTED]`

**Definition:** แปลง estimated batch allocation เป็นจำนวนที่ต้องยืมเหนือ limit
ปัจจุบัน โดยคิดพื้นที่ว่างที่ job มีใน base/effective quota และไม่นับ
`inactive_file` เป็น memory ที่ Reserved Memory ต้องปกป้อง

ตัวแปรสองตัวนี้ห้ามใช้แทนกัน:

- `estimated allocation` คือ RAM ใหม่ที่คาดว่า workload จะใช้ระหว่างสร้าง batch
- `borrow needed` คือส่วนของ estimate ที่ไม่มีที่ว่างรองรับใน effective limit
  ปัจจุบันและต้องหักจาก Reserved Memory

สูตรที่ implement:

```text
protected current = max(0, memory.current - memory.stat[inactive_file])
projected peak     = protected current
                   + remaining estimates ของ tokens อื่นใน job
                   + estimated allocation ของ request ใหม่
                   + admission safety margin
borrow needed      = max(0, projected peak - current effective limit)
```

`inactive_file` คือ filesystem-backed pages ที่ kernel วางไว้ใน inactive list
มันยังนับอยู่ใน `memory.current` และยังอยู่ใต้ hard `memory.max` แต่ kernel reclaim
ได้เมื่อ job ต้องใช้พื้นที่สำหรับ working set ใหม่ SRCG ไม่ได้สั่ง drop host cache
และไม่หัก `active_file`, `anon` หรือ kernel memory ออกจาก protected current

Reserved Memory และการเพิ่ม `memory.max` ตรวจด้วย `borrow needed` แต่ host-headroom
ต้องตรวจด้วย `estimated allocation` เต็มก้อน เพราะ RAM ที่กำลังจะ allocate จริง
ไม่ได้ลดลงเพียงเพราะ job มีพื้นที่เหลือใต้ cgroup limit

**Origin/Evidence:** ใน full run มีตัวอย่าง `memory.current ≈ 934 MiB`, base
`1,660 MiB` และ batch estimate `1,242.5 MiB` ระบบปัจจุบันยืม 1,242.5 MiB ทั้งที่
base ยังมีพื้นที่ประมาณ 726 MiB นี่คือ contract mismatch ระหว่าง estimate กับ
จำนวนที่ manager นำไปบวกบน base quota Phase 1 ยังพบว่า file cache ถูก charge ให้
สอง jobs ไม่เท่ากันมาก จึงห้ามใช้ `memory.current` ทั้งก้อนเป็น working set

**Decision:** manager อ่าน `memory.current` และ `inactive_file` จาก job cgroup เมื่อ
รับ RESERVE แล้วคำนวณ `borrow needed` ภายใต้ accounting mutex request ที่ WAIT
เก็บ estimate เดิมและคำนวณ borrow ใหม่จาก snapshot ล่าสุดเมื่อ retry ถึงหัว FIFO
token บันทึกทั้ง `estimated_bytes` กับ `borrowed_bytes`; ถ้า existing headroom พอ
จะได้ token ที่ `borrowed_bytes=0` และ release ได้ทันทีโดยไม่เขียน `memory.max`

**Evidence after implementation:** current-aware รุ่นแรกที่ยังใช้ total current
(`phase2-current-aware-1`) fail ที่ 28/63 + 21/63 เพราะ file cache ทำให้ pending
tokens สะสม หลังใช้ `inactive_file` สอง no-trace trials จบครบ 126/126, ไม่มี WAIT,
pending release หรือ OOM:

```text
Normal mean: 93.103 s, parent peak 5.000 GiB
SRCG mean:   91.640 s, parent peak 4.599 GiB
Difference:  makespan -1.57%, parent peak -8.01%
```

มีเพียงสอง trials ต่อ mode จึงถือเป็น regression/performance signal ไม่ใช่
thesis-level statistical conclusion

**Trade-off:** kernel อาจ reclaim file cache แล้วต้องอ่าน dataset จาก storage ใหม่
ทำให้ I/O latency เพิ่มได้ และ `memory.current`/inactive state ยังเปลี่ยนหลัง snapshot
hard `memory.max`, estimator safety factor และ Host headroom จึงยังเป็น safety
boundaries; ห้ามให้ Python หัก cache หรือคำนวณ borrow เอง

## D009 — Whole-job serialization baseline

**Status:** `[IMPLEMENTED]` runner พร้อม; เก็บ preliminary boot-separated pair แล้ว

**Definition:** รัน training jobs ทีละตัวเป็นวิธีควบคุม memory ที่ง่ายที่สุด

**Origin:** ต้องใช้ตอบ reviewer ว่า batch-aware orchestration เร็วกว่าการไม่ให้ jobs
overlap เลยหรือไม่ หาก SRCG ไม่ดีกว่าวิธีนี้ ความซับซ้อนเพิ่มอาจไม่คุ้ม

**Trade-off:** ปลอดภัยและเข้าใจง่าย แต่ไม่ใช้ช่วง CPU/GPU idle ของแต่ละ job เพื่อ
ซ้อนงานอื่น

**Measurement rule:** sequential และ SRCG timing ต้องมาจากคนละ reboot และเริ่ม
ภายใน boot-age limit เดียวกัน `run_cold_boot_comparison.sh` อนุญาตหนึ่ง recorded
run ต่อ boot และบันทึก hashes ของ code/binary ห้ามใช้ `sequential-normal-{1,2}`
เป็น timing result เพราะสองรอบนั้นไม่ได้ควบคุม page-cache state

**Model-output invariant:** ก่อนใช้ timing candidate ต้องผ่าน
`verify_training_equivalence.py`: training config, 63/63 steps, loss sequence และ
final model SHA-256 ต้องตรง normal baseline ค่า default ใช้ loss tolerance 0

**Preliminary result:** sequential จบ 2/2 jobs ใน 170.015 s; SRCG จบ 2/2 jobs
ใน 93.384 s (`1.821x`, makespan ลด 45.07%) ทั้งคู่ OOM-kill 0 และ exact quality
gate ผ่าน อย่างไรก็ตาม sequential เริ่มที่ boot age 7,257 s ส่วน SRCG เริ่มที่
82 s และมีเพียงหนึ่ง run ต่อ mode จึงยังไม่ใช่ timing result สำหรับสรุปเชิงสถิติ
ใน thesis ดูรายละเอียดและข้อจำกัดใน `docs/safety-evaluation.md`

## D010 — Phase 1 memory trace

**Status:** `[IMPLEMENTED]`

**Definition:** diagnostic แบบ opt-in ที่บันทึกจุดเวลาและสถานะ memory ของ job
ก่อน reserve, หลัง grant, หลัง DataLoader ทำ collate, หลัง CPU-to-GPU call,
หลัง optimizer และก่อน/หลัง release แล้วนำไปเทียบกับ controller sample ทุก 50 ms
มันเขียนลง experiment log เท่านั้นและไม่แสดงบน terminal ของผู้ใช้ทั่วไป

คำใน trace:

- `anon` คือ anonymous memory เช่น heap/allocator/tensor ของ process
- `file` คือ filesystem cache ที่ kernel charge ให้ cgroup; ค่านี้รวม `shmem`
- `phase point` คือจุดสังเกต ไม่ใช่คำสั่งควบคุม และไม่เปลี่ยน training flow
- `sampled peak` คือค่าสูงสุดที่เห็นจาก sample จึงอาจพลาด spike ที่สั้นกว่า 50 ms

**Origin:** ผล full FIFO ลด peak แต่เพิ่ม makespan 26.80% จึงต้องแยกว่า estimate
สูงเกินจริงตรงไหนก่อนเปลี่ยน admission

**Decision:** เปิดด้วย `--memory-trace` เฉพาะการทดลอง SRCG และวิเคราะห์ด้วย
`benchmarks/dataloader_policy/analyze_memory_trace.py` ไม่มีการเปลี่ยน FIFO,
quota, token lifetime หรือ cgroup release rule ใน Phase 1

**Evidence:** `phase1-memory-trace-2` รัน Lung สอง jobs ได้ 28/63 steps ต่อ job
ก่อน timeout โดยไม่มี OOM จุดสุดท้ายก่อนค้างเป็นดังนี้:

```text
Job A: anon 915.95 MiB, file 12.08 MiB, current 936.36 MiB
Job B: anon 915.96 MiB, file 690.16 MiB, current 1,618.96 MiB
```

ทั้งสอง job ใช้ model, seed และ sample order เดียวกัน ความต่างหลักจึงไม่ได้มาจาก
tensor resident ของแต่ละ process แต่เป็น `file` charge ที่กระจายไม่เท่ากันตาม
memory ownership ของ cgroup v2

Job B คืน token 615 MiB ไม่ได้เพราะ current 1,618.96 MiB บวก release safety
64 MiB สูงกว่า base limit 1,660 MiB จึงเหลือ Reserved Memory 1,185 MiB ขณะที่
batch ถัดไปขอ 1,217.5 MiB ขาดเพียง 32.5 MiB Job B จึง WAIT และ Job A ที่อยู่
ด้านหลัง FIFO ก็ WAIT จนทั้งคู่ timeout

**Trade-off:** trace อ่าน `memory.current`, `memory.peak`, `memory.max` และ
`memory.stat` หลายครั้งต่อ batch จึงห้ามใช้ตัวเลขเวลาจาก diagnostic run เป็นผล
performance หลัก

**Consequence for D007/D008:** `SHRINK` อย่างเดียวอาจยัง PENDING เพราะ kernel
ยัง charge file cache อยู่ Phase 2 จึง implement D008 พร้อมแยก `inactive_file`
ก่อน โดยยังรักษา host-headroom และ parent-limit checks เดิม

## ลำดับเหตุการณ์ที่ทำให้เกิด D005–D008

```text
M2–M3 เดิม: atomic reserve + WAIT/retry แต่ไม่มี fairness rule
        ↓
full run repro-srcg-1: A timeout step 31, B จบ step 63
        ↓
D005 implement FIFO เพื่อกัน starvation
        ↓
D006 implement CANCEL เพราะ FIFO ต้องลบ request ที่ client ทิ้ง
        ↓
full post-fix runs: ทั้งสอง jobs จบ แต่ makespan +26.80%
        ↓
ตรวจ trace พบ request ถือ capacity ครอบ decode ถึง optimizer
และยืม batch estimate เต็มก้อนโดยไม่หัก base headroom
        ↓
D007 SHRINK และ D008 current-aware borrow ถูกเสนอ
        ↓
D010 Phase 1 trace แยก anon/file โดยไม่เปลี่ยน policy
        ↓
พบ file charge ไม่สมดุลทำให้ release หนึ่ง token PENDING และ pool ขาด 32.5 MiB
        ↓
D008 รุ่น total-current ยังสะสม pending tokens และ fail
        ↓
D008 รุ่น inactive-file-aware จบ full runs 2/2 โดยไม่มี WAIT/PENDING/OOM
ส่วน D007 ยังเป็น proposal และยังไม่ implement
```

## D011 — Safety-first evaluation objective

**Status:** `[ORIGINAL PLAN] [OBSERVED]`

**Definition:** outcome หลักคือจำนวน training jobs ที่ทำงานเสร็จและจำนวน process
ที่ถูก OOM-kill เมื่อเกิด RAM contention ไม่ได้กำหนดว่า runtime overhead ต้องต่ำ
กว่า 1--3%

**Origin:** Implementation Plan วางเป้าหมายให้ลด predictable OOM และ contain
overuse ผู้ใช้ยืนยันเพิ่มเติมว่า SRCG ต้องรับกรณี multi-training แย่ง RAM และกรณี
training ใช้ RAM อยู่แล้วมี process อื่นขอเพิ่มจนเกิด OOM

**Evidence:** capacity 4 GiB ทำให้ normal ล้ม 0/2 jobs แต่ SRCG จบ 2/2 jobs ใน
2/2 trials และ late-arrival 512 MiB ให้ผลเดียวกัน รายละเอียดอยู่ใน
`docs/safety-evaluation.md`

**Decision:** รายงาน `completed jobs`, `oom_kill` และ time-to-successful-result เป็น
ผลหลัก รายงาน makespan, WAIT และ peak เป็น trade-off ห้ามเปรียบเทียบเวลา run ที่
ถูกฆ่ากลางทางกับ run ที่ทำงานเสร็จเหมือนเป็น slowdown ปกติ

**Trade-off:** safety อาจ serialize บางช่วงและเพิ่มเวลารอ ถ้า capacity เพียงพออยู่
แล้ว SRCG ต้องแสดงต้นทุนนี้อย่างตรงไปตรงมา

## D012 — Late-arriving sibling experiment

**Status:** `[OBSERVED]` — เป็น evaluation scenario ไม่ใช่ SRCG API

**Definition:** เริ่ม process ที่อยู่นอก SRCG parent หลัง training เริ่มแล้ว 5
วินาที process นี้ allocate 512 MiB และถือไว้ 30 วินาที เพื่อแทน daemon, notebook
หรือโปรแกรมอื่นที่ต้องการ RAM ระหว่าง train

**Origin:** requirement ของผู้ใช้ที่ว่า training อาจใช้ RAM จน process ซึ่งขอเพิ่ม
เพียงเล็กน้อยทำให้ระบบ OOM

**Evidence:** เมื่อ outer limit และ training parent เท่ากัน 5 GiB พร้อม headroom 0
kernel OOM-kill training ทั้งสองงานใน 2/2 trials เมื่อจำกัด SRCG parent ที่ 4 GiB
และใช้ ancestor-aware headroom 1 GiB ใต้ outer 5 GiB training สองงานและ sibling
จบครบใน 2/2 trials โดยไม่มี OOM

**Decision:** ใช้ sibling จริงที่ allocate/touch pages ไม่ inject synthetic tensor
ใน training program และเก็บ outer/parent `memory.events` แยกกัน

**Trade-off:** kernel อาจเลือก victim คนละ process ใน host หรือ workload อื่น จึง
สรุปได้เพียงว่า parent boundary และ ancestor-aware admission ลดความเสี่ยงใน
topology นี้ ไม่รับประกันว่า newcomer จะเป็นหรือไม่เป็น victim เสมอ

## D013 — Ancestor-cgroup-aware headroom

**Status:** `[IMPLEMENTED]`

**Definition:** ถ้า SRCG อยู่ใน container หรือ systemd scope ให้ใช้ finite
`memory.max` และ `memory.current` ของ ancestor cgroup เป็น boundary เพิ่มเติม ไม่
ประเมิน capacity จาก host `/proc/meminfo` อย่างเดียว

**Origin:** late-arrival test รุ่นแรกสร้าง outer scope 5 GiB แต่ implementation
ขณะนั้นของ `srcg init` ตรวจ host `MemTotal` และ `srcg serve` ตรวจ host
`MemAvailable` structural headroom เกิดจากการตั้ง child parent 4 GiB โดยตรง ไม่ได้
เกิดจาก manager ค้นพบ outer 5 GiB อัตโนมัติ

**Decision:** อ่าน ancestors ทุกระดับเหนือ SRCG parent ที่มี finite `memory.max`
ตอน `init` ทุก boundary ต้องรองรับ `parent limit + headroom` ตอนแต่ละ RESERVE ใช้
available ที่ต่ำที่สุดระหว่าง host `MemAvailable` และทุกค่า
`ancestor memory.max - ancestor memory.current` ถ้า ancestors เป็น `max` ทั้งหมด
จึง fallback ไป host อย่างเดียว

**Evidence after implementation:** late-arrival full trials
`ancestor-aware-srcg-{1,2}` ทำครบ 126/126 steps พร้อม 512 MiB sibling และไม่มี OOM
ทั้งสองรอบ outer peak เฉลี่ย 3.699 GiB ใต้ limit 5 GiB manager ตอบ WAIT ด้วยเหตุ
`host_headroom` 407 ครั้งต่อ trial ขณะที่ normal headroom 0 ถูก OOM-kill ทั้งสอง
training jobs

**Trade-off:** `memory.current` รวม sibling usage และเปลี่ยนตลอด policy จึง
conservative ขึ้น ในสอง trials makespan เฉลี่ยเพิ่มเป็น 111.674 s และ WAIT รวม
เฉลี่ย 52.814 s แต่ได้ completed jobs 2/2 แทน 0/2

## D014 — In-flight commitment ของ DataLoader workers

**Status:** `[IMPLEMENTED]` — **เกิดจาก multi-worker optimization test**

**Definition:** `remaining_estimated_bytes` คือ RAM ที่ token ซึ่ง grant แล้ว
ยังมีสิทธิ์ allocate ต่อ แม้ allocation นั้นยังไม่ปรากฏใน `memory.current`

**Origin/Evidence:** workers สองตัว reserve ติดกันก่อนตัวแรกเริ่ม allocate ทั้งคู่
จึงเคยนำ base headroom ก้อนเดียวกันไปใช้ซ้ำ และเกิด per-job OOM ใน
`workers2-legacy-1` ต่อมา `workers2-feasible-5g-1` พบ race อีกแบบ: RELEASE token
เก่าลด limit ขณะที่ token ใหม่ยังโหลดอยู่ ทำให้ quota ของ token ใหม่หายกลางทาง

**Decision:** RESERVE รวม remaining estimate ของ active tokens, SHRINK ลด commitment
แบบ monotonic และ RELEASE/RETRY ห้ามลด `memory.max` ต่ำกว่า
`memory.current + other in-flight estimates + safety margin`

**Trade-off:** ระหว่าง worker กำลัง allocate manager นับ estimate เต็มแม้บางส่วน
เข้า `memory.current` แล้ว จึง conservative และอาจเกิด WAIT เพิ่ม แต่ไม่ใช้
snapshot ที่ล้าหลังเป็นเหตุผลดึง quota ที่ grant แล้วคืน

## D015 — Admission safety, file-cache estimate และ startup allowance

**Status:** `[IMPLEMENTED]`

**Definition:** เป็น safety สามชั้นคนละหน้าที่:

- `policy safety factor` คูณ memory estimate ของ batch
- `admission safety margin` เผื่อ process/COW growth ระหว่าง snapshot กับ allocation;
  default training path ใช้ 64 MiB + 32 MiB ต่อ DataLoader worker
- `startup allowance` 256 MiB เก็บไว้ใน SHRINK เฉพาะ batches ที่ prefetch ก่อน
  optimizer step แรกเสร็จ เพื่อรองรับ lazy host allocations ของ PyTorch/CUDA

NIfTI/JPEG estimate รวมขนาด compressed input file ด้วย เพราะการอ่านไฟล์ทำให้
filesystem-cache pages ถูก charge เข้า cgroup ก่อนที่จะกลายเป็น `inactive_file`

**Origin/Evidence:** `workers2-worker-shrink-safety-{1,2}` ชน limit แม้ estimate
decoded tensors ถูกต้อง; trace พบ `.nii.gz` image+label ของ batch แรก 74–81 MiB
ไม่อยู่ในสูตร และ step แรกมี host growth หลัง worker ส่ง SHRINK

**Decision:** ทั้งหมดเป็น policy ภายใน ผู้ใช้ยังระบุเพียง options ปกติ เช่น
batch size, epochs, optimizer และ `num_workers`

**Evidence after complete fix:** `workers2-release-wait-5g-1` รัน Lung สอง jobs,
สอง workers/job, 63 steps/job จบครบ parent OOM=0, model hash ตรง baseline และ loss
ทุก step ตรงแบบ exact; makespan 92.956 s, parent peak 4,818,538,496 bytes

**Trade-off:** margin ทำให้ grant conservative และค่าปัจจุบันเป็นผล calibration
ของ workload/machine นี้ ยังต้องทดลองหลายเครื่องก่อนอ้าง generality ใน thesis

## D016 — Partial pending release และ automatic retry

**Status:** `[IMPLEMENTED] [VALIDATED]`

**Definition:** ถ้า RELEASE ยังลด `memory.max` กลับถึง base quota ไม่ได้ manager
จะคืนเฉพาะ bytes ที่ลดได้โดยยังรักษา
`memory.current + other in-flight estimates + safety margin` แล้วเก็บส่วนที่เหลือ
เป็น `PENDING_RELEASE` ทุก RESERVE attempt ของ job เดียวกันจะตรวจ pending tokens
ด้วย snapshot ล่าสุดก่อนคำนวณ grant ใหม่

**Origin/Evidence:** cold `high-srcg` attempt แรกหยุดที่ step 8 tokens `rsv-2`
และ `rsv-4` ค้าง PENDING รวมประมาณ 1.20 GB เหลือ Reserved Memory 691,431,402 bytes
ขณะที่ FIFO head ต้องการประมาณ 1.13 GB workers ทั้งหมดอยู่ใน RESERVE loop จึงไม่
มี worker กลับไปเรียก RETRY อีก ระบบไม่ OOM แต่ไม่มี progress

**Decision:** แก้ที่ C++ `ReservationManager` เพื่อให้ทุก client ได้ semantics
เดียวกัน ไม่ผลักภาระไปให้ PyTorch training code partial lowering ใช้ complete
`memory.current` และรักษา commitments ของ active tokens จึงไม่ลด hard limit จาก
การหัก file cache แบบคาดเดา

**Evidence after implementation:** unit tests ครอบ partial accounting และ
RESERVE-triggered retry; C++/Python/IPC suites ผ่าน warm full Lung validation
สอง jobs × 63 steps จบใน 95.434 s, parent peak 4,748,357,632 bytes, parent OOM=0,
loss delta 0 และ final model hash ตรง baseline

**Trade-off:** token อาจอยู่ PENDING ต่อโดยถือ capacity เล็กน้อยจน usage ลดลงหรือ
job ถูก reclaim และ RESERVE polling มีงาน accounting เพิ่ม แต่ตัดวงจรที่ caller
ทุกตัวรอ quota ซึ่งจะถูกคืนได้ก็ต่อเมื่อ caller ส่ง RETRY แยกต่างหาก

## D017 — Experiment scope ต้องรวม orchestration overhead

**Status:** `[CORRECTED] [VALIDATED]`

**Definition:** `parent memory` คือ hard boundary ของ training jobs ส่วน enclosing
systemd scope ยังครอบ manager, controllers และ runner ด้วย ดังนั้น outer limit
ต้องมากกว่า `parent + protected headroom` พอรองรับ process เหล่านี้

**Origin/Evidence:** high-pressure cold attempt ใช้ parent 5 GiB, headroom 1 GiB
และ outer 6 GiB ทำให้ batch estimate ใหญ่สุดขาด ancestor capacity ประมาณ 136 MiB
ทั้ง jobs WAIT ที่ step 8 โดยไม่มี OOM policy ทำงานถูก แต่ test topology ไม่มี
capacity ตาม contract ที่ตั้งใจ

**Decision:** คง training parent 5 GiB และ headroom 1 GiB แต่เพิ่ม outer test scope
เป็น 7 GiB หนึ่ง GiB ที่เพิ่มไม่ขยาย quota ของ training; มันรองรับ framework
processes นอก parent

**Evidence after correction:** warm Lung 2-job validation จบ 126/126 steps ใน
86.201 s, parent peak 5,009,297,408 bytes, OOM-kill 0 และ exact quality gate ผ่าน

**Trade-off:** sequential baseline เดิมใช้ outer 6 GiB แม้มันไม่ชน limit; สำหรับ
thesis repeated trials ควรรันทั้งสอง mode ด้วย outer 7 GiB แต่ผล engineering pair
สามารถรายงานพร้อมข้อจำกัดนี้ได้

## D018 — Periodic checkpoint budget admission

**Status:** `[IMPLEMENTED] [VALIDATED]` สำหรับ periodic save และ manual resume;
CUDA OOM recovery จริงยังไม่ได้ทดสอบ

**Definition:** `X` คือจำนวน training steps ระหว่าง checkpoint สองครั้ง
`CHECKPOINT_BEGIN` ให้สิทธิ์เพิ่ม job `memory.max` จาก checkpoint budget ก่อนสร้าง
payload หรือเขียนไฟล์ `CHECKPOINT_END` คืนสิทธิ์เมื่อ `memory.current` และ
batch allocations ที่ยังไม่เสร็จยอมให้ลด limit ได้ ถ้ายังลดไม่ได้ capacity ค้าง
ไว้และโอนให้ checkpoint ครั้งถัดไปด้วย token ใหม่

**Origin:** ผู้ใช้เลือก periodic save ทุก X steps ที่ safe point เพื่อรับมือทั้ง
CUDA OOM และสาเหตุอื่น โดยไม่ต้องพึ่ง predictor, auto batch change หรือ auto
restart เป็นกลไกหลัก ก่อนหน้านี้ checkpoint budget ถูกกันจาก batch pool แล้ว
แต่ยังไม่ถูกเพิ่มให้ job ตอน save

**Decision:** save เฉพาะหลัง optimizer step จบ ใช้ checkpoint budget ผ่าน shared
manager และให้ผู้ใช้สั่ง resume เองจาก checkpoint ล่าสุด ค่า `--max-restarts`
เริ่มต้นยังเป็น 0 Checkpoint budget เป็น quota เพิ่มชั่วคราว ไม่ใช่ RAM ที่ lock
ไว้จริงหรือขนาดไฟล์ checkpoint

**Evidence:** Lung 63/63 steps, workers 4, parent 3840 MiB, X=0/5/10 จบครบ
และ final model SHA-256 ตรงกัน; X=5 บนโค้ดล่าสุดจบ 63/63, save
12 ครั้ง, OOM=0 หลังจำลอง process failure ที่ checkpoint step 10 แล้ว manual
resume งานจบ step 63 ด้วย hash เดียวกัน รายละเอียดและข้อจำกัดอยู่ใน
`docs/periodic-checkpoint.md`

**Trade-off:** checkpoint เพิ่มเวลาเขียนไฟล์และใช้ RAM ชั่วคราว ค่า X ที่เหมาะ
ต้องวัดซ้ำกับ workload จริง หาก CUDA OOM เกิดกลาง step จะใช้ checkpoint ล่าสุด
แทนการพยายาม save ในจังหวะที่ VRAM หมด
