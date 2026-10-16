# Periodic checkpoint at a training safe point

## คำที่ใช้

- **Step:** โหลด batch หนึ่งชุด, คำนวณผลและ gradient, แล้วเรียก
  `optimizer.step()` หนึ่งครั้ง
- **Safe point:** หลัง step จบสมบูรณ์และก่อนเริ่ม step ถัดไป โมเดลกับ optimizer
  อยู่ในสถานะที่ save ร่วมกันได้
- **Checkpoint:** ไฟล์สถานะสำหรับเริ่มฝึกต่อ มี model, optimizer, ลำดับ batch
  ที่ใช้ไปแล้ว และสถานะการสุ่ม ไม่ใช่แค่ไฟล์น้ำหนักโมเดล
- **X:** จำนวน steps ระหว่าง checkpoint สองครั้ง `X=0` ปิด periodic save
- **Checkpoint budget:** สิทธิ์เพิ่มเพดาน RAM ของ job ชั่วคราวตอนสร้าง checkpoint
  ไม่ใช่การจอง RAM จริงและไม่ใช่ขนาดไฟล์ checkpoint บน disk

## พฤติกรรมที่มีอยู่

`--checkpoint-every-steps X` save ที่ safe point หลัง step หมายเลข X, 2X, 3X...
ถ้าเกิด SIGTERM/SIGINT/SIGHUP จะตั้ง flag แล้ว save ที่ safe point ถัดไป ไม่เรียก
PyTorch จาก signal handler ถ้า process ถูกฆ่ากลาง step จะใช้ checkpoint ล่าสุด
และต้องทำ steps หลัง checkpoint นั้นใหม่

เมื่อ training ต่อผ่าน `srcg serve --checkpoint-budget`, client ขอ
`CHECKPOINT_BEGIN` ก่อนสร้าง payload/เขียนไฟล์ manager เพิ่ม `memory.max` ของ job
จาก checkpoint budget ก่อนตอบ `GRANTED` และให้ได้ทีละ job หลัง save (สำเร็จหรือ
ล้มเหลว) client ส่ง `CHECKPOINT_END` manager ลด `memory.max` เฉพาะเมื่อ
`memory.current` รวมสิทธิ์ของ batch ที่ worker กำลังโหลดอยู่ต่ำพอ ถ้ายังลดไม่ได้
จะถือสิทธิ์ไว้และใช้ซ้ำกับ checkpoint ครั้งถัดไป การเพิ่มเพดานนี้ไม่เพิ่ม
`memory.max` ของ parent

ไฟล์ใหม่ถูกเขียนเป็น temporary file และเปลี่ยน `latest.json` หลังเขียนและ
ตรวจ checksum เสร็จ หาก save ล้มเหลว manifest เดิมยังชี้ไปยังไฟล์ที่สมบูรณ์
การ resume ด้วย `--resume auto` โหลด checkpoint ล่าสุดแล้วเริ่ม batch ถัดไป
ผู้ใช้เป็นคนสั่ง resume เองตามแนวทางปัจจุบัน ไม่ต้องเปิด auto restart

## การใช้งาน

ตั้ง checkpoint budget ครั้งเดียวตอนเปิด manager เช่น:

```bash
./build-srcg/srcg serve \
  --parent /sys/fs/cgroup/srcg \
  --socket /run/srcg-manager.sock \
  --reserved-memory 1924MiB \
  --checkpoint-budget 256MiB \
  --host-headroom 1GiB \
  --log srcg-logs/reservations.log
```

ในคำสั่งฝึกปกติให้เพิ่ม `--checkpoint-dir DIR --checkpoint-every-steps 5`
และให้ `srcg run` รับ `--checkpoint-dir DIR` ด้วย โดยค่า `DIR` ต้องเป็นที่เดียวกัน
เมื่อจะเริ่มต่อ ให้รันคำสั่งเดิมอีกครั้งโดยเพิ่ม `--resume auto` หากใช้
`srcg run --checkpoint-dir`, ตัว launcher ส่งค่า resume นี้ให้ benchmark ผ่าน
environment อยู่แล้ว `--max-restarts` มีค่าเริ่มต้น 0 จึงไม่ restart เอง

ควรตั้ง budget จาก RAM ที่เพิ่มขึ้นจริงระหว่าง save พร้อมเผื่อความแปรปรวนของ
workload ค่า 256 MiB ด้านล่างเป็นค่าทดลองของ Lung ไม่ใช่ default สำหรับทุกโมเดล
ถ้า manager ไม่มี checkpoint budget แต่เปิด periodic save พร้อม manager การ save
จะหยุดพร้อม error `checkpoint_budget_zero` ก่อนสร้าง checkpoint

