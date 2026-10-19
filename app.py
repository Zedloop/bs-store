"""
Mini ERP — ombor va kassa nazorati
Ishga tushirish: python3 app.py
Keyin brauzerda: http://localhost:5000
"""
import os
import re
import sqlite3
import uuid
from pathlib import Path
from dotenv import load_dotenv
from flask import Flask, g, jsonify, redirect, render_template, request, session, url_for
from PIL import Image

# Aniq shu loyihaning o'z .env fayli, override=True bilan - aks holda bir
# nechta loyiha bitta jarayonda birlashtirilganda (masalan PythonAnywhere'da),
# avval yuklangan boshqa loyihaning o'zgaruvchilari ustidan yozilmay qolishi
# mumkin edi.
load_dotenv(dotenv_path=Path(__file__).parent / ".env", override=True)

MAHSULOT_HAJMI_RE = re.compile(r"(\d+)\s*[gG][bB]")


def mahsulot_sort_key(nomi):
    match = MAHSULOT_HAJMI_RE.search(nomi)
    if match:
        hajmi = int(match.group(1))
        asosiy_nomi = nomi[: match.start()].strip(" ,").lower()
    else:
        hajmi = -1
        asosiy_nomi = nomi.strip().lower()
    return (asosiy_nomi, hajmi)

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "erp.db"
SCHEMA_PATH = BASE_DIR / "schema.sql"
UPLOAD_DIR = BASE_DIR / "static" / "uploads"
ALLOWED_RASM_EXT = {"png", "jpg", "jpeg", "gif", "webp"}
MAX_RASM_OLCHAM = 640
OMBOR_TURLARI = ("kirim", "chiqim", "samovykup", "spisanie", "prodazha")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024
app.secret_key = os.environ["FLASK_SECRET_KEY"]
ERP_PASSWORD = os.environ["ERP_PASSWORD"]
ERP_API_TOKEN = os.environ["ERP_API_TOKEN"]


@app.before_request
def himoyalash():
    if request.endpoint in ("login", "static", "uzum_prices_api"):
        return
    if not session.get("authed"):
        return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    xato = None
    if request.method == "POST":
        if request.form.get("parol") == ERP_PASSWORD:
            session["authed"] = True
            session.permanent = True
            return redirect(url_for("index"))
        xato = "Parol noto'g'ri"
    return render_template("login.html", xato=xato)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/api/uzum-prices")
