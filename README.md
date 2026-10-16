# SRCG

SRCG คือ framework สำหรับควบคุม RAM ของหลาย training process บน Linux เครื่อง
เดียวด้วย **C++20 และ cgroup v2** เป้าหมายหลักคือให้งานรอดจาก memory contention:
เมื่อ RAM ไม่พอ ระบบยอมหน่วงการสร้าง batch แทนการปล่อยให้ Linux OOM-kill งาน
หนึ่งหรือหลายงาน เวลา execution เป็น trade-off ที่ต้องรายงาน ไม่ใช่เงื่อนไขว่า
overhead ต้องต่ำกว่า 1--3%

เวอร์ชันนี้ทำหน้าที่เป็น foreground process supervisor: สร้าง cgroup ให้แต่ละ
job, กำหนด RAM/swap quota, ย้าย process เข้า cgroup ก่อน `exec`, เฝ้าดูการใช้
memory, จำแนก cgroup OOM และ cleanup cgroup หลัง job จบ

> `memory.max` คือเพดานการใช้ memory ไม่ใช่การจอง RAM จริงล่วงหน้า และไม่ได้
> รับประกันว่า workload จะไม่มี OOM 100%

Milestone 2 accounting และ shared Unix-socket manager ต่อเข้า `srcg run` แล้ว
Milestone 3 เชื่อม PyTorch batch-aware reservation กับ COCO/Lung และมีผลทดลองบน
GPU จริง ดู [docs/milestone-2.md](docs/milestone-2.md) และ
[docs/milestone-3.md](docs/milestone-3.md)

Periodic checkpoint ทุก X steps มี checkpoint budget admission และทดสอบ
resume หลัง process จบแล้ว ดู
[docs/periodic-checkpoint.md](docs/periodic-checkpoint.md)

ผล safety แบบ full workload ซึ่งเปรียบเทียบทั้ง RAM ไม่พอและ process ที่เข้ามา
ขอ RAM ระหว่าง train อยู่ที่
[docs/safety-evaluation.md](docs/safety-evaluation.md)

คำหรือกลไกที่เพิ่มภายหลังแผนเดิมต้องอ่านพร้อมที่มาและสถานะจาก
[docs/decision-trace.md](docs/decision-trace.md) เพื่อแยกของที่ implement แล้วออก
จากข้อเสนอที่ยังเรียกใช้ไม่ได้

## ขอบเขตของ Milestone 1

สิ่งที่มีแล้ว:

- ตรวจว่า host ใช้ cgroup v2 และมี memory controller
- สร้าง/configure parent ผ่าน `srcg init` และสร้าง job ผ่าน `srcg run`
- กำหนด `memory.max`, `memory.swap.max` และ `memory.oom.group`
- ตรวจ host และ finite ancestor-cgroup headroom ก่อนกำหนด parent limit
- ย้าย child process เข้า job cgroup ก่อน `exec`
- ให้ process/worker ที่ fork ภายหลัง inherit cgroup เดียวกัน
- อ่าน `memory.current`, `memory.peak`, `memory.events`, `memory.stat` และ
  `cgroup.events`
- อ่าน host `MemAvailable`, swap และ memory PSI
- จำแนก completed, nonzero exit, signal exit และ cgroup OOM
- ไม่ลบ cgroup ที่ยังมี process ทำงานอยู่
- มี CLI สำหรับ check, init, run, status และ cleanup
- NVML build เป็น optional component และไม่ขวาง RAM-only workflow

สิ่งที่ยังไม่มีใน Milestone 1:

- resident daemon หรือ Unix-domain socket
- Reserved Memory และ reservation token
- durable journal/reconcile หลัง manager process crash
- checkpoint, cooperative stop และ restart
- VRAM quota หรือ CUDA OOM recovery
- predictive OOM และ Peak Decay
- Kubernetes/Airflow integration

## Architecture

