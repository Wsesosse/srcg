# SRCG Milestone 1: คู่มือการใช้งานตามสถานการณ์

เอกสารนี้อธิบาย behavior ที่ implement อยู่จริงใน Milestone 1 ตั้งแต่สร้าง parent
จน process จบ รวมทั้งกรณี rerun, process ค้าง, signal, OOM และ DataLoader worker

## Mental model

SRCG มี configuration สองระดับที่มีอายุไม่เท่ากัน:

```text
ทำครั้งเดียว: srcg init
  สร้าง /sys/fs/cgroup/srcg และตั้งเพดานรวมของทุก job

ทำทุก training run: srcg run
  สร้าง <parent>/<job> ชั่วคราว
    -> ตั้งเพดานของ job
    -> attach Python ก่อน exec
    -> รอ Python จบ
    -> ลบ <parent>/<job> เมื่อ process tree ว่าง
```

- `init` คือการตั้ง parent policy แบบ persistent และเป็นคำสั่งเดียวที่แก้
  `parent/memory.max` กับ `parent/memory.swap.max`
- `run` ไม่ใช่คำสั่ง register job ถาวร หนึ่ง invocation เท่ากับหนึ่ง execution
- `--job` เป็น logical ID ของ execution ไม่ใช่ stored profile
- ดังนั้นทุก invocation ของ `run` ต้องระบุ `--job` และ `--memory` แต่ไม่ต้องระบุ
  `--parent-memory` หรือ `--host-headroom` อีก
- `--parent` เป็นเพียงการเลือก parent path ค่า default คือ
  `/sys/fs/cgroup/srcg` จึงละได้ถ้าใช้ path นี้

ดู syntax จาก binary ได้โดยตรง:

```bash
./build-srcg/srcg run --help
```

## เตรียมครั้งแรก

Build:

```bash
cmake -S . -B build-srcg -DCMAKE_BUILD_TYPE=Debug -DBUILD_TESTING=ON
cmake --build build-srcg --parallel
```

Initialize parent หนึ่งครั้ง:

```bash
sudo ./build-srcg/srcg init \
  --parent /sys/fs/cgroup/srcg \
  --parent-memory 12GiB \
  --parent-swap 0 \
  --host-headroom 4GiB
```

ความหมายของตัวเลขนี้คือ SRCG jobs ทุกตัวรวมกันถูก kernel จำกัดไว้ที่ 12 GiB
ส่วน 4 GiB เป็น validation ตอน `init`: SRCG ยอมรับ configuration ต่อเมื่อ
`parent-memory + host-headroom` ไม่เกินทั้ง host `MemTotal` และ `memory.max` ของ
finite ancestor cgroup ทุกระดับ มันไม่ใช่ dynamic reservation และไม่ตรวจ
`MemAvailable` ใหม่ทุกครั้งที่ `run`

หากเรียก `init` ซ้ำ:

- `parent-memory` และ `parent-swap` ตรงกับ cgroup เดิม: สำเร็จแบบ idempotent
- สองค่านี้ต่างจากของเดิม: ปฏิเสธ ไม่ overwrite parent โดยอัตโนมัติ

`host-headroom` ไม่ได้ถูก persist หลัง validation จึงไม่มีค่าเดิมให้ `init` รอบถัดไป
เปรียบเทียบ มันจะตรวจ configuration ที่ส่งมาใหม่กับ host และ ancestor limits

ตรวจ environment:

```bash
sudo ./build-srcg/srcg check --parent /sys/fs/cgroup/srcg
```

สถานะพร้อมใช้ควรมี `unified_v2=1`, memory controller พร้อม, parent มีอยู่ และ
เขียนได้ การ `check` โดยไม่ใช้ `sudo` อาจรายงาน `parent_writable=0` ทั้งที่คำสั่ง
ซึ่งรันด้วย `sudo` ใช้งานได้ เพราะคำสั่งตรวจสิทธิ์ของ process ปัจจุบัน

## เตรียม Python ใน Fish