def uzum_prices_api():
    """promo-agent (boshqa serverda ishlaydi) ERP narxlarini shu orqali
    oladi - sessiya (login) emas, maxfiy token orqali himoyalangan."""
    if request.args.get("token") != ERP_API_TOKEN:
        return jsonify({"error": "forbidden"}), 403

    db = get_db()
    rows = db.execute(
        "SELECT uzum_sku, sotish_narxi, tannarx, logistika, boshqa_harajat, nomi, ikpu "
        "FROM mahsulotlar WHERE uzum_sku IS NOT NULL AND TRIM(uzum_sku) != ''"
    ).fetchall()

    prices = {}
    for row in rows:
        sku = row["uzum_sku"].strip()
        prices[sku] = {
            "sale_price": row["sotish_narxi"],
            "tannarx": row["tannarx"],
            "logistika": row["logistika"],
            "boshqa_harajat": row["boshqa_harajat"],
            "nomi": row["nomi"],
            "ikpu": (row["ikpu"] or "").strip() or None,
        }
    return jsonify(prices)


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    with sqlite3.connect(DB_PATH) as db:
        db.executescript(SCHEMA_PATH.read_text())
        mahsulot_ustunlari = [r[1] for r in db.execute("PRAGMA table_info(mahsulotlar)")]
        if "rasm" not in mahsulot_ustunlari:
            db.execute("ALTER TABLE mahsulotlar ADD COLUMN rasm TEXT")
        if "tannarx" not in mahsulot_ustunlari:
            db.execute("ALTER TABLE mahsulotlar ADD COLUMN tannarx REAL NOT NULL DEFAULT 0")
        if "sotish_narxi" not in mahsulot_ustunlari:
            db.execute("ALTER TABLE mahsulotlar ADD COLUMN sotish_narxi REAL NOT NULL DEFAULT 0")
        if "logistika" not in mahsulot_ustunlari:
            db.execute("ALTER TABLE mahsulotlar ADD COLUMN logistika REAL NOT NULL DEFAULT 5000")
        if "boshqa_harajat" not in mahsulot_ustunlari:
            db.execute("ALTER TABLE mahsulotlar ADD COLUMN boshqa_harajat REAL NOT NULL DEFAULT 100")
        if "narx_usd" not in mahsulot_ustunlari:
            db.execute("ALTER TABLE mahsulotlar ADD COLUMN narx_usd REAL NOT NULL DEFAULT 0")
        if "kurs" not in mahsulot_ustunlari:
            db.execute("ALTER TABLE mahsulotlar ADD COLUMN kurs REAL NOT NULL DEFAULT 12500")
        if "uzum_sku" not in mahsulot_ustunlari:
            db.execute("ALTER TABLE mahsulotlar ADD COLUMN uzum_sku TEXT")
        if "ikpu" not in mahsulot_ustunlari:
            db.execute("ALTER TABLE mahsulotlar ADD COLUMN ikpu TEXT")
        ombor_ustunlari = [r[1] for r in db.execute("PRAGMA table_info(ombor_harakatlari)")]
        if "operatsiya_id" not in ombor_ustunlari:
            db.execute(
                "ALTER TABLE ombor_harakatlari ADD COLUMN operatsiya_id INTEGER REFERENCES operatsiyalar(id)"
            )

        # eski bazalarda 'turi' CHECK cheklovi yangi turlarni (spisanie, prodazha) bilmaydi —
        # jadvalni qayta yaratib, ma'lumotlarni ko'chiramiz
        operatsiyalar_sql = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='operatsiyalar'"
        ).fetchone()
        if operatsiyalar_sql and "spisanie" not in operatsiyalar_sql[0]:
            db.executescript("""
                CREATE TABLE operatsiyalar_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sana TEXT NOT NULL DEFAULT (datetime('now')),
                    turi TEXT NOT NULL CHECK(turi IN ('kirim', 'chiqim', 'samovykup', 'spisanie', 'prodazha')),
                    izoh TEXT
                );
                INSERT INTO operatsiyalar_new SELECT id, sana, turi, izoh FROM operatsiyalar;
                DROP TABLE operatsiyalar;
                ALTER TABLE operatsiyalar_new RENAME TO operatsiyalar;
            """)

        ombor_sql = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='ombor_harakatlari'"
        ).fetchone()
        if ombor_sql and "spisanie" not in ombor_sql[0]:
            db.executescript("""
                CREATE TABLE ombor_harakatlari_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sana TEXT NOT NULL DEFAULT (datetime('now')),
                    mahsulot_id INTEGER NOT NULL REFERENCES mahsulotlar(id),
                    operatsiya_id INTEGER REFERENCES operatsiyalar(id),
                    turi TEXT NOT NULL CHECK(turi IN ('kirim', 'chiqim', 'samovykup', 'spisanie', 'prodazha')),
                    miqdor INTEGER NOT NULL CHECK(miqdor > 0),
                    izoh TEXT
                );
                INSERT INTO ombor_harakatlari_new
                    SELECT id, sana, mahsulot_id, operatsiya_id, turi, miqdor, izoh FROM ombor_harakatlari;
                DROP TABLE ombor_harakatlari;
                ALTER TABLE ombor_harakatlari_new RENAME TO ombor_harakatlari;
            """)

        db.commit()