```text
ผู้ใช้
  │
  │ srcg run ... -- PROGRAM
  ▼
srcg CLI
  │
  ▼
SrcgDaemon
  ├── LinuxMonitor
  │     ├── /proc/meminfo
  │     ├── /proc/pressure/memory
  │     └── /proc/<pid>/status
  │
  ├── CgroupV2Manager
  │     └── /sys/fs/cgroup/<parent>/<job>/
  │           ├── memory.max
  │           ├── memory.swap.max
  │           ├── memory.current
  │           ├── memory.peak
  │           ├── memory.events
  │           ├── memory.stat
  │           ├── memory.oom.group
  │           ├── cgroup.events
  │           └── cgroup.procs
  │
  └── ProcessSupervisor
        ├── ThreadManager: polling worker
        ├── fork + pipe barrier
        ├── attach PID เข้า cgroup
        ├── exec program
        └── waitpid + exit/OOM classification
```

`srcg` ยังไม่ใช่ background service ในเวอร์ชันนี้ คำสั่ง `srcg run` จะอยู่ใน
foreground ตลอดอายุของ training process หนึ่ง job ส่วนหลาย job สามารถรันผ่าน
หลาย terminal/process โดยใช้ parent cgroup เดียวกันและ job ID คนละชื่อ

## ลำดับการทำงานของ `srcg run`

```text
1. parse CLI และแปลงหน่วยทั้งหมดเป็น bytes
                     ↓
2. อ่าน parent limits ที่ `srcg init` ตั้งไว้จาก cgroup filesystem
                     ↓
3. ตรวจว่า job limit ไม่เกิน parent limit
                     ↓
4. สร้าง job cgroup และตั้ง job quota
                     ↓
5. fork child แต่ให้ child รอที่ pipe barrier
                     ↓
6. parent เขียน child PID ลง cgroup.procs
                     ↓
7. parent ปลด barrier แล้ว child จึง exec program
                     ↓
8. sampling cgroup + host memory ตาม interval
                     ↓
9. waitpid และเปรียบเทียบ memory.events ก่อน/หลัง
                     ↓
10. จำแนก exit และ cleanup เมื่อ cgroup ไม่มี process
```

Pipe barrier ในข้อ 5–7 เป็น invariant สำคัญของ framework: Python process ต้อง
อยู่ใน cgroup **ก่อน** เริ่มโปรแกรม ดังนั้น DataLoader worker หรือ subprocess ที่
เกิดภายหลังจึง inherit job cgroup โดยอัตโนมัติ

## Memory model

ตัวอย่างโครงสร้าง:

```text
/sys/fs/cgroup/srcg/          memory.max = 6 GiB
├── training-A/               memory.max = 2 GiB
└── training-B/               memory.max = 2 GiB
```

- Parent limit เป็น safety boundary รวมของ SRCG jobs
- Job limit เป็น fixed quota ของแต่ละ process tree
- `host_headroom` ไม่ถูกแจกให้ SRCG parent เพื่อเหลือพื้นที่แก่ OS และ process อื่น
  และตรวจ ancestor limit/current เพิ่มเมื่ออยู่ใน container หรือ systemd scope
- Swap policy กำหนดแยกด้วย `memory.swap.max`
- ค่า `--swap 0` หมายถึง job ใช้ swap ไม่ได้
- ค่า `max` หมายถึงไม่มี child-level swap limit แต่ยังถูกจำกัดโดย ancestor cgroup

เมื่อใช้ `srcg serve` และ `srcg run --manager-socket`, Milestone 3 จะเพิ่ม/ลด
`memory.max` ตาม batch plan จริง ส่วน `srcg run` ที่ไม่ต่อ manager ยังคงเป็น
fixed-quota mode ของ Milestone 1

## Process และ OOM classification

หลัง process จบ `ProcessSupervisor` ใช้ทั้ง `waitpid()` และผลต่างของ
`memory.events`:

| Classification | ความหมาย |
| --- | --- |
| `completed` | process exit code 0 |
| `nonzero_exit` | process จบด้วย exit code ที่ไม่ใช่ 0 |
| `signal_exit` | process ถูก signal แต่ `oom_kill` ไม่เพิ่ม |
| `cgroup_oom` | `oom_kill` หรือ `oom_group_kill` เพิ่มระหว่าง run |

