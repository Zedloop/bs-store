# Mini ERP — Ombor va Kassa

Uzum Market do'koningiz uchun oddiy ombor va kassa nazorati. Barcha yozuvlar
qo'lda kiritiladi (kirim/chiqim/samovykup), Uzum API bilan avtomatik
sinxronizatsiya yo'q — bu keyingi bosqich.

## Ishga tushirish

```bash
cd mini-erp
pip install -r requirements.txt
python3 app.py
```

Keyin brauzerda oching: http://localhost:5000

Birinchi ishga tushirishda `erp.db` nomli SQLite fayl avtomatik yaratiladi.

## Tuzilma

```
mini-erp/
  app.py          — backend (Flask) va barcha API endpointlar
  schema.sql      — 3 ta jadval: mahsulotlar, ombor_harakatlari, kassa_harakatlari
  templates/
    index.html    — dashboard (jadvallar + kiritish formalari)
  erp.db          — ma'lumotlar bazasi (birinchi ishga tushirishda yaratiladi)
```

## Ombor mantig'i

- **kirim** — yangi mahsulot omborga qo'shilganda → qoldiq **ortadi**
- **chiqim** — mahsulot Uzum skladiga jo'natilganda → qoldiq **kamayadi**
- **samovykup** — o'zi sotib olingan tovar qaytib kelganda → qoldiq **ortadi**

Kassa esa alohida, oddiy pul oqimi logi — hech qanday avtomatik bog'lanish yo'q.

## Keyingi qadamlar (Claude Code'da davom ettirish uchun)

Bu loyihani Claude Code'ga ochib, quyidagilarni so'rashingiz mumkin:

- Tannarx va foyda hisoboti modulini qo'shish (mahsulotga tannarx maydoni
  qo'shiladi, kassa/ombor harakatlaridan avtomatik foyda hisoblanadi,
  samovykup ajratib ko'rsatiladi)
- Mahsulotlarni tahrirlash/o'chirish
- Sanaga ko'ra filtrlash va hisobot eksport qilish (Excel/PDF)
- Uzum API bilan integratsiya (buyurtmalarni avtomatik o'qish)
- Autentifikatsiya (agar boshqa xodimlar qo'shilsa)

Loyiha oddiy Flask + SQLite ustida qurilgan, shuning uchun Claude Code
bu strukturani tushunib, ustiga osongina qo'shimcha qila oladi.