def rasm_ruxsat_etilganmi(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_RASM_EXT


# ---------- sahifa ----------

@app.route("/")
def index():
    return render_template("mahsulotlar.html", active="mahsulotlar")


@app.route("/mahsulot/<int:mahsulot_id>")
def mahsulot_detail_sahifa(mahsulot_id):
    return render_template(
        "mahsulot_detail.html", active="mahsulotlar", mahsulot_id=mahsulot_id
    )


@app.route("/ombor")
def ombor_sahifa():
    return render_template("ombor.html", active="ombor")


@app.route("/operatsiya/<int:operatsiya_id>")
def operatsiya_detail_sahifa(operatsiya_id):
    return render_template(
        "operatsiya_detail.html", active="ombor", operatsiya_id=operatsiya_id
    )


@app.route("/kassa")
def kassa_sahifa():
    return render_template("kassa.html", active="kassa")


@app.route("/tannarx")
def tannarx_sahifa():
    return render_template("tannarx.html", active="tannarx")


@app.route("/uzum-sozlamalari")
def uzum_sozlamalari_sahifa():
    return render_template("uzum_sozlamalari.html", active="uzum")


# ---------- mahsulotlar ----------

@app.route("/api/mahsulotlar", methods=["GET"])
def list_mahsulotlar():
    db = get_db()
    rows = db.execute(
        "SELECT id, nomi, qoldiq, tannarx, sotish_narxi, rasm, uzum_sku, ikpu FROM mahsulotlar"
    ).fetchall()
    mahsulotlar = [dict(r) for r in rows]
    mahsulotlar.sort(key=lambda m: mahsulot_sort_key(m["nomi"]))
    return jsonify(mahsulotlar)


@app.route("/api/mahsulotlar", methods=["POST"])
def add_mahsulot():
    data = request.get_json(force=True)
    nomi = (data.get("nomi") or "").strip()
    if not nomi:
        return jsonify({"xato": "Укажите название товара"}), 400
    db = get_db()
    cur = db.execute("INSERT INTO mahsulotlar (nomi) VALUES (?)", (nomi,))
    db.commit()
    return jsonify({"id": cur.lastrowid, "nomi": nomi, "qoldiq": 0, "tannarx": 0, "sotish_narxi": 0, "rasm": None}), 201


@app.route("/api/mahsulotlar/<int:mahsulot_id>", methods=["GET"])
def get_mahsulot(mahsulot_id):
    db = get_db()
    row = db.execute(
        "SELECT id, nomi, qoldiq, tannarx, sotish_narxi, rasm, uzum_sku, ikpu FROM mahsulotlar WHERE id = ?", (mahsulot_id,)
    ).fetchone()
    if row is None:
        return jsonify({"xato": "Товар не найден"}), 404
    return jsonify(dict(row))


@app.route("/api/mahsulotlar/<int:mahsulot_id>", methods=["PATCH"])
def update_mahsulot(mahsulot_id):
    data = request.get_json(force=True)
    nomi = (data.get("nomi") or "").strip()
    if not nomi:
        return jsonify({"xato": "Укажите название товара"}), 400
    uzum_sku = (data.get("uzum_sku") or "").strip() or None
    ikpu = (data.get("ikpu") or "").strip() or None
    db = get_db()
    mahsulot = db.execute(
        "SELECT id FROM mahsulotlar WHERE id = ?", (mahsulot_id,)
    ).fetchone()
    if mahsulot is None:
        return jsonify({"xato": "Товар не найден"}), 404
    db.execute(
        "UPDATE mahsulotlar SET nomi = ?, uzum_sku = ?, ikpu = ? WHERE id = ?",
        (nomi, uzum_sku, ikpu, mahsulot_id),
    )
    db.commit()
    return jsonify({"ok": True, "nomi": nomi, "uzum_sku": uzum_sku, "ikpu": ikpu})


@app.route("/api/mahsulotlar/<int:mahsulot_id>", methods=["DELETE"])
def delete_mahsulot(mahsulot_id):
    db = get_db()
    mahsulot = db.execute(
        "SELECT id, rasm FROM mahsulotlar WHERE id = ?", (mahsulot_id,)
    ).fetchone()
    if mahsulot is None:
        return jsonify({"xato": "Товар не найден"}), 404

    db.execute("DELETE FROM ombor_harakatlari WHERE mahsulot_id = ?", (mahsulot_id,))
    db.execute("DELETE FROM mahsulotlar WHERE id = ?", (mahsulot_id,))
    db.commit()

    if mahsulot["rasm"]:
        (UPLOAD_DIR / mahsulot["rasm"]).unlink(missing_ok=True)

    return jsonify({"ok": True})


@app.route("/api/mahsulotlar/<int:mahsulot_id>/narxlar", methods=["PATCH"])
def update_narxlar(mahsulot_id):
    data = request.get_json(force=True)
    try:
        narx_usd = float(data.get("narx_usd"))
        kurs = float(data.get("kurs"))
        sotish_narxi = float(data.get("sotish_narxi"))
        logistika = float(data.get("logistika"))
        boshqa_harajat = float(data.get("boshqa_harajat"))
        if min(narx_usd, kurs, sotish_narxi, logistika, boshqa_harajat) < 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"xato": "Все значения должны быть числами не меньше 0"}), 400

    tannarx = narx_usd * kurs

    db = get_db()
    mahsulot = db.execute(
        "SELECT id FROM mahsulotlar WHERE id = ?", (mahsulot_id,)
    ).fetchone()
    if mahsulot is None:
        return jsonify({"xato": "Товар не найден"}), 404
    db.execute(
        """UPDATE mahsulotlar
           SET tannarx = ?, narx_usd = ?, kurs = ?, sotish_narxi = ?, logistika = ?, boshqa_harajat = ?
           WHERE id = ?""",
        (tannarx, narx_usd, kurs, sotish_narxi, logistika, boshqa_harajat, mahsulot_id),
    )
    db.commit()
    return jsonify({
        "ok": True,
        "tannarx": tannarx,
        "narx_usd": narx_usd,
        "kurs": kurs,
        "sotish_narxi": sotish_narxi,
        "logistika": logistika,
        "boshqa_harajat": boshqa_harajat,
    })


