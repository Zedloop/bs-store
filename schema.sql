-- Mini ERP uchun boshlang'ich sxema
-- Barcha harakatlar qo'lda kiritiladi (Uzum API bilan avtomatik sinxron emas)

CREATE TABLE IF NOT EXISTS mahsulotlar (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nomi TEXT NOT NULL,
    qoldiq INTEGER NOT NULL DEFAULT 0,
    tannarx REAL NOT NULL DEFAULT 0,
    narx_usd REAL NOT NULL DEFAULT 0,
    kurs REAL NOT NULL DEFAULT 12500,
    sotish_narxi REAL NOT NULL DEFAULT 0,
    logistika REAL NOT NULL DEFAULT 5000,
    boshqa_harajat REAL NOT NULL DEFAULT 100,
    komissiya_foiz REAL NOT NULL DEFAULT 25,
    boost_foiz REAL NOT NULL DEFAULT 0,
    rasm TEXT,
    yaratilgan_sana TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Operatsiyalar: bitta amalda bir nechta mahsulot qo'shilishi mumkin
-- (masalan bitta "Расход" operatsiyasi ichida fleshka HAM mikrofon bo'lishi mumkin)
-- turi = 'kirim'      -> qoldiq ORTADI  (yangi tovar ombor ga qo'shilganda)
-- turi = 'chiqim'     -> qoldiq KAMAYADI (tovar Uzum skladiga jo'natilganda)
-- turi = 'samovykup'  -> qoldiq ORTADI  (o'zi sotib olingan tovar qaytib kelganda)
-- turi = 'spisanie'   -> qoldiq KAMAYADI (brak/yo'qotish sababli spisaniye)
-- turi = 'prodazha'   -> qoldiq KAMAYADI (qo'lda to'g'ridan-to'g'ri sotilganda)
CREATE TABLE IF NOT EXISTS operatsiyalar (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sana TEXT NOT NULL DEFAULT (datetime('now')),
    turi TEXT NOT NULL CHECK(turi IN ('kirim', 'chiqim', 'samovykup', 'spisanie', 'prodazha')),
    izoh TEXT
);

-- Ombor harakatlari: bitta operatsiya ichidagi bitta mahsulot qatori.
-- Har bir yozuv mahsulotlar.qoldiq ni yangilaydi
CREATE TABLE IF NOT EXISTS ombor_harakatlari (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sana TEXT NOT NULL DEFAULT (datetime('now')),
    mahsulot_id INTEGER NOT NULL REFERENCES mahsulotlar(id),
    operatsiya_id INTEGER REFERENCES operatsiyalar(id),
    turi TEXT NOT NULL CHECK(turi IN ('kirim', 'chiqim', 'samovykup', 'spisanie', 'prodazha')),
    miqdor INTEGER NOT NULL CHECK(miqdor > 0),
    izoh TEXT
);

-- Kassa harakatlari: sof pul oqimi logi, hech qanday avtomatik hisob-kitobsiz
-- turi = 'kirim'  -> balans ORTADI
-- turi = 'chiqim' -> balans KAMAYADI
CREATE TABLE IF NOT EXISTS kassa_harakatlari (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sana TEXT NOT NULL DEFAULT (datetime('now')),
    turi TEXT NOT NULL CHECK(turi IN ('kirim', 'chiqim')),
    summa REAL NOT NULL CHECK(summa > 0),
    izoh TEXT
);

-- Foydalanuvchilar: rol = admin | menejer | kuzatuvchi
CREATE TABLE IF NOT EXISTS foydalanuvchilar (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    login TEXT NOT NULL UNIQUE,
    ism TEXT NOT NULL,
    email TEXT UNIQUE,
    parol_hash TEXT NOT NULL,
    rol TEXT NOT NULL CHECK(rol IN ('admin', 'menejer', 'kuzatuvchi')),
    faol INTEGER NOT NULL DEFAULT 1,
    yaratilgan TEXT
);

-- Email orqali yuborilgan takliflar (bir martalik havola)
CREATE TABLE IF NOT EXISTS takliflar (
    token TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    rol TEXT NOT NULL,
    yaratdi INTEGER,
    yaratilgan TEXT NOT NULL,
    muddat TEXT NOT NULL,
    ishlatilgan TEXT
);

-- O'zgarishlar jurnali: kim, qachon, nima qildi
CREATE TABLE IF NOT EXISTS jurnal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sana TEXT NOT NULL,
    user_id INTEGER,
    user_login TEXT,
    amal TEXT NOT NULL,
    tafsilot TEXT
);