CUDA OOM ยังไม่ถูกรวมกับตารางนี้ เพราะเป็น error ที่ PyTorch/CUDA สามารถ throw
ภายใน process ได้และเป็นงานของ Milestone ถัดไป

## Cleanup semantics

SRCG อนุญาตให้ลบ job cgroup เมื่อ:

```text
cgroup.events: populated = 0
และ
cgroup.procs ว่าง
```

`memory.current` อาจยังมากกว่า 0 หลัง process สุดท้ายจบ เพราะ page cache หรือ
kernel charge ที่ยังตกค้าง ค่านี้จึงถูกบันทึกแต่ไม่ใช้เป็น liveness condition
เมื่อ `rmdir` empty cgroup สำเร็จ kernel จะจัดการ residual charge ต่อเอง

หาก supervisor crash แต่ training process ยังอยู่ cgroup จะไม่ถูกลบ ผู้ใช้ตรวจ
ด้วย `status` และเรียก `cleanup` ได้หลัง process จบแล้ว

ถ้า training process จบแล้วแต่ cgroup ค้างจาก supervisor ที่ถูก `SIGKILL` หรือ
crash การเรียก `srcg run` รอบถัดไปด้วย job ID เดิมจะตรวจว่า `populated=0` และ
`cgroup.procs` ว่าง แล้วลบ stale cgroup ก่อนสร้าง run ใหม่ให้อัตโนมัติ แต่ถ้ายังมี
process อยู่จะปฏิเสธเพื่อไม่ชนกับ job ที่กำลังทำงาน

ระหว่าง supervision สัญญาณ `SIGINT`, `SIGTERM` และ `SIGHUP` ที่ส่งให้ SRCG จะถูก
forward ไป child จากนั้น SRCG จะรอเก็บ exit status และ cleanup cgroup ก่อนจบ
`SIGKILL` ไม่สามารถดักได้ตาม semantics ของ Linux จึงอาศัย stale recovery ใน run
ครั้งถัดไป

## Source layout

| ไฟล์ | หน้าที่ |
| --- | --- |
| `cgroup_manager.h/.cpp` | cgroup v2 environment, quota, stats, PID attachment และ cleanup |
| `process_supervisor.h/.cpp` | fork/exec barrier, monitoring และ process result |
| `srcg_daemon.h/.cpp` | ประกอบ Linux monitor, cgroup manager และ supervisor |
| `srcg_cli.cpp` | command-line interface |
| `linux_monitor.h/.cpp` | host memory, PSI และ process memory telemetry |
| `thread_manager.h/.cpp` | worker pool ที่ใช้รัน monitoring task |
| `memory_size.h/.cpp` | แปลงหน่วย GB/GiB/MB/MiB เป็น bytes |
| `gpu_monitor.h/.cpp` | optional NVML component; ยังไม่อยู่ใน RAM control path |
| `reservation_manager.h/.cpp` | global Reserved Memory/job/token accounting |
| `reservation_ipc.h/.cpp` | `srcg serve`, Unix socket, peer verification และ event log |
| `srcg_torch/client.py` | PyTorch-side reserve/wait/release client |
| `srcg_torch/checkpoint.py`, `session.py` | early M4 atomic checkpoint/safe point slice |

## Build

```bash
cd /home/achira363/Programming_Langauge_Testing/Cpp/NVML-test

cmake -S . -B build-srcg \
  -DCMAKE_BUILD_TYPE=Debug \
  -DBUILD_TESTING=ON

cmake --build build-srcg --parallel
```

ติดตั้ง Python client เข้า virtual environment เพื่อให้ training script ของผู้ใช้
ที่อยู่นอก repository import ได้:

```bash
.venv/bin/python -m pip install -e . --no-build-isolation
```

ผลลัพธ์หลักคือ:

```text
build-srcg/srcg
```

หาก CMake พบ CUDA Toolkit จะ build `srcg_gpu_monitor` เพิ่ม แต่ binary `srcg`
ไม่ต้องใช้ CUDA/NVML ใน Milestone 1

## ตรวจ cgroup environment

```bash
./build-srcg/srcg check --parent /sys/fs/cgroup/srcg
```