@app.route("/api/mahsulotlar/<int:mahsulot_id>/rasm", methods=["POST"])
def upload_mahsulot_rasm(mahsulot_id):
    db = get_db()
    mahsulot = db.execute(
        "SELECT id, rasm FROM mahsulotlar WHERE id = ?", (mahsulot_id,)
    ).fetchone()
    if mahsulot is None:
        return jsonify({"xato": "Товар не найден"}), 404

    fayl = request.files.get("rasm")
    if fayl is None or fayl.filename == "":
        return jsonify({"xato": "Файл не выбран"}), 400
    if not rasm_ruxsat_etilganmi(fayl.filename):
        return jsonify({"xato": "Разрешены только изображения (png, jpg, jpeg, gif, webp)"}), 400

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    kengaytma = fayl.filename.rsplit(".", 1)[1].lower()
    yangi_nom = f"{mahsulot_id}_{uuid.uuid4().hex}.{kengaytma}"

    rasm = Image.open(fayl.stream)
    rasm.thumbnail((MAX_RASM_OLCHAM, MAX_RASM_OLCHAM))
    saqlash_sozlamalari = {"optimize": True}
    if kengaytma in ("jpg", "jpeg"):
        if rasm.mode in ("RGBA", "P"):
            rasm = rasm.convert("RGB")
        saqlash_sozlamalari["quality"] = 82
    elif kengaytma == "webp":
        saqlash_sozlamalari["quality"] = 82
    rasm.save(UPLOAD_DIR / yangi_nom, **saqlash_sozlamalari)

    eski_rasm = mahsulot["rasm"]
    db.execute("UPDATE mahsulotlar SET rasm = ? WHERE id = ?", (yangi_nom, mahsulot_id))
    db.commit()

    if eski_rasm:
        (UPLOAD_DIR / eski_rasm).unlink(missing_ok=True)

    return jsonify({"ok": True, "rasm": yangi_nom})


# ---------- ombor harakatlari ----------