ให้ใช้ Python ภายใน venv ด้วย path เดิม อย่าใช้ `realpath` เพราะ
`.venv/bin/python` มักเป็น symlink และการ resolve ไป system interpreter อาจทำให้
Python หา package ใน venv ไม่เจอ:

```fish
set py (pwd)/.venv/bin/python
$py -c 'import torch; print(torch.__version__)'
```

กฎของ `srcg run` คือ `--` ต้องเป็น argument แยกเดี่ยว:

```text
ถูก:   -- $py benchmarks/dataloader_policy/train.py lung
ผิด:   --$py benchmarks/dataloader_policy/train.py lung
```

ทุกอย่างก่อน `--` เป็น option ของ SRCG ทุกอย่างหลัง `--` เป็น program และ
arguments ของ training program

## Scenario 1: รัน Task06_Lung ตามปกติ

เริ่มจาก smoke test ที่เล็กพอให้เห็นผลเร็ว:

```fish
set py (pwd)/.venv/bin/python

sudo ./build-srcg/srcg run \
  --parent /sys/fs/cgroup/srcg \
  --job pytorch-demo \
  --memory 6GiB \
  --swap 0 \
  -- \
  $py benchmarks/dataloader_policy/train.py lung \
  --batch-size 1 \
  --epochs 1 \
  --optimizer adamw \
  --lr 0.0001 \
  --num-workers 2 \
  --device cuda \
  --limit 4 \
  --max-steps 2
```

เมื่อ smoke test ผ่าน ค่อยเอา `--limit` และ `--max-steps` ออกเพื่อรัน dataset และ
จำนวน epoch เต็ม ตัวเลือก `batch-size`, `epochs`, `optimizer`, `lr` และ
`num-workers` เป็นตัวเลือก training ปกติ ไม่ใช่การกำหนด burst pattern

`srcg run` อยู่ foreground และ terminal แสดง stdout/stderr ของ `train.py` เท่านั้น
เมื่อ training จบ `srcg` คืน exit status ของ training แล้ว cleanup job cgroup

## Scenario 2: รัน COCO val2017 resize

```fish
sudo ./build-srcg/srcg run \
  --parent /sys/fs/cgroup/srcg \
  --job coco-resize \
  --memory 4GiB \
  --swap 0 \
  -- \
  $py benchmarks/dataloader_policy/train.py coco \
  --batch-size 32 \
  --epochs 5 \
  --optimizer adamw \
  --lr 0.0003 \
  --num-workers 4 \
  --pin-memory \
  --device cuda
```

COCO workload decode JPEG แล้ว resize เป็น 2D tensor ส่วน Lung โหลด image และ
mask NIfTI แบบ 3D เต็ม volume ก่อน resize จึงสร้าง CPU RAM spike ตามขนาดจริงของ
แต่ละ case

## Scenario 3: ต้องการเก็บ SRCG telemetry

เพิ่ม `--log` ก่อน delimiter `--`:

```fish
sudo ./build-srcg/srcg run \
  --parent /sys/fs/cgroup/srcg \
  --job pytorch-demo \
  --memory 6GiB \
  --swap 0 \
  --interval-ms 250 \
  --log srcg-logs/pytorch-demo.log \
  -- \
  $py benchmarks/dataloader_policy/train.py lung \
  --batch-size 1 \
  --epochs 1 \
  --num-workers 2 \
  --device cuda \
  --max-steps 2
```

- terminal ยังเป็น output ของ training เท่านั้น
- SRCG append `job_start`, memory samples, final snapshot และ `job_exit` ลงไฟล์
- `--interval-ms` คือช่วงห่างของ sample เมื่อเปิด `--log`; default 500 ms
- ถ้าไม่ใส่ `--log` จะไม่มี periodic SRCG telemetry ให้เห็นหรือเก็บย้อนหลัง
- ไฟล์เป็น append-only หากต้องการแยกรอบทดลองให้ใช้ชื่อไฟล์คนละชื่อ

## Scenario 4: ดูสถานะระหว่าง training