คำสั่งนี้เป็น read-only และไม่สร้าง parent ให้ ผลลัพธ์สำคัญ:

```text
unified_v2=1
memory_controller_available=1
memory_controller_enabled=1
parent_exists=1
parent_writable=1
```

ถ้า parent ยังไม่มี อาจเห็น:

```text
problem=configured srcg parent does not exist
problem=nearest cgroup ancestor is not delegated to the current user
```

นั่นไม่ได้หมายความว่า kernel ไม่รองรับ แต่หมายความว่า user ปัจจุบันไม่มีสิทธิ์
สร้าง/ควบคุม cgroup ใต้ตำแหน่งดังกล่าว

## Initialize parent

ตั้ง parent policy หนึ่งครั้งก่อนรัน job:

```bash
sudo ./build-srcg/srcg init \
  --parent /sys/fs/cgroup/srcg \
  --parent-memory 12GiB \
  --parent-swap 0 \
  --host-headroom 4GiB
```

`init` เป็นคำสั่งเดียวที่เขียน `parent/memory.max` และ
`parent/memory.swap.max` การเรียกซ้ำด้วยค่าเดิมทำได้แบบ idempotent แต่ถ้า parent
มีค่าแตกต่างอยู่แล้ว SRCG จะปฏิเสธและไม่ overwrite โดยอัตโนมัติ

| Option | ความหมาย |
| --- | --- |
| `--parent` | parent cgroup ที่เก็บ SRCG jobs |
| `--parent-memory` | RAM limit รวมของ parent |
| `--parent-swap` | swap limit รวมของ parent |
| `--host-headroom` | จำนวน RAM นอก parent ที่ใช้ตรวจความสมเหตุสมผลตอน `init` |

`host-headroom` ของ `init` ใช้ตรวจ configuration ตอน initialize และไม่ใช่ RAM
reservation มันตรวจทั้ง host `MemTotal` และ `memory.max` ของ finite ancestors
หลัง initialize แล้วค่าใน cgroup filesystem เป็น source of truth ของ parent limit
ส่วน `srcg serve --host-headroom` เป็น dynamic admission threshold ที่ใช้ค่าต่ำสุด
ระหว่าง host `MemAvailable` กับพื้นที่ว่างของ finite ancestors ตอนมี reserve request

## เปิด Reserved Memory manager

Milestone 3 ใช้ shared manager หนึ่งตัวต่อ parent:

```bash
sudo ./build-srcg/srcg serve \
  --parent /sys/fs/cgroup/srcg \
  --socket /run/srcg-manager.sock \
  --reserved-memory 2GiB \
  --checkpoint-budget 512MiB \
  --host-headroom 1GiB \
  --log srcg-logs/reservations.log
```

`serve` อยู่ foreground และไม่พิมพ์ telemetry ลง training terminal ส่วน
reservation events ไปที่ `--log` เมื่อมีหลาย jobs ทุก job ต้องต่อ socket ตัวเดียวกัน
เพื่อให้เห็น committed capacity ก้อนเดียวกัน

ตั้งแต่ Phase 2 ค่า estimate จาก PyTorch ไม่ถูกหักจาก Reserved Memory ตรง ๆ
manager อ่าน `memory.current` และ `memory.stat:inactive_file` ของ job แล้วใช้
headroom ที่มีอยู่ก่อน token/log จึงแยก `estimated_bytes` (RAM ที่คาดว่าจะใช้)
กับ `borrowed_bytes` (quota ที่เพิ่มจริง) Host headroom ยังตรวจ estimate เต็มก้อน
รวม sibling usage ผ่าน ancestor cgroup และ `memory.max` ยังเป็น hard boundary
เหมือนเดิม รายละเอียดสูตรและผล full run อยู่ที่
[docs/milestone-3.md](docs/milestone-3.md)

เมื่อใช้ PyTorch `DataLoader` หลาย worker framework จะ RESERVE ก่อนอ่าน batch,
SHRINK หลัง decode/resize/collate และ RELEASE หลัง optimizer step โดยอัตโนมัติ
manager นับสิทธิ์ที่ workers อื่นยัง allocate ไม่ครบก่อนลด limit ค่า WAIT ไม่มี
deadline โดย default เพราะ resource contention อาจนานกว่าระยะเวลาคงที่; ตั้ง
`--reserve-timeout-ms` เฉพาะเมื่อต้องการ operational deadline