@app.route("/api/ombor", methods=["GET"])
def list_ombor():
    db = get_db()
    mahsulot_id = request.args.get("mahsulot_id", type=int)
    query = """SELECT oh.id, oh.sana, oh.turi, oh.miqdor, oh.izoh,
                      m.nomi AS mahsulot_nomi
               FROM ombor_harakatlari oh
               JOIN mahsulotlar m ON m.id = oh.mahsulot_id"""
    params = ()
    if mahsulot_id is not None:
        query += " WHERE oh.mahsulot_id = ?"
        params = (mahsulot_id,)
    query += " ORDER BY oh.id DESC LIMIT 50"
    rows = db.execute(query, params).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/ombor", methods=["POST"])
def add_ombor_harakat():
    data = request.get_json(force=True)
    mahsulot_id = data.get("mahsulot_id")
    turi = data.get("turi")
    miqdor = data.get("miqdor")
    izoh = (data.get("izoh") or "").strip() or None

    if turi not in OMBOR_TURLARI:
        return jsonify({"xato": "Неверный тип операции"}), 400
    try:
        miqdor = int(miqdor)
        if miqdor <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"xato": "Количество должно быть положительным числом"}), 400

    db = get_db()
    mahsulot = db.execute(
        "SELECT id, qoldiq FROM mahsulotlar WHERE id = ?", (mahsulot_id,)
    ).fetchone()
    if mahsulot is None:
        return jsonify({"xato": "Товар не найден"}), 404

    # kirim va samovykup qoldiqni oshiradi, chiqim kamaytiradi
    delta = miqdor if turi in ("kirim", "samovykup") else -miqdor
    yangi_qoldiq = mahsulot["qoldiq"] + delta
    if yangi_qoldiq < 0:
        return jsonify({"xato": "На складе недостаточно товара"}), 400

    db.execute(
        """INSERT INTO ombor_harakatlari (mahsulot_id, turi, miqdor, izoh)
           VALUES (?, ?, ?, ?)""",
        (mahsulot_id, turi, miqdor, izoh),
    )
    db.execute(
        "UPDATE mahsulotlar SET qoldiq = ? WHERE id = ?",
        (yangi_qoldiq, mahsulot_id),
    )
    db.commit()
    return jsonify({"ok": True, "yangi_qoldiq": yangi_qoldiq}), 201


# ---------- operatsiyalar (bir nechta mahsulotli ombor amali) ----------

@app.route("/api/operatsiyalar", methods=["GET"])
def list_operatsiyalar():
    db = get_db()
    turi = request.args.get("turi")
    query = """SELECT o.id, o.sana, o.turi, o.izoh, COUNT(oh.id) AS tovar_soni
               FROM operatsiyalar o
               LEFT JOIN ombor_harakatlari oh ON oh.operatsiya_id = o.id"""
    params = ()
    if turi in OMBOR_TURLARI:
        query += " WHERE o.turi = ?"
        params = (turi,)
    query += " GROUP BY o.id ORDER BY o.id DESC LIMIT 50"
    rows = db.execute(query, params).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/operatsiyalar/<int:operatsiya_id>", methods=["GET"])
def get_operatsiya(operatsiya_id):
    db = get_db()
    operatsiya = db.execute(
        "SELECT id, sana, turi, izoh FROM operatsiyalar WHERE id = ?", (operatsiya_id,)
    ).fetchone()
    if operatsiya is None:
        return jsonify({"xato": "Операция не найдена"}), 404
    tovarlar = db.execute(
        """SELECT oh.id, oh.mahsulot_id, m.nomi AS mahsulot_nomi, m.rasm AS mahsulot_rasm, oh.miqdor
           FROM ombor_harakatlari oh
           JOIN mahsulotlar m ON m.id = oh.mahsulot_id
           WHERE oh.operatsiya_id = ?
           ORDER BY oh.id""",
        (operatsiya_id,),
    ).fetchall()
    natija = dict(operatsiya)
    natija["tovarlar"] = [dict(r) for r in tovarlar]
    return jsonify(natija)