## ผลทดลองเบื้องต้น: Task06_Lung

วันที่ 16 ตุลาคม 2026, 3D U-Net base 32, Task06_Lung 63 volumes, batch 1,
workers 4, parent 3840 MiB, checkpoint budget 256 MiB, CUDA หนึ่ง epoch
ทุกแถวใช้ training config และ seed เดียวกัน และจบ 63/63 steps โดย OOM=0

| X | Checkpoints | เวลาฝึกที่ log รายงาน | เวลาเขียนไฟล์รวม | Final model SHA-256 |
| ---: | ---: | ---: | ---: | --- |
| 0 | 0 | 47.808 s | 0 | `216b2f43…891690` |
| 10 | 6 | 48.306 s | 3.254 s | `216b2f43…891690` |
| 5 | 12 | 50.312 s | 6.197 s | `216b2f43…891690` |
| 5 หลังแก้ safe release | 12 | 51.058 s | 6.359 s | `216b2f43…891690` |
| 5 โค้ดล่าสุด | 12 | 47.252 s | 6.340 s | `216b2f43…891690` |

เวลาเขียนไฟล์รวมไม่ใช่ overhead ทั้งหมดที่บวกตรง ๆ กับเวลาฝึก เพราะ worker
อาจเตรียม batch ซ้อนกับช่วง save ได้ รอบละหนึ่งครั้งและรันเรียงกันบน boot เดียว
จึงยังใช้ตัดสินว่า X=5 หรือ X=10 คุ้มกว่ากันในเชิงสถิติไม่ได้ ต้องสลับลำดับ
และทำซ้ำหลายรอบถ้าจะอ้างเป็นผล thesis

รอบ X=5 โค้ดล่าสุดอยู่ที่
`artifacts/milestone4/periodic-checkpoint/srcg-x5-final-1/`:
parent peak 3,745,746,944 bytes และ OOM-kill 0; checkpoint admission สำเร็จ
12 ครั้ง ส่วนรอบหลังแก้ safe release แต่ก่อนแยก token เก็บไว้ที่
`artifacts/milestone4/periodic-checkpoint/srcg-x5-safe-release-1/`
รอบ X=0/5/10 แรกอยู่ใต้ `artifacts/milestone4/periodic-checkpoint/`

ทดสอบการกู้จากความล้มเหลวด้วยการให้ process จบด้วย exit 75 หลัง save step 10
แล้วผู้ใช้รัน resume เองจาก `latest.json` งานทำ steps 11–63 จบ และ final model
SHA-256 ตรงกับการฝึกต่อเนื่องทุกแถวข้างบน ผลซ้ำบนโค้ดหลังแก้ token transfer
อยู่ที่ `artifacts/milestone4/periodic-checkpoint/fault-step10-final-1/`
ซึ่งบันทึก attempt แรกและ checkpoint step 10; resume ตรวจจาก output ของคำสั่ง
ด้านล่าง ได้ `processed_steps=53`, `resumed=true` และ final SHA-256
`216b2f43bdeee6b888a5b1ce5a3f5695ad5bfa7b2d04cb9f598a9a4ad6891690`

```bash
.venv/bin/python benchmarks/dataloader_policy/train.py lung \
  --batch-size 1 --epochs 1 --optimizer adamw --lr 0.0001 \
  --num-workers 4 --prefetch-factor 2 --volume-size 32 128 128 \
  --device cuda \
  --checkpoint-dir artifacts/milestone4/periodic-checkpoint/fault-step10-final-1/checkpoints \
  --resume auto --checkpoint-every-steps 0
```
การ resume รอบนี้รันนอก SRCG cgroup เพื่อทดสอบความถูกต้องของสถานะ training;
ยังไม่ได้พิสูจน์การ resume หลัง CUDA OOM หรือการกู้ข้าม reboot แบบครบคู่

## ขอบเขตที่ยังเหลือ

- CUDA OOM กลาง step ไม่สามารถ save ณ จุดนั้นได้ ต้องใช้ checkpoint ล่าสุด
- การวัด RAM เพิ่มเฉพาะช่วง save ยังไม่มี peak sampler แยกจาก worker prefetch;
  ค่าที่มีคือ parent peak ทั้ง run และ `memory.current` ตอน BEGIN/END
- ยังไม่มีการตรวจ disk free ก่อน save; ถ้า disk เต็ม atomic store จะเก็บ
  checkpoint เก่าไว้ แต่การ save ใหม่ล้มเหลว
- manager ยังไม่มี journal สำหรับกู้ accounting หลัง manager crash