ขณะที่ `run` ยังทำงาน ให้เปิดอีก terminal:

```bash
sudo ./build-srcg/srcg status \
  --parent /sys/fs/cgroup/srcg \
  --job pytorch-demo
```

ค่าหลัก:

| Field | ความหมาย |
| --- | --- |
| `memory_current_bytes` | CPU RAM และ page cache ที่ process tree นี้ charge อยู่ |
| `memory_peak_bytes` | peak CPU memory ของ cgroup |
| `memory_max` | เพดาน RAM ของ job |
| `swap_max` | เพดาน swap ของ job |
| `populated` | `1` เมื่อยังมี process ใน cgroup หรือลูกของมัน |
| `process_count` | จำนวน PID ที่อยู่ตรง job cgroup |
| `oom`, `oom_kill` | kernel memory event counters |

หลัง job จบปกติ `status` ควรตอบว่า cgroup ไม่มีอยู่ เพราะ job cgroup ถูก cleanup
แล้ว นั่นเป็น expected behavior ไม่ใช่ข้อมูล job หายผิดปกติ ถ้าต้องการ history ต้อง
เปิด `--log` ตอนรัน

`memory.max` ไม่ควบคุม GPU VRAM และตัวเลขจาก `nvidia-smi` ไม่ได้บอกว่า job cgroup
ยัง active หรือไม่ ให้ใช้ `srcg status`/`cgroup.procs` สำหรับ CPU process tree

## Scenario 5: Training จบ แล้วรัน job ID เดิมอีกครั้ง

ใช้คำสั่ง `run` เต็มคำสั่งอีกครั้งได้ทันที:

```fish
sudo ./build-srcg/srcg run \
  --parent /sys/fs/cgroup/srcg \
  --job pytorch-demo \
  --memory 6GiB \
  --swap 0 \
  -- $py benchmarks/dataloader_policy/train.py lung --epochs 1 --batch-size 1
```

ไม่ต้อง `init` ใหม่และไม่ต้อง `cleanup` ก่อนทุก run เพราะ job configuration ไม่ได้
ถูก register ถาวร มีสองกรณี:

- รอบก่อน cleanup สำเร็จ: SRCG สร้าง job cgroup ใหม่
- รอบก่อนเหลือ empty stale cgroup: SRCG ตรวจว่าไม่มี PID แล้วลบและสร้างใหม่ให้เอง

ค่า `--memory` ของรอบใหม่เปลี่ยนได้ เพราะเป็น quota ของ execution รอบนั้น

## Scenario 6: ใช้ job ID เดียวกันขณะที่รอบเก่ายังทำงาน

SRCG ปฏิเสธด้วย `job is already running` และไม่แตะ process เก่า การใช้ชื่อเดียวกัน
ไม่ใช่คำสั่ง resume, attach หรือ replace

ทางเลือกคือรอรอบเดิมจบ หรือใช้ ID ใหม่:

```text
pytorch-demo-a
pytorch-demo-b
```

## Scenario 7: มี empty stale cgroup หลัง kill/crash

ถ้า cgroup มี `populated=0` และ `cgroup.procs` ว่าง แม้
`memory.current` ยังไม่เป็นศูนย์ SRCG ถือว่าไม่มี process ที่ต้องปกป้อง การ `run`
ด้วย job ID เดิมจะ reclaim cgroup นั้นอัตโนมัติแล้วเริ่ม training รอบใหม่

ดังนั้น stale `/sys/fs/cgroup/srcg/pytorch-demo` ที่ไม่มี PID ไม่ควรบังคับให้ผู้ใช้
cleanup เองอีก

## Scenario 8: กด Ctrl-C หรือส่ง SIGTERM/SIGHUP ให้ SRCG

SRCG forward signal ไปยัง training process โดยตรง รอเก็บ exit status แล้ว cleanup
เมื่อ process tree ว่าง ตัวอย่างผลสำหรับ SIGTERM โดยทั่วไปคือ exit status 143
(`128 + 15`)