@app.route("/api/operatsiyalar", methods=["POST"])
def add_operatsiya():
    data = request.get_json(force=True)
    turi = data.get("turi")
    izoh = (data.get("izoh") or "").strip() or None
    tovarlar = data.get("tovarlar")

    if turi not in OMBOR_TURLARI:
        return jsonify({"xato": "Неверный тип операции"}), 400
    if not isinstance(tovarlar, list) or not tovarlar:
        return jsonify({"xato": "Добавьте хотя бы один товар"}), 400

    tozalangan = []
    for item in tovarlar:
        try:
            mahsulot_id = int(item.get("mahsulot_id"))
            miqdor = int(item.get("miqdor"))
            if miqdor <= 0:
                raise ValueError
        except (TypeError, ValueError, AttributeError):
            return jsonify({"xato": "Неверное количество товара"}), 400
        tozalangan.append((mahsulot_id, miqdor))

    db = get_db()

    # avval barcha yangi qoldiqlarni hisoblab, faqat shundan keyin yozamiz
    qoldiqlar = {}
    for mahsulot_id, miqdor in tozalangan:
        if mahsulot_id not in qoldiqlar:
            mahsulot = db.execute(
                "SELECT qoldiq FROM mahsulotlar WHERE id = ?", (mahsulot_id,)
            ).fetchone()
            if mahsulot is None:
                return jsonify({"xato": f"Товар с id={mahsulot_id} не найден"}), 404
            qoldiqlar[mahsulot_id] = mahsulot["qoldiq"]
        delta = miqdor if turi in ("kirim", "samovykup") else -miqdor
        qoldiqlar[mahsulot_id] += delta

    if any(q < 0 for q in qoldiqlar.values()):
        return jsonify({"xato": "На складе недостаточно товара"}), 400

    cur = db.execute("INSERT INTO operatsiyalar (turi, izoh) VALUES (?, ?)", (turi, izoh))
    operatsiya_id = cur.lastrowid

    for mahsulot_id, miqdor in tozalangan:
        db.execute(
            """INSERT INTO ombor_harakatlari (mahsulot_id, operatsiya_id, turi, miqdor, izoh)
               VALUES (?, ?, ?, ?, ?)""",
            (mahsulot_id, operatsiya_id, turi, miqdor, izoh),
        )
    for mahsulot_id, yangi_qoldiq in qoldiqlar.items():
        db.execute(
            "UPDATE mahsulotlar SET qoldiq = ? WHERE id = ?", (yangi_qoldiq, mahsulot_id)
        )
    db.commit()

    return jsonify({"id": operatsiya_id, "turi": turi, "izoh": izoh}), 201


