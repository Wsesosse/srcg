# รายงานกราฟผลทดลอง SRCG

> อ่านหัวข้อ **คำศัพท์และ Definition** ก่อนดูตัวเลขหรือกราฟ ทุกคำเทคนิคบนกราฟ
> ใช้ความหมายตามตารางนี้ ไม่ใช่ความหมายที่ผู้อ่านต้องเดาเอง

## 1. คำศัพท์และ Definition

| คำที่ใช้ | Definition ในรายงานนี้ |
|---|---|
| `framework` | ชุดโปรแกรมและ API ที่ช่วยควบคุมวิธีรันงาน; SRCG ไม่ได้แทน model หรือ training algorithm ของ PyTorch |
| `PyTorch` | library สำหรับสร้าง model, โหลด data, คำนวณ gradient และปรับ parameters ระหว่าง training |
| `SRCG` | framework ที่ครอบ PyTorch เพื่อควบคุม host RAM ด้วย cgroup และหน่วงการสร้าง batch เมื่อ RAM ไม่พอ |
| `resource` | สิ่งที่มี capacity จำกัดและโปรแกรมใช้ เช่น CPU time, host RAM หรือ GPU memory |
| `process` | โปรแกรมหนึ่ง instance ที่ Linux กำลังรันและมี process ID ของตัวเอง |
| `process tree` | main process และ child processes ทั้งหมดที่มันสร้างต่อกันเป็นลำดับ |
| `CPU` | หน่วยประมวลผลหลักของเครื่อง; preprocessing และ DataLoader มักทำงานฝั่งนี้ |
| `GPU` | หน่วยประมวลผลแบบขนานที่ใช้เร่ง model training และมี VRAM ของตัวเอง |
| `RAM` | หน่วยความจำหลักที่ CPU และ Linux processes ใช้; ข้อมูลหายเมื่อปิดเครื่อง |
| `CUDA` | ระบบ software ของ NVIDIA ที่ PyTorch ใช้คำนวณบน GPU และจัดการ GPU memory |
| `tensor` | array หลายมิติที่ PyTorch ใช้เก็บ input, parameter, activation และ gradient |
| `allocator` | ส่วนของ runtime ที่ขอ คืน และ cache memory ให้ tensors หรือ objects |
| `cache` | memory ที่ยังถือไว้เพื่อใช้ซ้ำให้เร็วขึ้น แม้ข้อมูลเดิมอาจไม่ถูกใช้งานแล้ว |
| `dataset` | ชุด samples ทั้งหมดที่ใช้ใน workload เช่นภาพ COCO หรือ CT volumes ของ Lung |
| `sample` | ข้อมูลหนึ่งรายการจาก dataset เช่นภาพหนึ่งภาพหรือ CT volume หนึ่งชุด |
| `training` | กระบวนการให้ model คำนวณ loss, gradient และปรับ parameters ซ้ำหลาย steps |
| `training step` | หนึ่งรอบที่ใช้ batch ทำ forward, คำนวณ loss/backward และให้ optimizer ปรับ parameters |
| `epoch` | การที่ training ใช้ samples ตามแผนจนครบ dataset หนึ่งรอบ |
| `optimizer` | algorithm ที่ใช้ gradient ปรับ model parameters เช่น SGD หรือ AdamW |
| `gradient` | ค่าที่บอกทิศทางและขนาดการเปลี่ยน parameter เพื่อลด loss |
| `activation` | tensor ชั่วคราวที่ layer ของ model สร้างระหว่าง forward/backward |
| `loss` | ตัวเลขที่วัดความผิดพลาดของ prediction และเป็นเป้าหมายให้ optimizer ลดลง |
| `PyTorch baseline / baseline` | training เดิมที่ไม่ใช้ SRCG reservation; อาจยังอยู่ใน cgroup เพื่อให้วัด RAM ภายใต้ boundary เดียวกัน |
| `mode` | วิธีจัด resource ใน scenario เช่น baseline, sequential, fixed quota หรือ SRCG |
| `fixed quota / fixed` | mode ที่ memory limit คงที่และ job ยืม Reserved Memory ไม่ได้ |
| `not_logged / unknown` | artifact รุ่นนั้นไม่ได้บันทึกค่าดังกล่าว จึงไม่เดาหรือแทนด้วยศูนย์ |
| `workload` | งานทดลองหนึ่งชนิด ซึ่งรวม dataset, model, preprocessing และ training configuration |
| `configuration` | ชุดค่าที่กำหนดวิธีรัน เช่น batch size, epochs, optimizer, workers และ memory limit |
| `architecture` | โครงสร้าง layers และการเชื่อมต่อภายใน model ซึ่งกำหนดชนิดและจำนวน parameters |
| `model` | โครงข่ายประสาทที่รับ input แล้วคำนวณ prediction เช่น ResNet-50 หรือ 3D U-Net |
| `model parameter` | ค่าตัวเลขที่ model เรียนรู้ระหว่าง training เช่น weight และ bias |
| `model size` | ในรายงานนี้หมายถึงจำนวน model parameters ไม่ใช่ RAM ทั้งหมดที่ training ใช้ |
| `FP32 weight bytes` | พื้นที่ของ parameters อย่างเดียวเมื่อแต่ละค่าเป็นเลข 32-bit หรือ 4 bytes; ไม่รวม gradient/optimizer/activation |
| `ResNet-50` | โมเดล convolution 2D ที่ใช้กับ COCO ใน full-model run |
| `3D U-Net` | โมเดล segmentation 3D ที่ใช้กับ Task06_Lung ใน full-model run |
| `COCO val2017` | ชุดภาพ JPEG 2D ที่ใช้ทดสอบ decode/resize และ training pipeline |
| `JPEG` | รูปแบบไฟล์ภาพ 2D แบบบีบอัด; ต้อง decode ก่อนจึงได้ pixel array |
| `Task06_Lung` | ชุด CT และ mask 3D ที่ใช้ทดสอบ RAM spike จากการโหลด volume ทั้งก้อน |
| `CT` | Computed Tomography: ภาพทางการแพทย์สามมิติที่เก็บค่าความหนาแน่นของเนื้อเยื่อ |
| `segmentation mask` | array ที่ระบุ class ของแต่ละ pixel/voxel เพื่อใช้เป็น target ของ segmentation model |
| `pixel / voxel` | pixel คือจุดข้อมูลภาพ 2D; voxel คือจุดข้อมูลปริมาตร 3D |
| `RAM spike / peak` | ช่วงสั้นที่การใช้ RAM เพิ่มสูงกว่าปกติ; peak คือค่าสูงสุดที่วัดได้ตลอดช่วง |
| `NIfTI` | รูปแบบไฟล์ medical image 3D; การแตกไฟล์ .nii.gz อาจสร้าง array ใน RAM ขนาดใหญ่ |
| `decode` | อ่านไฟล์บีบอัดแล้วสร้างข้อมูล pixels/voxels จริงใน RAM |
| `resize` | เปลี่ยนขนาด image/volume; ระหว่างทำอาจมี input และ output อยู่ใน RAM พร้อมกัน |
| `batch` | กลุ่ม samples ที่ DataLoader ส่งให้ model ประมวลผลหนึ่ง training step |
| `DataLoader worker` | process ที่ PyTorch ใช้โหลดและ preprocess data; workers หลายตัวอาจใช้ RAM พร้อมกัน |
| `prefetch` | การเตรียม batches ล่วงหน้าก่อน model ขอใช้ ซึ่งเพิ่มจำนวน batches ที่ค้างใน RAM |
| `job` | process tree ของ training หนึ่งงาน รวม main process และ DataLoader workers |
| `concurrent` | หลาย jobs ทำงานในช่วงเวลาเดียวกัน |
| `sequential` | รัน job แรกให้จบก่อนจึงเริ่ม job ถัดไป |
| `scenario` | ชุดเงื่อนไขทดลองหนึ่งแบบ เช่น RAM limit, จำนวน jobs, mode และ workload |
| `trial (n=)` | การทำ scenario ซ้ำหนึ่งรอบ; n คือจำนวนรอบที่นำมารวม |
| `attempt` | การเริ่ม process หนึ่งครั้งใน recovery experiment; งานเดียวอาจมีหลาย attempts |
| `artifact` | ไฟล์หลักฐานจากการรัน เช่น log, summary, memory.events หรือ checkpoint |
| `valid` | รอบที่ไม่มี INVALID.txt และผ่านเงื่อนไขที่กำหนดสำหรับกราฟนั้น |
| `invalid` | รอบที่พบปัญหาการทดลองหรือ implementation จึงเก็บหลักฐานไว้แต่ห้ามใช้สรุปผล |
| `canonical comparison` | ชุดผลหลักที่คัดเฉพาะ modes ซึ่ง workload และเงื่อนไขสำคัญเทียบกันได้ |
| `smoke test` | รอบสั้นเพื่อเช็กว่า code path รันได้ ไม่ใช่ผล performance ของ workload เต็ม |
| `calibration` | รอบทดลองหาค่าหรือ boundary ก่อนเก็บผลจริง ไม่ใช่ thesis timing result |
| `optimization history` | รอบระหว่างพัฒนาเพื่อหาสาเหตุและแก้ failure; ไม่ใช่ independent final trials |
| `cgroup` | กลไก Linux ที่รวม processes เป็นกลุ่มเพื่อวัดและจำกัด resource เช่น RAM |
| `parent cgroup` | cgroup ที่ครอบ training jobs ของ SRCG และกำหนดเพดาน RAM รวมของ jobs เหล่านั้น |
| `outer scope` | cgroup ระดับบนที่ครอบทั้ง SRCG parent, manager และ process จำลองที่อยู่นอก SRCG |
| `headroom` | capacity ที่กันไว้ไม่ให้ training ใช้ เพื่อเหลือให้ OS หรือ process นอก SRCG; ไม่ใช่ RAM ที่ allocate ไว้ล่วงหน้า |
| `Reserved Memory` | quota ส่วนกลางที่ SRCG ให้ jobs ยืมชั่วคราว; เป็นสิทธิ์เพิ่ม memory.max ไม่ใช่การจอง physical RAM |
| `reservation` | การที่ manager อนุมัติสิทธิ์ให้ job เพิ่ม memory.max ชั่วคราวก่อนสร้าง batch |
| `reservation token` | รหัสอ้างอิงสิทธิ์ที่อนุมัติ เพื่อให้ release/shrink สิทธิ์เดิมได้โดยไม่คืน quota ซ้ำ |
| `host RAM` | หน่วยความจำหลักของเครื่องที่ Linux และ CPU processes ใช้ |
| `VRAM / GPU memory` | หน่วยความจำบน GPU; cgroup memory.max ไม่ได้จำกัด resource นี้ |
| `CUDA allocated` | VRAM ที่ PyTorch tensors ใช้งานจริงตาม CUDA allocator ณ peak |
| `CUDA reserved` | VRAM ที่ PyTorch CUDA allocator ถือไว้เพื่อ reuse; รวมพื้นที่ว่างใน cache จึงมักมากกว่า allocated |
| `RSS` | Resident Set Size: RAM ที่ process มี resident อยู่โดยประมาณ; ไม่เท่ากับยอดทั้ง cgroup |
| `memory limit / boundary` | เพดาน RAM ที่ cgroup อนุญาต; allocation ที่ชนเพดานอาจถูก reclaim หรือ OOM-kill |
| `parent peak RAM` | ค่าสูงสุดของ memory.peak ใน parent cgroup ตลอด scenario รวมทุก training jobs ใต้ parent |
| `cgroup memory peak` | ค่าสูงสุดของ RAM ที่ cgroup ของ training log นั้นถูก charge |
| `OOM` | Out Of Memory: ระบบไม่สามารถตอบ allocation ภายใต้ capacity/limit ปัจจุบัน |
| `OOM-kill` | kernel ส่ง SIGKILL เพื่อหยุด process และคืน RAM |
| `signal / SIGKILL` | signal คือข้อความควบคุม process จากระบบ; SIGKILL หยุด process ทันทีและ process ดักเพื่อ save ไม่ได้ |
| `exit status` | ตัวเลขที่ process ส่งเมื่อจบ; 0 หมายถึงสำเร็จตาม convention ส่วนค่าที่ไม่ใช่ 0 หมายถึง error/termination |
| `completed job` | training ทำครบ steps/samples ตาม workload และ process จบด้วย exit status 0 |
| `makespan` | เวลาจากเริ่ม scenario จน process ที่ scenario รออยู่ทั้งหมดจบ ไม่ใช่เวลาของ job เดี่ยวเสมอ |
| `time-to-failure` | เวลาตั้งแต่เริ่มจนงานล้ม; ห้ามตีความว่าสั้นกว่าแล้วเร็วกว่า เพราะไม่ได้ผลลัพธ์สำเร็จ |
| `time-to-result` | เวลาจนได้ผล training ที่สมบูรณ์; ใช้เปรียบเทียบ performance ได้เมื่อทุก mode จบงานเดียวกัน |
| `elapsed time` | เวลาที่ training log หนึ่งตัวใช้ตั้งแต่เริ่มถึงจบการรันนั้น |
| `throughput` | ปริมาณงานที่จบต่อเวลา เช่น samples/second หรือ steps/second |
| `checkpoint` | ไฟล์สถานะ model/optimizer/ตำแหน่ง data ที่เขียนเสร็จแล้ว เพื่อ resume ภายหลัง |
| `resume` | เริ่ม training process ใหม่แล้วโหลด checkpoint เพื่อทำต่อจากสถานะที่บันทึกไว้ |
| `hash / SHA-256` | ค่าลายนิ้วมือของข้อมูล; hash ตรงกันใช้ยืนยันว่า bytes ของ final model ตรงกัน |
| `performance` | เวลาและอัตราการทำงานเมื่อได้ผลลัพธ์ที่ถูกต้อง เช่น time-to-result และ throughput |
| `safety result` | ผลที่ตอบว่างานจบหรือถูก OOM-kill ภายใต้ resource pressure ไม่ใช่คำตอบเรื่องความเร็วเพียงอย่างเดียว |
| `static scheduling` | กำหนด limit หรือลำดับรันไว้ล่วงหน้าโดยไม่ปรับตาม RAM usage ระหว่างทำงาน |
| `randomized order` | สลับลำดับว่า mode ใดรันก่อนในแต่ละ trial เพื่อลดผลจาก cache/อุณหภูมิ/สภาพเครื่อง |
| `mean` | ค่าเฉลี่ยเลขคณิตของ trials ที่เลือก |
| `median` | ค่ากลางหลังเรียงตัวเลข ช่วยลดผลของค่าที่สูงหรือต่ำผิดปกติ |
| `log scale` | แกนที่ระยะเท่ากันหมายถึงการคูณ เช่น 1→10 เท่ากับ 10→100 ไม่ใช่การบวกเท่ากัน |
| `JSONL` | ไฟล์ข้อความที่แต่ละบรรทัดเป็น JSON event หนึ่งรายการจาก training |
| `CSV` | ไฟล์ตาราง comma-separated สำหรับเปิดด้วย spreadsheet หรือวิเคราะห์ต่อ |
| `MiB / GiB` | หน่วยข้อมูลฐาน 1024: 1 MiB = 1,048,576 bytes และ 1 GiB = 1,073,741,824 bytes |
| `raw artifact` | ไฟล์หลักฐานต้นทางที่ยังไม่ถูกรวมค่า เช่น JSONL log หรือ memory.events |
| `performance_summary` | JSON event ท้าย training ที่รวมเวลา, peak memory, throughput และ final model hash |