## รัน job

ดู lifecycle และตัวอย่างทุกกรณีได้ที่
[docs/run-scenarios.md](docs/run-scenarios.md) หรือดู syntax จาก binary ด้วย
`./build-srcg/srcg run --help`

ตัวอย่าง smoke test:

```bash
sudo ./build-srcg/srcg run \
  --parent /sys/fs/cgroup/srcg \
  --job training-A \
  --memory 2GiB \
  --swap 0 \
  -- /bin/sh -c 'sleep 5; exit 0'
```

ความหมายของ options:

| Option | ความหมาย |
| --- | --- |
| `--parent` | parent cgroup ที่เก็บ SRCG jobs |
| `--job` | ID ของ job; รองรับตัวอักษร ตัวเลข `_`, `-`, `.` |
| `--memory` | fixed RAM quota ของ job |
| `--swap` | fixed swap quota ของ job |
| `--interval-ms` | sampling interval ของ SRCG log; default 500 ms |
| `--log FILE` | เขียน SRCG telemetry ลง text file; ถ้าไม่ใส่จะไม่เก็บ |
| `--manager-socket PATH` | register base quota และเปิด batch-aware reservation |
| `--checkpoint-dir PATH` | ส่ง checkpoint directory ให้ cooperative client |
| `--max-restarts N` | early M4: จำกัดจำนวน restart จาก checkpoint |
| `--` | สิ้นสุด SRCG options; หลังจากนี้คือ program และ arguments |

หน่วยที่รองรับ:

```text
B, KiB, MiB, GiB
KB, MB, GB
```

IEC และ decimal มีค่าไม่เท่ากัน เช่น `1GiB = 1,073,741,824 bytes` แต่
`1GB = 1,000,000,000 bytes`

## Training output และ SRCG log

`srcg run` ไม่พิมพ์ lifecycle หรือ memory telemetry ลง stdout/stderr โดย default
ดังนั้น output ที่ terminal เป็นของ training program เท่านั้น ส่วน error ที่ทำให้
SRCG launch/supervise ไม่ได้ยังออก stderr ตามปกติ

ถ้าต้องการเก็บ telemetry ให้ระบุ text file ด้วย `--log`:

```bash
sudo ./build-srcg/srcg run \
  --parent /sys/fs/cgroup/srcg \
  --job training-A \
  --memory 2GiB \
  --swap 0 \
  --interval-ms 500 \
  --log srcg-logs/training-A.log \
  -- python3 train.py
```

SRCG เปิดไฟล์แบบ append และเขียน event หนึ่งบรรทัดต่อ sample:

```text
event=memory_sample monotonic_time_ms=... job=training-A \
memory_current_bytes=... memory_peak_bytes=... memory_max=... \
host_mem_available_bytes=... host_swap_free_bytes=... \
host_psi_some_avg10=... host_psi_full_avg10=... oom=... oom_kill=...
```

เมื่อ job จบ SRCG เขียนผลสุดท้ายลงไฟล์เดียวกัน:

```text
event=job_exit job=training-A pid=... classification=completed \
exit_code=0 signal=none cgroup_cleaned=1
```

`monotonic_time_ms` ใช้เรียงลำดับและคำนวณช่วงเวลา ไม่ใช่ wall-clock timestamp
ไฟล์นี้มีเฉพาะ SRCG telemetry ไม่รวม stdout/stderr ของ training process

## Status และ manual cleanup

ระหว่าง job ทำงาน เปิดอีก terminal แล้วเรียก:

```bash
sudo ./build-srcg/srcg status \
  --parent /sys/fs/cgroup/srcg \
  --job training-A
```

หากมี cgroup ค้างจาก supervisor crash:

```bash
sudo ./build-srcg/srcg cleanup \
  --parent /sys/fs/cgroup/srcg \
  --job training-A
```