@app.route("/api/operatsiyalar/<int:operatsiya_id>", methods=["DELETE"])
def delete_operatsiya(operatsiya_id):
    db = get_db()
    operatsiya = db.execute(
        "SELECT id, turi FROM operatsiyalar WHERE id = ?", (operatsiya_id,)
    ).fetchone()
    if operatsiya is None:
        return jsonify({"xato": "Операция не найдена"}), 404

    tovarlar = db.execute(
        "SELECT mahsulot_id, miqdor FROM ombor_harakatlari WHERE operatsiya_id = ?",
        (operatsiya_id,),
    ).fetchall()

    turi = operatsiya["turi"]
    for tovar in tovarlar:
        # bekor qilish uchun qoldiqni teskari yo'nalishda tiklaymiz
        delta = -tovar["miqdor"] if turi in ("kirim", "samovykup") else tovar["miqdor"]
        db.execute(
            "UPDATE mahsulotlar SET qoldiq = qoldiq + ? WHERE id = ?",
            (delta, tovar["mahsulot_id"]),
        )

    db.execute("DELETE FROM ombor_harakatlari WHERE operatsiya_id = ?", (operatsiya_id,))
    db.execute("DELETE FROM operatsiyalar WHERE id = ?", (operatsiya_id,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/operatsiyalar/<int:operatsiya_id>/tovarlar", methods=["POST"])
def add_operatsiya_tovar(operatsiya_id):
    data = request.get_json(force=True)
    try:
        mahsulot_id = int(data.get("mahsulot_id"))
        miqdor = int(data.get("miqdor"))
        if miqdor <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"xato": "Неверные данные товара"}), 400

    db = get_db()
    operatsiya = db.execute(
        "SELECT id, turi, izoh FROM operatsiyalar WHERE id = ?", (operatsiya_id,)
    ).fetchone()
    if operatsiya is None:
        return jsonify({"xato": "Операция не найдена"}), 404
    mahsulot = db.execute(
        "SELECT qoldiq FROM mahsulotlar WHERE id = ?", (mahsulot_id,)
    ).fetchone()
    if mahsulot is None:
        return jsonify({"xato": "Товар не найден"}), 404

    turi = operatsiya["turi"]
    delta = miqdor if turi in ("kirim", "samovykup") else -miqdor
    yangi_qoldiq = mahsulot["qoldiq"] + delta
    if yangi_qoldiq < 0:
        return jsonify({"xato": "На складе недостаточно товара"}), 400

    db.execute(
        """INSERT INTO ombor_harakatlari (mahsulot_id, operatsiya_id, turi, miqdor, izoh)
           VALUES (?, ?, ?, ?, ?)""",
        (mahsulot_id, operatsiya_id, turi, miqdor, operatsiya["izoh"]),
    )
    db.execute("UPDATE mahsulotlar SET qoldiq = ? WHERE id = ?", (yangi_qoldiq, mahsulot_id))
    db.commit()
    return jsonify({"ok": True}), 201


@app.route("/api/ombor/<int:harakat_id>", methods=["PATCH"])
def update_ombor_harakat(harakat_id):
    data = request.get_json(force=True)
    try:
        yangi_miqdor = int(data.get("miqdor"))
        if yangi_miqdor <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"xato": "Количество должно быть положительным числом"}), 400

    db = get_db()
    harakat = db.execute(
        "SELECT id, mahsulot_id, turi, miqdor FROM ombor_harakatlari WHERE id = ?", (harakat_id,)
    ).fetchone()
    if harakat is None:
        return jsonify({"xato": "Запись не найдена"}), 404
    mahsulot = db.execute(
        "SELECT qoldiq FROM mahsulotlar WHERE id = ?", (harakat["mahsulot_id"],)
    ).fetchone()

    turi = harakat["turi"]
    eski_delta = harakat["miqdor"] if turi in ("kirim", "samovykup") else -harakat["miqdor"]
    yangi_delta = yangi_miqdor if turi in ("kirim", "samovykup") else -yangi_miqdor
    yangi_qoldiq = mahsulot["qoldiq"] - eski_delta + yangi_delta
    if yangi_qoldiq < 0:
        return jsonify({"xato": "На складе недостаточно товара"}), 400

    db.execute("UPDATE ombor_harakatlari SET miqdor = ? WHERE id = ?", (yangi_miqdor, harakat_id))
    db.execute(
        "UPDATE mahsulotlar SET qoldiq = ? WHERE id = ?", (yangi_qoldiq, harakat["mahsulot_id"])
    )
    db.commit()
    return jsonify({"ok": True, "miqdor": yangi_miqdor})


@app.route("/api/ombor/<int:harakat_id>", methods=["DELETE"])
def delete_ombor_harakat(harakat_id):
    db = get_db()
    harakat = db.execute(
        "SELECT id, mahsulot_id, turi, miqdor FROM ombor_harakatlari WHERE id = ?", (harakat_id,)
    ).fetchone()
    if harakat is None:
        return jsonify({"xato": "Запись не найдена"}), 404

    delta = -harakat["miqdor"] if harakat["turi"] in ("kirim", "samovykup") else harakat["miqdor"]
    db.execute(
        "UPDATE mahsulotlar SET qoldiq = qoldiq + ? WHERE id = ?",
        (delta, harakat["mahsulot_id"]),
    )
    db.execute("DELETE FROM ombor_harakatlari WHERE id = ?", (harakat_id,))
    db.commit()
    return jsonify({"ok": True})


