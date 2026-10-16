# SRCG safety evaluation: ป้องกัน training จาก RAM contention

เอกสารนี้ตอบคำถามหลักว่า **เมื่อหลาย process ต้องใช้ RAM พร้อมกัน SRCG ทำให้
training เสร็จแทนการถูก OOM-kill ได้หรือไม่** เวลาที่เพิ่มขึ้นเป็นผลแลกเปลี่ยนที่
รายงาน แต่ไม่ใช่ pass/fail threshold แบบ 1--3%

ทุก run ในเอกสารนี้ใช้ Task06_Lung ครบ 63 volumes ต่อ job, 3D U-Net base 32,
หนึ่ง epoch, CUDA, batch size 1 และ DataLoader defaults ไม่มี `--limit`,
`--max-steps`, `--model tiny` หรือ synthetic allocation ใน training program

## คำที่ใช้ในผลทดลอง

- **Memory contention**: หลาย process ขอ RAM ในช่วงเดียวกันจนความต้องการรวมสูง
  กว่า capacity ที่ Linux อนุญาต
- **OOM**: Out Of Memory คือ Linux หา RAM เพิ่มให้ allocation ไม่ได้
- **OOM-kill**: kernel ส่ง `SIGKILL` ให้ process เพื่อเอา RAM กลับมา exit status
  ที่ shell เห็นมักเป็น 137
- **Completed job**: training ทำครบ 63/63 steps และ process exit 0
- **Makespan**: เวลาตั้งแต่เริ่ม training สองงานจน process ทุกตัวใน scenario จบ
- **Late-arriving sibling**: process ที่อยู่นอก SRCG parent แต่แชร์ RAM boundary
  ระดับบนเดียวกัน มันเริ่มหลัง training 5 วินาที ขอ 512 MiB และถือไว้ 30 วินาที
- **Structural headroom**: ส่วนต่างระหว่าง outer limit 5 GiB กับ SRCG parent
  limit 4 GiB ส่วน 1 GiB นี้เป็น capacity ที่ training ข้ามไปใช้ไม่ได้ จึงเหลือให้
  process นอก parent; Linux ไม่ได้ allocate หรือ lock RAM ก้อนนี้ไว้ล่วงหน้า
- **Ancestor-aware headroom**: SRCG อ่าน `memory.max` และ `memory.current` ของ
  cgroup ระดับบนซึ่งครอบทั้ง training และ process อื่น ถ้า batch ใหม่จะทำให้พื้นที่
  ว่างหลัง allocation ต่ำกว่า headroom ที่ตั้งไว้ SRCG ตอบ WAIT ก่อนโหลด batch
- **WAIT**: SRCG ยังไม่อนุญาตให้สร้าง batch เพราะ Reserved Memory ยังไม่พอ,
  request เก่ากว่าอยู่หน้า FIFO หรือ headroom check ยังไม่ผ่าน training ยังมีชีวิต
  และลอง request เดิมใหม่ภายหลัง

`memory.events:max` นับจำนวนครั้งที่ allocation กำลังจะเกิน hard limit และ kernel
ต้องพยายาม reclaim มันไม่เท่ากับ OOM ส่วน `oom_kill` ที่มากกว่า 0 คือหลักฐานว่า
kernel ฆ่า process จริง

## โครงสร้างของ test

```text
systemd transient scope: memory.max = 5 GiB, swap = 0
├── runner/
│   └── late-arriving 512 MiB process (มีเฉพาะ scenario ที่ 2)
└── srcg parent/
    ├── Lung training A
    └── Lung training B
```

`systemd transient scope` คือ cgroup ชั่วคราวที่ test runner สร้างเพื่อจำลองเครื่อง
หรือ container ที่มี RAM 5 GiB โดยไม่เปลี่ยน quota ของ desktop ทั้งเครื่อง

## Scenario 1: training สองงานแย่ง RAM ใน parent 4 GiB

ทั้ง normal และ SRCG ใช้ boundary เดียวกัน: outer scope 5 GiB, training parent
4 GiB และ swap 0 ความต่างมีเพียง normal ปล่อยให้ DataLoader ของทั้งสองงานโหลดตาม
ปกติ ส่วน SRCG ให้แต่ละ job มี base 1,400 MiB และแชร์ Reserved Memory 1,296 MiB