คำสั่งจะปฏิเสธ cleanup หาก job ยังมี process อยู่

## Tests

### Unit tests

```bash
ctest --test-dir build-srcg --output-on-failure
```

ชุดทดสอบประกอบด้วย:

- memory-unit parsing, Linux memory/PSI parsing และ thread pool
- cgroup file parsing, job ID validation และ cleanup guard
- fork/exec result และ cgroup OOM event classification
- Reserved Memory accounting, concurrency และ Unix-socket protocol
- atomic checkpoint, RNG restore และ Python WAIT/retry client
- real-kernel cgroup integration test

Integration test จะ skip หากไม่ได้กำหนด delegated parent

### Real cgroup integration

รันจาก host terminal ที่ `/sys/fs/cgroup` เป็น read-write:

```bash
sudo env SRCG_TEST_CGROUP_PARENT=/sys/fs/cgroup/srcg-integration \
  ./build-srcg/srcg_cgroup_integration_tests
```

ใน Fish ตรวจ exit status ด้วย:

```fish
echo $status
```

ต้องได้ `0` Test นี้สร้าง process พร้อม worker เพื่อตรวจ inheritance แล้วสร้าง
allocation ที่เกิน quota เพื่อตรวจ `oom_kill` จริง

หลังทดสอบ:

```bash
sudo rmdir /sys/fs/cgroup/srcg-integration
```

## Permission และ delegation

การเขียน cgroup ต้องใช้ root หรือ cgroup parent ที่ administrator/service manager
delegate ให้ user ที่รัน SRCG ตัวโปรแกรมจะไม่เรียก `sudo`, ไม่เปลี่ยน owner และ
ไม่แก้ systemd configuration เอง หากสิทธิ์ไม่พอจะหยุดพร้อมระบุ control file ที่
เขียนไม่ได้

สำหรับการทดลองใช้ `sudo` ได้ตามตัวอย่าง ส่วน deployment จริงควรสร้าง parent
cgroup และกำหนด delegation ให้ service account แทนการรัน training workload เป็น
root

## Safety invariants

- `srcg init` ไม่ overwrite parent ที่มี limits แตกต่างอยู่แล้ว
- `srcg run` อ่าน parent limits เท่านั้นและไม่มีสิทธิ์แก้ parent configuration
- ไม่ `exec` workload ก่อน attach PID เข้า cgroup สำเร็จ
- ไม่สร้าง job จาก ID ที่มี slash, path traversal หรืออักขระนอก whitelist
- ไม่ตั้ง job RAM quota สูงกว่า parent RAM limit
- ไม่ตั้ง parent limit ที่ละเมิด configured host หรือ ancestor-cgroup headroom
- ไม่ลบ cgroup ที่ยัง populated หรือยังมี PID
- reuse job ID ได้โดย reclaim เฉพาะ cgroup เก่าที่ empty แล้ว
- forward termination signal แล้ว reap child/cleanup ก่อน supervisor จบ
- ไม่ตีความ quota ว่าเป็น RAM reservation
- ไม่ใช้ `VmSize - (MemAvailable + SwapFree)` เป็น hard safety decision
- ไม่พึ่ง NVML สำหรับ RAM-only control path

## เอกสารเพิ่มเติม

- ภาพรวมระบบ, flows, modules และคำศัพท์:
  [docs/project-architecture-and-glossary.md](docs/project-architecture-and-glossary.md)
- คู่มือ usage และ lifecycle ทุกกรณี: [docs/run-scenarios.md](docs/run-scenarios.md)
- รายละเอียด implementation แบบย่อ: [docs/milestone-1.md](docs/milestone-1.md)
- สถานะและ API contract ของ Milestone 2: [docs/milestone-2.md](docs/milestone-2.md)
- PyTorch integration และผลทดลอง Milestone 3: [docs/milestone-3.md](docs/milestone-3.md)
- Periodic checkpoint, checkpoint RAM budget และผล resume:
  [docs/periodic-checkpoint.md](docs/periodic-checkpoint.md)
- ที่มา หลักฐาน และสถานะของ design decisions: [docs/decision-trace.md](docs/decision-trace.md)