# ---------- tannarx va foyda hisoboti ----------

@app.route("/api/tannarx-hisobot", methods=["GET"])
def tannarx_hisobot():
    db = get_db()
    mahsulotlar_qatorlari = db.execute(
        """SELECT id, nomi, qoldiq, tannarx, narx_usd, kurs, sotish_narxi, logistika, boshqa_harajat, rasm
           FROM mahsulotlar"""
    ).fetchall()
    mahsulotlar = [dict(r) for r in mahsulotlar_qatorlari]
    mahsulotlar.sort(key=lambda m: mahsulot_sort_key(m["nomi"]))

    turlar_tannarxi = db.execute(
        """SELECT oh.turi, COALESCE(SUM(oh.miqdor * m.tannarx), 0) AS jami
           FROM ombor_harakatlari oh
           JOIN mahsulotlar m ON m.id = oh.mahsulot_id
           GROUP BY oh.turi"""
    ).fetchall()
    tannarx_turlar = {t: 0 for t in OMBOR_TURLARI}
    for row in turlar_tannarxi:
        tannarx_turlar[row["turi"]] = row["jami"]

    kassa_balans_row = db.execute(
        """SELECT COALESCE(SUM(CASE WHEN turi = 'kirim' THEN summa ELSE -summa END), 0) AS balans
           FROM kassa_harakatlari"""
    ).fetchone()
    kassa_balans = kassa_balans_row["balans"]

    # Uzumga jo'natilgan va qo'lda sotilgan tovar tannarxidan samovykup (qaytgan) qismi ayriladi
    sotilgan_tannarx = (
        tannarx_turlar["chiqim"] + tannarx_turlar["prodazha"] - tannarx_turlar["samovykup"]
    )
    foyda = kassa_balans - sotilgan_tannarx
    ombor_qiymati = sum(m["qoldiq"] * m["tannarx"] for m in mahsulotlar)

    return jsonify({
        "mahsulotlar": mahsulotlar,
        "ombor_qiymati": ombor_qiymati,
        "kassa_balans": kassa_balans,
        "kirim_tannarxi": tannarx_turlar["kirim"],
        "chiqim_tannarxi": tannarx_turlar["chiqim"],
        "samovykup_tannarxi": tannarx_turlar["samovykup"],
        "spisanie_tannarxi": tannarx_turlar["spisanie"],
        "prodazha_tannarxi": tannarx_turlar["prodazha"],
        "sotilgan_tannarx": sotilgan_tannarx,
        "foyda": foyda,
    })


# ---------- kassa harakatlari ----------

@app.route("/api/kassa", methods=["GET"])
def list_kassa():
    db = get_db()
    rows = db.execute(
        "SELECT id, sana, turi, summa, izoh FROM kassa_harakatlari ORDER BY id DESC LIMIT 50"
    ).fetchall()
    balans_row = db.execute(
        """SELECT
             COALESCE(SUM(CASE WHEN turi = 'kirim' THEN summa ELSE -summa END), 0) AS balans
           FROM kassa_harakatlari"""
    ).fetchone()
    return jsonify({
        "harakatlar": [dict(r) for r in rows],
        "balans": balans_row["balans"],
    })


@app.route("/api/kassa", methods=["POST"])
def add_kassa_harakat():
    data = request.get_json(force=True)
    turi = data.get("turi")
    summa = data.get("summa")
    izoh = (data.get("izoh") or "").strip() or None

    if turi not in ("kirim", "chiqim"):
        return jsonify({"xato": "Неверный тип операции"}), 400
    try:
        summa = float(summa)
        if summa <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"xato": "Сумма должна быть положительным числом"}), 400

    db = get_db()
    db.execute(
        "INSERT INTO kassa_harakatlari (turi, summa, izoh) VALUES (?, ?, ?)",
        (turi, summa, izoh),
    )
    db.commit()
    return jsonify({"ok": True}), 201


if __name__ == "__main__":
    init_db()
    app.run(debug=True, port=5000)