```text
Normal                            SRCG
A โหลด batch ─┐                   A ขอ RAM ─┐
B โหลด batch ─┴─> รวมเกิน 4 GiB   B ขอ RAM ─┴─> manager grant ทีละจังหวะ
               ↓                                  ↓
kernel OOM-kill งาน              งานที่ยังไม่ได้สิทธิ์ WAIT ก่อน load
               ↓                                  ↓
0/2 jobs เสร็จ                   2/2 jobs เสร็จ
```

| Mode | Trials ที่ได้ผลเหมือนกัน | Completed | Mean makespan | Mean parent peak | OOM kills |
| --- | ---: | ---: | ---: | ---: | ---: |
| Normal | 2/2 | 0/2 jobs ต่อ trial | 16.812 s ก่อนล้ม | 4.000 GiB | 4 processes ต่อ trial |
| SRCG | 2/2 | 2/2 jobs, 126/126 steps | 91.578 s | 3.601 GiB | 0 |

SRCG รอรวมเฉลี่ย 11.821 วินาทีต่อ trial ระหว่างสอง jobs นี่คือกลไกที่เปลี่ยนผลจาก
“จบเร็วเพราะถูกฆ่า” เป็น “ใช้เวลานานกว่าแต่ training เสร็จ” จึงห้ามเอา 16.812 กับ
91.578 วินาทีมาคำนวณเป็น performance slowdown ตรง ๆ เพราะ normal ไม่ได้สร้าง
ผลลัพธ์สำเร็จ

Raw artifacts:

- `artifacts/milestone3/capacity-sweep/4g-normal-{1,2}/`
- `artifacts/milestone3/capacity-sweep/ancestor-aware-srcg-{1,2}/`

`4g-srcg-{1,2}` เป็นรอบก่อนเพิ่ม ancestor-aware admission; มันผ่านเหมือนกันแต่
ไม่ใช่ artifact หลักของ implementation ล่าสุด

## Scenario 2: มี process ใหม่ขอ RAM ระหว่าง train

ทุก run มี outer capacity 5 GiB แล้วเริ่ม process 512 MiB หลัง training 5 วินาที:

```text
Normal: parent = 5 GiB             SRCG: parent = 4 GiB, headroom = 1 GiB

outer 5 GiB                        outer 5 GiB
├── training ใช้ได้ถึง 5 GiB       ├── training ข้าม 4 GiB ไม่ได้
└── newcomer ขอ 512 MiB            └── newcomer ขอ 512 MiB
          ↓                                  ↓
ความต้องการรวมชน outer limit       manager เห็น outer current รวม newcomer
          ↓                                  ↓
kernel เลือก OOM victim            WAIT ถ้า batch จะเหลือ outer RAM < 1 GiB
          ↓                                  ↓
training ตาย 2 งาน                 ทั้ง training 2 งานและ newcomer เสร็จ
```

| Mode | Trials ที่ได้ผลเหมือนกัน | Training completed | 512 MiB process | Mean makespan | Scope OOM kills |
| --- | ---: | ---: | ---: | ---: | ---: |
| Normal, headroom 0 | 2/2 | 0/2 jobs ต่อ trial | completed | 35.064 s ก่อนทุก process จบ | 4 ต่อ trial |
| SRCG, headroom 1 GiB | 2/2 | 2/2 jobs, 126/126 steps | completed | 111.674 s | 0 |

SRCG parent peak เฉลี่ย 3.580 GiB และ outer scope peak เฉลี่ย 3.699 GiB จึงเหลือ
มากกว่า 1 GiB ใต้ outer limit ตอน peak ส่วน WAIT รวมเฉลี่ย 52.814 วินาทีต่อ trial
ในแต่ละ trial manager ตอบ WAIT เพราะ ancestor headroom 407 ครั้ง นอกจากการรอจาก
Reserved Memory และ FIFO training ทั้งสองงานให้ final model SHA256 ตรงกัน:
`3502fe809eb9d75055dab6c4bc5fa0a032bc850a09d711deb3e427c76882767e`

ผล normal ไม่ได้แปลว่า process ที่มาทีหลังจะรอดเสมอ รอบนี้ kernel เลือก training
เป็น OOM victims การเลือก victim เปลี่ยนได้ตาม process state และ kernel policy
ข้อสรุปที่ใช้ได้คือ **ถ้าไม่มี boundary กันพื้นที่ไว้ จะไม่มีหลักประกันว่า process
ใดรอด**