ขอบเขตปัจจุบันคือ SRCG forward signal ไป direct child ไม่ได้ broadcast ไปทุก
DataLoader worker เอง ปกติ Python/PyTorch จะปิด workers ตอน process หลักจบ แต่ถ้า
worker ใดรอดอยู่เกิน cleanup timeout 2 วินาที cgroup จะยังอยู่และถือว่า active

## Scenario 9: SRCG ถูก SIGKILL

`SIGKILL` ดักหรือ forward ไม่ได้ และการตายของ supervisor ไม่ได้ทำให้ kernel ลบ
cgroup หรือฆ่า training process อัตโนมัติ:

- ถ้า training/worker ยังรัน: cgroup ยัง active และ run ด้วย ID เดิมถูกปฏิเสธ
- ถ้าทุก process จบภายหลัง: cgroup กลายเป็น empty stale และ run รอบใหม่ reclaim
  ให้อัตโนมัติ

Milestone 1 ยังไม่มี `srcg stop` หากยังมี process ค้างต้องหยุด workload นั้นก่อน
แล้วจึงใช้ `cleanup` หรือรัน ID เดิมใหม่หลัง cgroup ว่าง

## Scenario 10: Direct child จบแต่ DataLoader worker ยังอยู่

SRCG supervises direct child ด้วย `waitpid` แล้วรอให้ process tree ทั้ง cgroup ว่าง
อีกไม่เกิน 2 วินาที ถ้ายังมี worker:

- SRCG ไม่ลบ active cgroup
- job ID เดิมยังใช้ไม่ได้ เพราะช่วยป้องกันไม่ให้ run ใหม่ชน orphan process
- ตรวจ PID ด้วย `srcg status`, หยุด worker จากภายนอก แล้วเรียก `cleanup`

นี่เป็น safety behavior ไม่ใช่การบังคับฆ่า worker อัตโนมัติ

## Scenario 11: Manual cleanup

ใช้สำหรับ empty cgroup ที่ค้างจาก crash เท่านั้น ไม่ใช่ขั้นตอนปกติของทุก run:

```bash
sudo ./build-srcg/srcg cleanup \
  --parent /sys/fs/cgroup/srcg \
  --job pytorch-demo
```

- cgroup ว่าง: ลบได้
- ยัง `populated=1` หรือมี PID: ปฏิเสธด้วย `refusing to remove active cgroup`
- cgroup ไม่มีอยู่แล้ว: รายงานว่าไม่มี cgroup; ไม่จำเป็นต้อง cleanup ซ้ำ

## Scenario 12: Job ชน CPU RAM limit

`--memory 6GiB` ตั้ง `memory.max` ให้ทั้ง process tree ซึ่งรวม Python main process,
DataLoader workers, subprocesses และ page cache ที่ถูก charge ให้ cgroup เมื่อ
kernel ต้อง OOM-kill process จะมี `oom_kill` เพิ่ม

ถ้าเปิด `--log` event สุดท้ายจะถูกจำแนกเป็น `classification=cgroup_oom` และ
process มักคืน status 137 เมื่อถูก SIGKILL Milestone 1 ไม่ restart หรือ restore
checkpoint ให้อัตโนมัติ

## Scenario 13: PyTorch แจ้ง CUDA out of memory

CUDA OOM คนละ resource กับ cgroup CPU memory:

- `memory.max` จำกัด host RAM ไม่ได้จำกัด VRAM
- CUDA OOM มักเป็น exception ของ PyTorch แล้ว process exit nonzero
- `nvidia-smi` ใช้ดู VRAM แต่ไม่แทน `srcg status`
- Milestone 1 ยังไม่มี VRAM quota, CUDA OOM classification หรือ recovery

## Scenario 14: หลาย jobs พร้อมกัน

รันแต่ละ job ในคนละ terminal และใช้ ID คนละชื่อ ทั้งหมดอยู่ใต้ parent เดียวกัน:

```text
/sys/fs/cgroup/srcg/             memory.max = 12 GiB
├── lung-a/                      memory.max = 6 GiB
└── coco-b/                      memory.max = 6 GiB
```

