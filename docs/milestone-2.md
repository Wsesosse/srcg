# SRCG Milestone 2: Reserved Memory

Milestone 2 ทำให้ jobs ภายใต้ SRCG parent ยืม CPU RAM จาก **Reserved Memory**
ก่อนโหลด batch โดย daemon ต้องเพิ่ม cgroup limit ก่อนตอบ `GRANTED` และต้องไม่คืน
Reserved Memory จนลด limit ได้อย่างปลอดภัย

## Development status

### Phase 2.1 — accounting foundation: implemented

ไฟล์หลัก:

```text
reservation_manager.h/.cpp
tests/reservation_manager_tests.cpp
```

สิ่งที่มีแล้ว:

- strict capacity partition ระหว่าง active base quotas, Reserved Memory และ
  checkpoint budget
- atomic reserve/release accounting ภายใต้ mutex เดียว
- reservation token lifecycle: `ACTIVE`, `PENDING_RELEASE`, `RELEASED`
- request ID idempotency ภายใน job เดียวกัน
- duplicate release ไม่คืน capacity ซ้ำ
- host-headroom admission check จาก future additional bytes โดยใช้ค่าต่ำสุดระหว่าง
  host `MemAvailable` และพื้นที่ว่างของ finite ancestor cgroups
- cgroup limit writer ต้องสำเร็จก่อน `GRANTED`
- failed limit write ไม่ commit token/accounting
- reclaim reservations หลัง caller ยืนยันว่า job cgroup ว่าง
- FIFO pending queue ป้องกัน job ที่เพิ่ง release แล้ว reserve ซ้ำแซง job ที่รอก่อน
- oversized request ถูก DENIED แทนการค้างเป็นหัว FIFO และ `CANCEL` ลบ request
  ที่ client เลิกรอ
- concurrency test ที่เริ่ม 16 requests พร้อมกันและยืนยันว่า committed bytes ไม่
  เกิน Reserved Memory

เพิ่ม dynamic cgroup operations แล้ว:

```cpp
setJobMemoryLimit(job_id, new_limit);
lowerJobMemoryLimitIfSafe(job_id, new_limit, safety_margin);
```

`lowerJobMemoryLimitIfSafe` ปฏิเสธการลดเมื่อ:

```text
memory.current + safety_margin > proposed_limit
```

### Phase 2.2 — shared manager and IPC: implemented for Milestone 3

ไฟล์หลัก:

```text
reservation_ipc.h/.cpp
srcg_torch/client.py
```

สิ่งที่มีแล้ว:

- `srcg serve` เป็น shared foreground manager ผ่าน Unix-domain socket
- ตรวจ peer UID และตรวจ cgroup membership สำหรับ reserve/release
- `srcg run --manager-socket` register/reclaim job และส่ง connection config ให้
  training process
- route `RESERVE`/`CANCEL`/`RELEASE`/`RETRY` เข้า `ReservationManager`
- route `CHECKPOINT_BEGIN`/`CHECKPOINT_END` เพื่อเพิ่ม job limit จาก checkpoint
  budget เฉพาะตอน save; manager คง limit ไว้หากลดแล้วจะกระทบ batch ที่กำลังโหลด
- event log แยกจาก stdout/stderr ของ training
- Python WAIT/retry ใช้ request ID เดิมเพื่อรักษา idempotency และส่ง `CANCEL`
  แบบ best effort เมื่อ timeout

สิ่งที่ยังเหลือคือ version negotiation, service/unit packaging และ journal/replay
สำหรับ manager crash จึงยังไม่เรียกส่วนนี้ว่า durable resident daemon

### Phase 2.3 — durable state and reconciliation: not implemented

- persistent journal สำหรับ jobs, requests และ tokens
- crash-consistent grant/release ordering
- scan cgroups และ reconcile accounting หลัง daemon restart

### Phase 2.4 / Milestone 3 — PyTorch client: implemented

- `srcg_torch.ReservationClient`
- worker-side `ReservedBatchDataset.__getitems__` สำหรับ batch admission
- `PlanningBatchSampler` สำหรับ plan debug/exact checkpoint cursor
- JPEG/NIfTI header estimator สำหรับ COCO val2017 และ Task06_Lung
- WAIT โดยไม่โหลด batch พร้อม timeout
- release หลัง training step และ pending-release retry ก่อน batch ถัดไป

normal reservation รองรับ multiprocessing prefetch แล้ว โดยแต่ละ in-flight
batch ขอ token ใน worker ก่อน decode และส่ง tokenกลับ main processเพื่อ release
หลัง optimizer ข้อจำกัด `num_workers=0` เหลือเฉพาะ `--policy-debug` ดูผลจริงที่
[milestone-3.md](milestone-3.md)

## Accounting invariants

```text
base_committed
+ reserved_memory_capacity
+ checkpoint_budget
<= parent_limit
```

```text
job_effective_limit = job_base_quota + job_borrowed_bytes
```

```text
reserved_memory_committed
= bytes ของ ACTIVE และ PENDING_RELEASE tokens
```

`PENDING_RELEASE` ยังถือ capacity เพราะ kernel limit ยังไม่ได้ลด ส่วน
`RELEASED` เท่านั้นที่คืน capacity ให้ Reserved Memory

## Current API contract

### Register

```cpp
manager.registerJob("job-A", base_quota_bytes);
```

- same job/same base เป็น idempotent
- same job/different base ถูกปฏิเสธ
- base capacity ไม่พอถูกปฏิเสธ

### Reserve

```cpp
auto result = manager.reserve(
    "job-A",
    "request-17",
    estimated_allocation_bytes,
    memory_current_bytes,
    host_mem_available_bytes,
    inactive_file_bytes);
```

ผลเป็น `Granted`, `Wait` หรือ `Denied` เมื่อ granted จะมี token และ effective
limit ใหม่ `Wait` จะอยู่ใน FIFO จน request เดิม retry มาถึงหัวคิวหรือถูก cancel
ตั้งแต่ Phase 2 manager คำนวณ `borrowed_bytes` จาก estimate, current effective
headroom และ `inactive_file`; Host headroom ยังคงตรวจ estimate เต็มก้อนและรวม
ancestor-cgroup capacity เมื่อรันใน container/systemd scope

### Cancel pending request

```cpp
manager.cancelPending("job-A", "request-17");
```

ใช้เมื่อ client timeout/interrupt ก่อน grant เพื่อไม่ให้ request ที่ไม่มีผู้รอแล้ว
ขวาง request ถัดไป

### Release

```cpp
auto result = manager.release(
    "job-A", token, memory_current_bytes, safety_margin_bytes);
```

ผลเป็น `Released`, `Pending`, `AlreadyReleased` หรือ `Denied`

### Empty-job recovery

```cpp
manager.reclaimEmptyJob("job-A");
```

caller ต้องพิสูจน์จาก cgroup ก่อนว่า job ว่าง operation นี้จึงคืน outstanding
tokens โดยไม่ลด limit ของ live process tree

## Test

```bash
cmake --build build-srcg --parallel
ctest --test-dir build-srcg -R srcg_reservation_tests --output-on-failure
```

ชุด regression ทั้งหมด:

```bash
ctest --test-dir build-srcg --output-on-failure
```

Real-kernel integration ยังใช้ delegated cgroup test path เหมือน Milestone 1

## ใช้จาก `train.py`

เปิด `srcg serve` แล้วส่ง `--manager-socket` ให้ `srcg run`; training โหมด `auto`
จะ reserve จาก estimate ก่อน load โดยอัตโนมัติ ส่วนการรันโดยไม่มี manager ยังคง
เป็น fixed quota แบบ Milestone 1