Raw artifacts:

- `artifacts/milestone3/late-arrival/ancestor-aware-normal-{1,2}/`
- `artifacts/milestone3/late-arrival/ancestor-aware-srcg-{1,2}/`

`unprotected-normal-{1,2}` และ `protected-srcg-{1,2}` เป็นรอบก่อนเพิ่ม dynamic
ancestor check เก็บไว้เป็น historical artifacts ไม่ใช่ผลของ implementation ล่าสุด

## Scenario 3: รันทีละงานเทียบกับ SRCG

baseline แบบ sequential รัน Lung A ให้จบก่อนเริ่ม Lung B ส่วน SRCG เริ่มทั้งสองงาน
พร้อมกัน แต่ควบคุมเฉพาะช่วงที่ DataLoader ต้องใช้ RAM ชั่วคราว ทั้งสอง mode ใช้
parent limit 4 GiB, swap 0, training configuration และ source/binary hashes เดียวกัน

| Mode | Completed | Makespan | Parent peak | OOM kills |
| --- | ---: | ---: | ---: | ---: |
| Sequential | 2/2 jobs, 126/126 steps | 170.015 s | 4.000 GiB | 0 |
| SRCG | 2/2 jobs, 126/126 steps | 93.384 s | 3.654 GiB | 0 |

ในคู่นี้ SRCG ใช้เวลาเป็น `0.549×` ของ sequential หรือเร็วขึ้น `1.821×`
(makespan ลด 45.07%) และ parent peak ลด 8.65% manager ให้ 126 grants/126 releases
และ training สองงาน WAIT รวม 11.671 วินาที การ WAIT ไม่ได้เพิ่มตรง ๆ เข้า makespan
ทั้งหมด เพราะ compute และ I/O ของอีก job ยังซ้อนกันได้

quality gate เทียบกับ normal baseline ผ่านแบบ exact ทั้งสอง jobs: config, samples,
63 loss values และ final model SHA-256 ตรงกัน โดย `max_loss_delta=0.0` และ hash คือ
`3502fe809eb9d75055dab6c4bc5fa0a032bc850a09d711deb3e427c76882767e`

ผลนี้เป็น **preliminary boot-separated pair** ไม่ใช่ thesis-level timing conclusion:
มีเพียงหนึ่ง run ต่อ mode และ boot age ไม่ matched กัน โดย sequential เริ่มที่ 7,257
วินาทีหลัง boot (ผู้ใช้ยืนยันว่าไม่ได้เปิด dataset หลัง poweroff) ส่วน SRCG เริ่มที่
82 วินาที ผลรอบถัดไปควรใช้ warm-cache randomized order หลาย repetitions สำหรับ
performance หลัก หรือ matched boot-age หลายคู่ถ้าต้องการอ้าง cold-start โดยตรง

Raw artifacts:

- `artifacts/milestone3/cold-boot/sequential-1/`
- `artifacts/milestone3/cold-boot/srcg-1/`

## Timing matrix: normal pressure และ high pressure

ส่วนนี้แยกคำถามเรื่องเวลาออกเป็นสองกรณี เพื่อไม่เอาเวลาของงานที่ถูก OOM-kill
มาเทียบกับเวลาของงานที่ทำเสร็จ:

1. **Normal pressure:** รัน Lung สองงานพร้อมกันทั้ง normal PyTorch และ SRCG โดยให้
   parent 8 GiB ซึ่งมากพอให้ทั้งสองแบบทำครบ เปรียบเทียบต้นทุนเวลาและ RAM peak
   โดยตรง
2. **High pressure:** ลด parent เหลือ 5 GiB ซึ่ง normal concurrent ทำไม่ครบ แล้ว
   เปรียบเทียบวิธีที่ให้ผลสำเร็จสองวิธี คือ normal PyTorch แบบ sequential กับ
   SRCG แบบ concurrent ผลคู่นี้ต้องเก็บคนละ reboot

Normal-pressure pair แรกใช้ Task06_Lung 63 volumes/job, 3D U-Net base 32,
batch size 1, one epoch, DataLoader workers 2/job, prefetch factor 2, pin memory off,
CUDA, outer scope 9 GiB, parent 8 GiB และ swap 0:

| Mode | Completed | Makespan | Parent peak | OOM kills |
| --- | ---: | ---: | ---: | ---: |
| Normal concurrent | 2/2 jobs, 126/126 steps | 44.409 s | 8.000 GiB | 0 |
| SRCG concurrent | 2/2 jobs, 126/126 steps | 50.582 s | 6.183 GiB | 0 |

SRCG ใช้เวลาเพิ่ม 6.173 s หรือ 13.90% และลด parent peak 1,951,457,280 bytes
หรือ 22.72% ค่า `memory.events:max` เท่ากับ 1,586 ใน normal และ 94 ใน SRCG;
counter นี้หมายถึงชน hard limit/reclaim ไม่ใช่ OOM ทั้งสองแบบมี `oom=0` และ
`oom_kill=0`

quality gate ผ่านแบบ exact ครบทั้งสี่งาน: 63/63 steps, loss ทุก step ต่างกันสูงสุด
0.0 และ final model SHA-256 ตรงกันที่
`3502fe809eb9d75055dab6c4bc5fa0a032bc850a09d711deb3e427c76882767e`

นี่เป็น paired engineering result หนึ่งรอบ ไม่ใช่ค่าเฉลี่ยเชิงสถิติ และ normal
ถูกรันก่อน SRCG บน boot เดียวกัน จึงควรทำหลาย repetitions พร้อมสลับลำดับก่อนใช้
เป็น thesis performance claim

Raw artifacts:

- `artifacts/milestone3/timing/pressure-normal-1/`
- `artifacts/milestone3/timing/pressure-srcg-1/`

High-pressure timing ใช้ `high-sequential` หนึ่ง boot และ `high-srcg` อีกหนึ่ง boot
ทั้งคู่ใช้ parent 5 GiB, workers 2/job และ training config เดียวกับ
normal-pressure pair การทดลองก่อนหน้าพิสูจน์แล้วว่า normal concurrent
ที่ 5 GiB ทำได้เพียง 69/126 steps และถูก OOM-kill จึงไม่ใช่ตัวเลขเวลาที่นำไปเทียบ
กับ successful run ได้

เก็บ cold-boot sequential baseline แล้วที่ boot age 272 วินาที:

| High-pressure mode | Completed | Makespan | Parent peak | OOM kills | Exact quality gate |
| --- | ---: | ---: | ---: | ---: | --- |
| Normal sequential | 2/2 jobs, 126/126 steps | 81.463 s | 5.000 GiB | 0 | pass |
| SRCG concurrent | รอ boot ถัดไป | — | — | — | — |

sequential job A ใช้ 36.319 s และ job B ใช้ 36.390 s; เวลาที่เหลือใน makespan
คือการสร้าง/ปิด controller และ cgroup ทั้งสองงาน loss ทุก step และ final model
SHA-256 ตรงกับ normal-pressure baseline แบบ exact

Raw artifact ที่เก็บแล้ว:

- `artifacts/milestone3/timing/high-cold-sequential-1/`

cold SRCG attempt แรกไม่ใช่ timing result: ทั้งสอง jobs หยุดที่ step 8 เพราะ
pending-release tokens สองตัวถือ Reserved Memory รวมประมาณ 1.20 GB ขณะที่ workers
ทุกตัวรออยู่ใน RESERVE จึงไม่มี caller เหลือมาสั่ง RETRY รอบนั้นถูก interrupt และ
เก็บไว้ที่ `high-cold-srcg-1-invalid-stall/` พร้อม `INVALID.txt`

หลังแก้ C++ manager ให้ RELEASE คืน quota บางส่วนที่ปลอดภัยทันที และให้ RESERVE
ทุก retry ตรวจ pending releases ของ job อัตโนมัติ warm validation ทำครบ 126/126
steps ใน 95.434 s, parent peak 4.422 GiB, OOM-kill 0 และ exact quality gate ผ่าน
ผลนี้ยืนยัน liveness fix แต่ **ห้ามนำ 95.434 s ไปเทียบกับ cold sequential** เพราะ
dataset cache ถูกใช้แล้ว ต้องเก็บ cold `high-srcg` ใหม่หลัง reboot

Raw validation artifacts:

- `artifacts/milestone3/timing/high-srcg-partial-release-validation-2/`
- `artifacts/milestone3/timing/high-srcg-partial-release-validation-1-invalid-vram/`

invalid-vram รอบแรกเกิดจาก transient unit ของ attempt ที่ interrupt แล้วยังทำงาน
ต่อและถือ VRAM อยู่ experiment runner จึงเพิ่ม signal cleanup ให้หยุดทั้ง unit
เมื่อรับ interrupt

cold attempt ถัดมาพบ test-boundary error อีกจุด: enclosing scope 6 GiB เท่ากับ
parent 5 GiB + protected headroom 1 GiB พอดี แต่ manager/controller/runner อยู่ใน
scope และอยู่นอก parent จึงเหลือ ancestor capacity ขาดประมาณ 136 MiB สำหรับ batch
ใหญ่ ทั้งสอง jobs WAIT อย่างถูกต้องที่ step 8 รอบนี้เก็บเป็น
`pressure-5g-cold-srcg-1-invalid-ancestor-headroom/`

แก้เฉพาะ enclosing scope เป็น 7 GiB โดย training parent ยังเป็น 5 GiB และ
headroom ยังเป็น 1 GiB warm validation หลังแก้จบ 126/126 steps ใน 86.201 s,
parent peak 5,009,297,408 bytes (4.665 GiB), OOM-kill 0 และ exact quality gate ผ่าน
ที่ `pressure-5g-scope7-validation-1/` เวลานี้ยังไม่ใช่ cold comparison

sequential artifact เดิมใช้ outer 6 GiB แต่ scope peak เพียงประมาณ 5.003 GiB และ
ไม่มี OOM จึงไม่ได้ถูก outer limit บีบ อย่างไรก็ตาม thesis-level repeated trials
ควรเก็บ sequential ใหม่ด้วย outer 7 GiB ให้ metadata ตรงกันทุก field

## วิธีรันซ้ำ

Build ก่อน:

```bash
cmake --build build-srcg --parallel
```

เลือก scenario หนึ่งตัว:

```bash
benchmarks/dataloader_policy/run_safety_experiment.sh capacity-normal
benchmarks/dataloader_policy/run_safety_experiment.sh capacity-srcg
benchmarks/dataloader_policy/run_safety_experiment.sh late-normal
benchmarks/dataloader_policy/run_safety_experiment.sh late-srcg
benchmarks/dataloader_policy/run_safety_experiment.sh pressure-normal
benchmarks/dataloader_policy/run_safety_experiment.sh pressure-srcg
```

high-pressure time-to-success ต้องรันทันทีหลัง reboot และเก็บหนึ่ง mode ต่อ boot:

```bash
# boot แรก
benchmarks/dataloader_policy/run_cold_boot_comparison.sh high-sequential

# reboot อีกครั้ง
benchmarks/dataloader_policy/run_cold_boot_comparison.sh high-srcg
```

สองคำสั่ง normal ถูกคาดว่าจะ exit non-zero เมื่อเกิด OOM แต่ artifact ยังถูกเขียน
ครบ ดูผลรวมที่ `summary.txt`, การจำแนก process ที่ `lung-*-srcg.log`, counters
จาก kernel ที่ `parent-memory.events`/`scope-memory.events` และผล training ที่
`lung-*.jsonl`

## ขอบเขตของข้อสรุป

ผลนี้พิสูจน์ behavior บนเครื่องและ configuration ที่ระบุซ้ำได้สอง trials ต่อ mode
ยังไม่ใช่การรับประกันทุก dataset/host และยังไม่พอสำหรับ confidence interval ใน
เล่ม thesis รอบถัดไปควรเพิ่มจำนวน repetitions, parent capacities และจำนวน jobs
โดยใช้ **completed jobs, OOM-kill count และ time-to-successful-result** เป็น metrics
หลัก แล้วรายงาน makespan/WAIT เป็นต้นทุนของ safety

เวอร์ชันปัจจุบันตรวจ container/systemd ancestor แล้ว: `srcg init` ปฏิเสธ parent
configuration ที่ `parent limit + headroom` เกิน finite ancestor limit และ
`srcg serve` ใช้ค่าพื้นที่ว่างที่ต่ำกว่าระหว่าง host `MemAvailable` กับ
`ancestor memory.max - ancestor memory.current` ตอน admission ทุกครั้ง