ผลรวมของ job limits เป็น nominal ceilings ไม่ใช่ RAM reservation ปัจจุบัน SRCG
ตรวจเพียงว่า limit ของแต่ละ jobไม่เกิน parent limit ส่วน kernel บังคับ actual usage
รวมไม่ให้เกิน parent `memory.max`

ตัวอย่าง job สองตัวอาจตั้ง 8 GiB ต่อ job ใต้ parent 12 GiB ได้ เพราะทั้งคู่ไม่ได้
จำเป็นต้องใช้ถึงเพดานพร้อมกัน แต่ถ้า actual usage รวมชน 12 GiB จะเกิด contention
หรือ parent-level OOM Milestone 1 ยังไม่มี admission, borrowing หรือ orchestration
เพื่อจัดสรร quota แบบ dynamic และการจำแนก parent-level OOM ยังไม่สมบูรณ์เท่า
job-level OOM เพราะปัจจุบันอ่าน event counter จาก job cgroup

## Scenario 15: DataLoader workers และ prefetch

SRCG attach Python main process เข้า cgroup ก่อน `exec` ดังนั้น worker ที่
DataLoader สร้างภายหลัง inherit cgroup เดียวกันโดยอัตโนมัติ:

- `--num-workers N` เพิ่ม process ที่โหลดข้อมูลพร้อมกัน
- `--prefetch-factor P` ทำให้มีได้ประมาณ `N * P` batches in flight
- `--pin-memory` เพิ่ม pinned CPU memory ก่อนส่งเข้า GPU
- `--persistent-workers` ทำให้ workers อยู่ข้าม epoch

ทั้งหมดนี้นับรวมใน `memory.max` จึงอาจทำให้ Lung ที่ใช้ batch ใหญ่และหลาย workers
ชน CPU quota ก่อนเห็น `step_end` ได้ นั่นเป็น behavior ของ workload จริง ไม่ใช่
parser ค้าง สำหรับ baseline ให้เริ่ม Lung ที่ `batch-size=1`, `num-workers=2`
แล้วค่อยเพิ่มทีละตัวแปร

## Scenario 16: Policy planning debug

training ปกติไม่ต้องกำหนด burst pattern หากต้องการดู estimate ของ batch ก่อน
DataLoader โหลดจริงจึงค่อยเปิด instrumentation:

```fish
sudo ./build-srcg/srcg run \
  --parent /sys/fs/cgroup/srcg \
  --job lung-policy-debug \
  --memory 6GiB \
  --swap 0 \
  -- \
  $py benchmarks/dataloader_policy/train.py lung \
  --batch-size 1 \
  --epochs 1 \
  --num-workers 0 \
  --policy-debug
```

`--policy-debug` emit `batch_plan` และบังคับ `num-workers=0` เพื่อให้ลำดับ log
อ่านง่าย ส่วน normal reservation mode ขอ token ภายใน worker ก่อน decode และรองรับ
หลาย in-flight batches แล้ว

ดูแผนโดยไม่โหลด tensor และไม่ train:

```fish
$py benchmarks/dataloader_policy/train.py lung --plan-only --num-workers 0
```

## Scenario 17: Command syntax ผิด

ตัวอย่างที่ผิด:

```fish
sudo ./build-srcg/srcg run --$py benchmarks/dataloader_policy/train.py lung
```

เหตุผลมีสองข้อ: ไม่มี required job options และเอา delimiter `--` ไปติดกับ path
รูปแบบที่ถูกต้องต้องเป็น:

```text
srcg run [SRCG OPTIONS] -- PROGRAM [PROGRAM OPTIONS]
```

เช่น:

```fish
sudo ./build-srcg/srcg run \
  --job pytorch-demo \
  --memory 6GiB \
  --swap 0 \
  -- $py benchmarks/dataloader_policy/train.py lung --batch-size 1
```