## 2. ขอบเขตข้อมูลที่อ่านได้

ตัวสร้างกราฟอ่าน raw files ใต้ `artifacts/` และพบ:

- `62` scenario/attempt summaries
- `2` explicitly invalid summaries
- `152` parseable training logs
- `98` logs with `performance_summary`
- `11` training logs below an `INVALID.txt`

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
| `resnet50` | 25,557,032 | 97.5 MiB |
| `unet3d` | 22,578,306 | 86.1 MiB |

ค่า FP32 ในตารางนับเฉพาะ parameter tensors ไม่รวม gradient, optimizer state,
CUDA allocator cache, activation, decoded image/volume, collated batch,
DataLoader workers และ filesystem cache ดังนั้น Task06_Lung ใช้ host RAM สูงจาก
full NIfTI decode/transform ไม่ใช่เพราะ model parameters มากกว่า

## 5. ตัวเลขจาก canonical comparison

| Scenario | Mode | Trials | Completed jobs/trial | Time (s) | Parent peak (GiB) |
|---|---|---:|---:|---:|---:|
| 4 GiB capacity / two Lung jobs | PyTorch concurrent | 2 | 0.0/2 | 16.812 | 4.000 |
| 4 GiB capacity / two Lung jobs | PyTorch sequential | 2 | 2.0/2 | 157.931 | 4.000 |
| 4 GiB capacity / two Lung jobs | SRCG concurrent | 2 | 2.0/2 | 91.578 | 3.601 |
| Late 512 MiB process / 5 GiB outer scope | PyTorch concurrent | 2 | 0.0/2 | 35.064 | 4.499 |
| Late 512 MiB process / 5 GiB outer scope | SRCG + 1 GiB headroom | 2 | 2.0/2 | 111.674 | 3.580 |
| Normal pressure / 8 GiB parent / workers=2 | PyTorch concurrent | 1 | 2.0/2 | 44.409 | 8.000 |
| Normal pressure / 8 GiB parent / workers=2 | SRCG concurrent | 1 | 2.0/2 | 50.582 | 6.183 |

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