อย่าใส่ `--parent-memory` หรือ `--host-headroom` ใน `run`; สองค่านี้เป็นของ
`init` เท่านั้นและ binary จะปฏิเสธเพื่อป้องกัน parent ถูกแก้โดยไม่ตั้งใจ

## Scenario 18: Program เริ่มไม่ได้หรือ training error

| อาการ | ผล |
| --- | --- |
| หา PROGRAM ไม่เจอ/exec ไม่ได้ | child exit 127 |
| training argument ผิด | `train.py` แสดง argparse error และ exit nonzero |
| dependency หรือ dataset หาย | `train.py` แสดง error และ exit 1 |
| training exit 0 | `srcg run` exit 0 |
| training exit code N | `srcg run` คืน N |
| training ถูก signal N | `srcg run` คืน `128 + N` |

ใน Fish ดู exit status ล่าสุดด้วย:

```fish
echo $status
```

ไม่ใช่ `echo $?` ซึ่งเป็น syntax ของ Bash

## Scenario 19: Swap policy

- `--swap 0`: job ใช้ swap ไม่ได้
- `--swap max`: ไม่มี child-level swap ceiling แต่ยังถูก ancestor/parent จำกัด
- ไม่ใส่ `--swap`: default เป็น `0`
- job swap limit ที่มากกว่า finite parent swap limit จะถูกปฏิเสธ

## สิ่งที่ `run` ยังไม่ทำใน Milestone 1

- ไม่จำค่า `--memory` เป็น profile สำหรับครั้งถัดไป
- ไม่ detach เป็น background daemon; หนึ่ง `run` supervise หนึ่ง execution
- ไม่ stop/kill active job ผ่าน CLI
- ไม่ restart training หรือ restore checkpoint
- ไม่ reserve/borrow RAM ระหว่าง batch
- ไม่ควบคุม VRAM
- `run` เองไม่ทำ dynamic admission จาก `MemAvailable` ณ ตอนเริ่ม job; dynamic
  host/ancestor check อยู่ใน `srcg serve` ตอน PyTorch ขอ batch reservation
- ไม่ orchestrate job neighbors หรือปรับ burst pattern ให้กันและกัน

สิ่งเหล่านี้คือ boundary สำหรับ Milestone ถัดไป ไม่ควรตีความว่า Milestone 1 ทำแล้ว

## Troubleshooting map

| ข้อความ/อาการ | ความหมาย | ทำอะไรต่อ |
| --- | --- | --- |
| `unknown run option: --/path/to/python` | เขียน `--$py` ติดกัน | เปลี่ยนเป็น `-- $py` |
| `job is already running` | ยังมี process ใน job ID นั้น | ใช้ `status`; รอหรือหยุด process เก่า |
| `cgroup does not exist` หลัง run จบ | cleanup สำเร็จแล้ว | ไม่ต้องทำอะไร; ใช้ `--log` ถ้าต้องการ history |
| `refusing to remove active cgroup` | cleanup ถูกสั่งตอนยังมี PID | ห้ามลบ; หยุด workload ก่อน |
| `job memory limit exceeds srcg parent limit` | quota ของ job ใหญ่กว่า parent | ลด `--memory` หรือเปลี่ยน parent policyอย่างตั้งใจ |
| `parent cgroup already exists with different limits` | เรียก `init` ด้วยค่าต่างจากเดิม | ตรวจ policy เดิมก่อน; SRCG ไม่ overwrite ให้ |
| `parent is not writable` | user ปัจจุบันไม่ได้รับ delegation | ใช้สิทธิ์ที่ถูกต้องหรือให้ administrator delegate |
| process จบ 137 และ `oom_kill` เพิ่ม | CPU cgroup OOM | ลด batch/workers/prefetch หรือเพิ่ม quotaอย่างตั้งใจ |
| PyTorch พิมพ์ `CUDA out of memory` | VRAM ไม่พอ | ลด GPU batch/model; cgroup RAM limitไม่แก้ VRAM |
| Lung นานก่อน step แรก | workers กำลังอ่าน/decompress full 3D volumes | เริ่มด้วย batch 1, limit 4, max-steps 2 |
