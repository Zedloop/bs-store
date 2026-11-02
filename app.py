"""
Mini ERP — ombor va kassa nazorati
Ishga tushirish: python3 app.py
Keyin brauzerda: http://localhost:5000
"""
import os
import re
import secrets
import shutil
import smtplib
import sqlite3
import time
import uuid
from email.message import EmailMessage
from pathlib import Path
from dotenv import load_dotenv
from flask import Flask, g, jsonify, redirect, render_template, request, session, url_for
from PIL import Image
from werkzeug.security import check_password_hash, generate_password_hash

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


ROLLAR = {"admin": "Администратор", "menejer": "Менеджер", "kuzatuvchi": "Наблюдатель"}
TAKLIF_MUDDATI_SOAT = 72
ERP_BASE_URL = os.environ.get("ERP_BASE_URL", "https://erp.bsuzum.duckdns.org").rstrip("/")

# Login talab qilinmaydigan manzillar (token bilan himoyalangan API'lar va taklif sahifasi)
OCHIQ_ENDPOINTLAR = ("login", "static", "uzum_prices_api", "uzum_costs_api", "taklif_qabul")
# Faqat admin: katalog, narx/tannarx, Uzum sozlamalari, foydalanuvchilar
ADMIN_ENDPOINTLAR = {
    "add_mahsulot", "update_mahsulot", "delete_mahsulot", "update_narxlar", "upload_mahsulot_rasm",
    "foydalanuvchilar_sahifa", "list_foydalanuvchilar", "taklif_yuborish", "taklif_bekor",
    "update_foydalanuvchi", "jurnal_api",
}
# Menejer o'zgartira oladigan amallar: ombor va kassa
MENEJER_YOZISH = {
    "add_ombor_harakat", "add_operatsiya", "delete_operatsiya", "add_operatsiya_tovar",
    "update_ombor_harakat", "delete_ombor_harakat", "add_kassa_harakat",
    "add_kontragent", "add_kontragent_harakat",
}


def _ruxsat_yoq():
    if request.path.startswith("/api/"):
        return jsonify({"xato": "Недостаточно прав для этого действия"}), 403
    return redirect(url_for("index"))


@app.before_request
def himoyalash():
    if request.endpoint in OCHIQ_ENDPOINTLAR:
        return
    user = None
    if session.get("user_id"):
        user = get_db().execute(
            "SELECT id, login, ism, email, rol FROM foydalanuvchilar WHERE id = ? AND faol = 1",
            (session["user_id"],),
        ).fetchone()
    if user is None:
        session.clear()
        if request.path.startswith("/api/"):
            return jsonify({"xato": "Сессия истекла — войдите снова"}), 401
        return redirect(url_for("login"))
    g.user = dict(user)
    if request.endpoint in ADMIN_ENDPOINTLAR and user["rol"] != "admin":
        return _ruxsat_yoq()
    if request.method not in ("GET", "HEAD") and user["rol"] != "admin":
        if not (user["rol"] == "menejer" and request.endpoint in MENEJER_YOZISH):
            return _ruxsat_yoq()


@app.context_processor
def joriy_foydalanuvchi():
    user = g.get("user")
    return {"joriy_user": user, "rol_nomi": ROLLAR.get(user["rol"]) if user else None}


# ---------- o'zgarishlar jurnali ----------

AMAL_NOMLARI = {
    "add_mahsulot": "Добавлен товар",
    "update_mahsulot": "Изменён товар",
    "delete_mahsulot": "Удалён товар",
    "update_narxlar": "Изменены цены / расходы",
    "upload_mahsulot_rasm": "Загружено фото товара",
    "add_ombor_harakat": "Движение товара на складе",
    "add_operatsiya": "Добавлена складская операция",
    "delete_operatsiya": "Удалена складская операция",
    "add_operatsiya_tovar": "Добавлен товар в операцию",
    "update_ombor_harakat": "Изменено количество в операции",
    "delete_ombor_harakat": "Удалён товар из операции",
    "add_kassa_harakat": "Операция по кассе",
    "add_kontragent": "Добавлен контрагент",
    "delete_kontragent": "Удалён контрагент",
    "add_kontragent_harakat": "Операция с контрагентом",
    "delete_kontragent_harakat": "Удалена операция с контрагентом",
    "taklif_yuborish": "Отправлено приглашение",
    "taklif_bekor": "Отменено приглашение",
    "update_foydalanuvchi": "Изменён пользователь",
}


def jurnalga_yoz(amal, tafsilot="", user_id=None, user_login=None):
    if user_id is None and g.get("user"):
        user_id, user_login = g.user["id"], g.user["login"]
    db = get_db()
    db.execute(
        "INSERT INTO jurnal (sana, user_id, user_login, amal, tafsilot) VALUES (datetime('now', '+5 hours'), ?, ?, ?, ?)",
        (user_id, user_login, amal, (tafsilot or "")[:600]),
    )
    db.commit()


def _raqam(v):
    v = float(v or 0)
    return f"{v:,.2f}".rstrip("0").rstrip(".").replace(",", " ")


def _sorov_tafsiloti():
    """Jurnal uchun so'rovdan qisqa, o'qiladigan izoh."""
    data = request.get_json(silent=True) or {}
    args = request.view_args or {}
    qismlar = []
    mid = args.get("mahsulot_id") or data.get("mahsulot_id")
    if mid:
        row = get_db().execute("SELECT nomi FROM mahsulotlar WHERE id = ?", (mid,)).fetchone()
        qismlar.append(row["nomi"] if row else f"товар #{mid}")
    for k in ("operatsiya_id", "harakat_id"):
        if args.get(k):
            qismlar.append(f"#{args[k]}")
    nomlar = {"nomi": "название", "turi": "тип", "miqdor": "кол-во", "summa": "сумма", "izoh": "комментарий"}
    for k, v in data.items():
        if k in nomlar and v not in (None, ""):
            qismlar.append(f"{nomlar[k]}: {v}")
    if isinstance(data.get("tovarlar"), list):
        qismlar.append(f"товаров: {len(data['tovarlar'])}")
    return "; ".join(map(str, qismlar))


@app.after_request
def jurnal_yozish(response):
    try:
        if request.endpoint == "uzum_costs_api" and response.status_code == 200:
            n = (response.get_json(silent=True) or {}).get("yangilandi")
            if n:
                jurnalga_yoz("Автосинхронизация с Uzum (комиссия / буст / логистика)",
                             f"обновлено полей: {n}", user_id=0, user_login="promo-agent")
        elif request.endpoint in AMAL_NOMLARI and response.status_code < 400 and g.get("user"):
            tafsilot = g.get("jurnal_tafsilot")
            if tafsilot is None:
                tafsilot = _sorov_tafsiloti()
            if tafsilot != "":  # "" - hech narsa o'zgarmagan (masalan narx avto-saqlashda)
                jurnalga_yoz(AMAL_NOMLARI[request.endpoint], tafsilot)
    except Exception:
        app.logger.exception("jurnalga yozib bo'lmadi")
    return response


# ---------- kirish / chiqish ----------

@app.route("/login", methods=["GET", "POST"])
def login():
    xato = None
    login_qiymat = ""
    if request.method == "POST":
        login_qiymat = (request.form.get("login") or "").strip().lower()
        user = get_db().execute(
            "SELECT id, login, parol_hash, faol FROM foydalanuvchilar WHERE login = ? OR email = ?",
            (login_qiymat, login_qiymat),
        ).fetchone()
        if user and user["faol"] and check_password_hash(user["parol_hash"], request.form.get("parol") or ""):
            session.clear()
            session["user_id"] = user["id"]
            session.permanent = True
            jurnalga_yoz("Вход в систему", user_id=user["id"], user_login=user["login"])
            return redirect(url_for("index"))
        xato = "Неверный логин или пароль"
    return render_template("login.html", xato=xato, login_qiymat=login_qiymat)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------- foydalanuvchilar va takliflar (faqat admin) ----------

LOGIN_RE = re.compile(r"[a-z0-9_.-]{3,32}")
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def taklif_xati_yubor(email, rol, havola):
    """Taklif havolasini emailga yuboradi. SMTP sozlanmagan bo'lsa False -
    unda admin havolani sahifadan nusxalab o'zi yuboradi."""
    host = os.environ.get("SMTP_HOST")
    user = os.environ.get("SMTP_USER")
    parol = os.environ.get("SMTP_PASSWORD")
    if not (host and user and parol):
        return False
    msg = EmailMessage()
    msg["Subject"] = "Приглашение в BS Store ERP / BS Store ERP'ga taklif"
    msg["From"] = os.environ.get("SMTP_FROM") or f"BS Store ERP <{user}>"
    msg["To"] = email
    rol_nomi = ROLLAR.get(rol, rol)
    msg.set_content(
        f"Assalomu alaykum!\n\nSizni BS Store ERP tizimiga taklif qilishdi (rol: {rol_nomi}).\n"
        f"Quyidagi havolani ochib, o'zingizga login va parol o'ylab toping:\n{havola}\n\n"
        f"Havola {TAKLIF_MUDDATI_SOAT} soat amal qiladi.\n\n---\n\n"
        f"Здравствуйте! Вас пригласили в BS Store ERP (роль: {rol_nomi}).\n"
        f"Откройте ссылку и придумайте логин и пароль:\n{havola}\n\n"
        f"Ссылка действует {TAKLIF_MUDDATI_SOAT} часа.\n"
    )
    msg.add_alternative(f"""<div style="font-family:Arial,sans-serif;max-width:480px;margin:auto;color:#23231f">
  <h2 style="margin:0 0 6px">BS <span style="color:#2f6f4f">Store</span> ERP</h2>
  <p>Sizni tizimga taklif qilishdi. Rol: <b>{rol_nomi}</b>.<br>
     Вас пригласили в систему. Роль: <b>{rol_nomi}</b>.</p>
  <p style="margin:24px 0"><a href="{havola}" style="background:#2f6f4f;color:#fff;text-decoration:none;
     padding:12px 22px;border-radius:10px;font-weight:bold;display:inline-block">Login va parol yaratish / Создать логин и пароль</a></p>
  <p style="color:#7a7972;font-size:13px">Havola {TAKLIF_MUDDATI_SOAT} soat amal qiladi · Ссылка действует {TAKLIF_MUDDATI_SOAT} ч.</p>
</div>""", subtype="html")
    port = int(os.environ.get("SMTP_PORT", "465"))
    with smtplib.SMTP_SSL(host, port, timeout=20) as smtp:
        smtp.login(user, parol)
        smtp.send_message(msg)
    return True


@app.route("/foydalanuvchilar")
def foydalanuvchilar_sahifa():
    return render_template("foydalanuvchilar.html", active="foydalanuvchilar")


@app.route("/api/foydalanuvchilar", methods=["GET"])
def list_foydalanuvchilar():
    db = get_db()
    users = db.execute(
        "SELECT id, login, ism, email, rol, faol, yaratilgan FROM foydalanuvchilar ORDER BY faol DESC, rol, ism"
    ).fetchall()
    takliflar = db.execute(
        """SELECT token, email, rol, yaratilgan, muddat FROM takliflar
           WHERE ishlatilgan IS NULL AND muddat > datetime('now', '+5 hours') ORDER BY yaratilgan DESC"""
    ).fetchall()
    return jsonify({
        "foydalanuvchilar": [dict(r) for r in users],
        "takliflar": [dict(t, havola=f"{ERP_BASE_URL}/taklif/{t['token']}") for t in takliflar],
        "rollar": ROLLAR,
        "men": g.user["id"],
        "email_sozlangan": bool(os.environ.get("SMTP_HOST") and os.environ.get("SMTP_PASSWORD")),
    })


@app.route("/api/takliflar", methods=["POST"])
def taklif_yuborish():
    data = request.get_json(force=True) or {}
    email = (data.get("email") or "").strip().lower()
    rol = data.get("rol")
    if not EMAIL_RE.fullmatch(email):
        return jsonify({"xato": "Укажите корректный email"}), 400
    if rol not in ROLLAR:
        return jsonify({"xato": "Выберите роль"}), 400
    db = get_db()
    if db.execute("SELECT 1 FROM foydalanuvchilar WHERE email = ?", (email,)).fetchone():
        return jsonify({"xato": "Пользователь с таким email уже есть"}), 400
    # Shu emailga oldingi ishlatilmagan takliflar bekor qilinadi
    db.execute("DELETE FROM takliflar WHERE email = ? AND ishlatilgan IS NULL", (email,))
    token = secrets.token_urlsafe(32)
    db.execute(
        f"""INSERT INTO takliflar (token, email, rol, yaratdi, yaratilgan, muddat)
            VALUES (?, ?, ?, ?, datetime('now', '+5 hours'), datetime('now', '+5 hours', '+{TAKLIF_MUDDATI_SOAT} hours'))""",
        (token, email, rol, g.user["id"]),
    )
    db.commit()
    havola = f"{ERP_BASE_URL}/taklif/{token}"
    try:
        yuborildi = taklif_xati_yubor(email, rol, havola)
        xato = None
    except Exception as e:
        yuborildi, xato = False, str(e)
    g.jurnal_tafsilot = f"{email}, роль: {ROLLAR[rol]}" + ("" if yuborildi else " (письмо не отправлено)")
    return jsonify({"ok": True, "havola": havola, "email_yuborildi": yuborildi, "email_xato": xato}), 201


@app.route("/api/takliflar/<token>", methods=["DELETE"])
def taklif_bekor(token):
    db = get_db()
    row = db.execute("SELECT email FROM takliflar WHERE token = ?", (token,)).fetchone()
    db.execute("DELETE FROM takliflar WHERE token = ? AND ishlatilgan IS NULL", (token,))
    db.commit()
    g.jurnal_tafsilot = row["email"] if row else token[:8]
    return jsonify({"ok": True})


@app.route("/taklif/<token>", methods=["GET", "POST"])
def taklif_qabul(token):
    db = get_db()
    taklif = db.execute(
        """SELECT token, email, rol FROM takliflar
           WHERE token = ? AND ishlatilgan IS NULL AND muddat > datetime('now', '+5 hours')""",
        (token,),
    ).fetchone()
    if taklif is None:
        return render_template("taklif.html", yaroqsiz=True), 404

    xato = None
    form = {"ism": "", "login": ""}
    if request.method == "POST":
        form = {"ism": (request.form.get("ism") or "").strip(), "login": (request.form.get("login") or "").strip().lower()}
        parol, parol2 = request.form.get("parol") or "", request.form.get("parol2") or ""
        if not form["ism"]:
            xato = "Укажите ваше имя"
        elif not LOGIN_RE.fullmatch(form["login"]):
            xato = "Логин: 3–32 символа — латинские буквы, цифры, точка, _ или -"
        elif len(parol) < 8:
            xato = "Пароль — минимум 8 символов"
        elif parol != parol2:
            xato = "Пароли не совпадают"
        elif db.execute("SELECT 1 FROM foydalanuvchilar WHERE login = ?", (form["login"],)).fetchone():
            xato = "Такой логин уже занят — придумайте другой"
        else:
            cur = db.execute(
                """INSERT INTO foydalanuvchilar (login, ism, email, parol_hash, rol, yaratilgan)
                   VALUES (?, ?, ?, ?, ?, datetime('now', '+5 hours'))""",
                (form["login"], form["ism"], taklif["email"], generate_password_hash(parol), taklif["rol"]),
            )
            db.execute("UPDATE takliflar SET ishlatilgan = datetime('now', '+5 hours') WHERE token = ?", (token,))
            db.commit()
            jurnalga_yoz("Принято приглашение — создан пользователь",
                         f"{form['ism']} ({form['login']}, {taklif['email']}), роль: {ROLLAR[taklif['rol']]}",
                         user_id=cur.lastrowid, user_login=form["login"])
            session.clear()
            session["user_id"] = cur.lastrowid
            session.permanent = True
            return redirect(url_for("index"))
    return render_template("taklif.html", yaroqsiz=False, taklif=dict(taklif),
                           rol_nomi=ROLLAR[taklif["rol"]], xato=xato, form=form)


@app.route("/api/foydalanuvchilar/<int:user_id>", methods=["PATCH"])
def update_foydalanuvchi(user_id):
    data = request.get_json(force=True) or {}
    db = get_db()
    user = db.execute("SELECT id, login, ism, rol, faol FROM foydalanuvchilar WHERE id = ?", (user_id,)).fetchone()
    if user is None:
        return jsonify({"xato": "Пользователь не найден"}), 404
    yangi_rol = data.get("rol", user["rol"])
    yangi_faol = int(bool(data.get("faol", user["faol"])))
    if yangi_rol not in ROLLAR:
        return jsonify({"xato": "Неизвестная роль"}), 400
    # Tizimda kamida bitta faol admin qolishi shart - aks holda hech kim boshqara olmaydi
    if user["rol"] == "admin" and user["faol"] and (yangi_rol != "admin" or not yangi_faol):
        adminlar = db.execute("SELECT COUNT(*) FROM foydalanuvchilar WHERE rol = 'admin' AND faol = 1").fetchone()[0]
        if adminlar <= 1:
            return jsonify({"xato": "Нельзя: это единственный активный администратор"}), 400
    ozgarish = []
    if yangi_rol != user["rol"]:
        ozgarish.append(f"роль {ROLLAR[user['rol']]} → {ROLLAR[yangi_rol]}")
    if yangi_faol != user["faol"]:
        ozgarish.append("доступ включён" if yangi_faol else "доступ отключён")
    parol = data.get("parol")
    if parol:
        if len(parol) < 8:
            return jsonify({"xato": "Пароль — минимум 8 символов"}), 400
        db.execute("UPDATE foydalanuvchilar SET parol_hash = ? WHERE id = ?", (generate_password_hash(parol), user_id))
        ozgarish.append("пароль сброшен администратором")
    db.execute("UPDATE foydalanuvchilar SET rol = ?, faol = ? WHERE id = ?", (yangi_rol, yangi_faol, user_id))
    db.commit()
    g.jurnal_tafsilot = f"{user['ism']} ({user['login']}): " + "; ".join(ozgarish) if ozgarish else ""
    return jsonify({"ok": True})


@app.route("/api/jurnal", methods=["GET"])
def jurnal_api():
    login_f = (request.args.get("login") or "").strip()
    sql = "SELECT sana, user_login, amal, tafsilot FROM jurnal"
    params = []
    if login_f:
        sql += " WHERE user_login = ?"
        params.append(login_f)
    sql += " ORDER BY id DESC LIMIT 300"
    return jsonify({"jurnal": [dict(r) for r in get_db().execute(sql, params).fetchall()]})


@app.route("/api/uzum-prices")
def uzum_prices_api():
    """promo-agent (boshqa serverda ishlaydi) ERP narxlarini shu orqali
    oladi - sessiya (login) emas, maxfiy token orqali himoyalangan."""
    if request.args.get("token") != ERP_API_TOKEN:
        return jsonify({"error": "forbidden"}), 403

    db = get_db()
    rows = db.execute(
        "SELECT uzum_sku, sotish_narxi, tannarx, logistika, boshqa_harajat, komissiya_foiz, boost_foiz, nomi, ikpu "
        "FROM mahsulotlar WHERE uzum_sku IS NOT NULL AND TRIM(uzum_sku) != ''"
    ).fetchall()

    prices = {}
    for row in rows:
        sku = row["uzum_sku"].split()[0]
        prices[sku] = {
            "sale_price": row["sotish_narxi"],
            "tannarx": row["tannarx"],
            "logistika": row["logistika"],
            "boshqa_harajat": row["boshqa_harajat"],
            "komissiya_foiz": row["komissiya_foiz"],
            "boost_foiz": row["boost_foiz"],
            "nomi": row["nomi"],
            "ikpu": (row["ikpu"] or "").strip() or None,
        }
    return jsonify(prices)


@app.route("/api/uzum-costs", methods=["POST"])
def uzum_costs_api():
    """promo-agent haftalik tekshiruvi Uzum'dagi haqiqiy komissiya va
    logistikani shu orqali yozadi: {uzum_sku: {"komissiya_foiz": .., "logistika": .., "boost_foiz": ..}}.
    O'zgartirishdan oldin baza nusxalanadi."""
    if request.args.get("token") != ERP_API_TOKEN:
        return jsonify({"error": "forbidden"}), 403
    data = request.get_json(force=True) or {}

    db = get_db()
    yangilandi = 0
    rows = db.execute(
        "SELECT id, uzum_sku FROM mahsulotlar WHERE uzum_sku IS NOT NULL AND TRIM(uzum_sku) != ''"
    ).fetchall()
    updates = []
    for row in rows:
        qiymat = data.get(row["uzum_sku"].split()[0])
        if not qiymat:
            continue
        for ustun in ("komissiya_foiz", "logistika", "boost_foiz"):
            if qiymat.get(ustun) is not None:
                updates.append((ustun, float(qiymat[ustun]), row["id"]))
    if updates:
        shutil.copy(DB_PATH, BASE_DIR / f"erp_backup_{time.strftime('%Y%m%d_%H%M%S')}.db")
        for ustun, val, pid in updates:
            db.execute(f"UPDATE mahsulotlar SET {ustun} = ? WHERE id = ?", (val, pid))
            yangilandi += 1
        db.commit()
    return jsonify({"ok": True, "yangilandi": yangilandi})


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
        # Birinchi ishga tushishda (foydalanuvchi yo'q) - eski umumiy parol bilan admin
        if db.execute("SELECT COUNT(*) FROM foydalanuvchilar").fetchone()[0] == 0:
            db.execute(
                "INSERT INTO foydalanuvchilar (login, ism, parol_hash, rol, yaratilgan) "
                "VALUES ('admin', 'Администратор', ?, 'admin', datetime('now', '+5 hours'))",
                (generate_password_hash(ERP_PASSWORD),),
            )
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
        if "komissiya_foiz" not in mahsulot_ustunlari:
            db.execute("ALTER TABLE mahsulotlar ADD COLUMN komissiya_foiz REAL NOT NULL DEFAULT 25")
        # "Буст заказов" - Uzum reklamasi, sotuvdan qo'shimcha foiz oladi
        if "boost_foiz" not in mahsulot_ustunlari:
            db.execute("ALTER TABLE mahsulotlar ADD COLUMN boost_foiz REAL NOT NULL DEFAULT 0")
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


@app.route("/kontragent/<int:kontragent_id>")
def kontragent_sahifa(kontragent_id):
    return render_template("kontragent.html", active="kassa", kontragent_id=kontragent_id)


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
    # SKU kodida bo'shliq bo'lmaydi - Uzum sahifasidan nusxa olganda ketidan
    # narx va "Aksiyada — Sotuvdagi" yozuvi ham qo'shilib qolishi mumkin,
    # shuning uchun faqat birinchi so'zni (kodning o'zini) olamiz.
    uzum_sku = ((data.get("uzum_sku") or "").split() or [None])[0]
    ikpu = (data.get("ikpu") or "").strip() or None
    db = get_db()
    mahsulot = db.execute(
        "SELECT id, nomi, uzum_sku, ikpu FROM mahsulotlar WHERE id = ?", (mahsulot_id,)
    ).fetchone()
    if mahsulot is None:
        return jsonify({"xato": "Товар не найден"}), 404
    ozgarish = [f"{nom}: {mahsulot[k] or '—'} → {v or '—'}"
                for k, nom, v in (("nomi", "название", nomi), ("uzum_sku", "Uzum SKU", uzum_sku), ("ikpu", "ИКПУ", ikpu))
                if (mahsulot[k] or None) != (v or None)]
    g.jurnal_tafsilot = f"{mahsulot['nomi']}: " + "; ".join(ozgarish) if ozgarish else ""
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
        "SELECT id, nomi, rasm FROM mahsulotlar WHERE id = ?", (mahsulot_id,)
    ).fetchone()
    if mahsulot is None:
        return jsonify({"xato": "Товар не найден"}), 404
    g.jurnal_tafsilot = mahsulot["nomi"]

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
        # Uzum komissiyasi kategoriyaga qarab har xil - har mahsulotga qo'lda
        komissiya_foiz = float(data.get("komissiya_foiz", 25))
        boost_foiz = float(data.get("boost_foiz", 0))
        if min(narx_usd, kurs, sotish_narxi, logistika, boshqa_harajat, komissiya_foiz, boost_foiz) < 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"xato": "Все значения должны быть числами не меньше 0"}), 400
    if komissiya_foiz > 100 or boost_foiz > 100:
        return jsonify({"xato": "Комиссия и буст не могут быть больше 100%"}), 400

    tannarx = narx_usd * kurs

    db = get_db()
    mahsulot = db.execute(
        """SELECT id, nomi, narx_usd, kurs, sotish_narxi, logistika, boshqa_harajat, komissiya_foiz, boost_foiz
           FROM mahsulotlar WHERE id = ?""", (mahsulot_id,)
    ).fetchone()
    if mahsulot is None:
        return jsonify({"xato": "Товар не найден"}), 404
    nomlar = {"narx_usd": "цена $", "kurs": "курс", "sotish_narxi": "цена продажи", "logistika": "логистика",
              "boshqa_harajat": "прочие", "komissiya_foiz": "комиссия %", "boost_foiz": "буст %"}
    yangi = {"narx_usd": narx_usd, "kurs": kurs, "sotish_narxi": sotish_narxi, "logistika": logistika,
             "boshqa_harajat": boshqa_harajat, "komissiya_foiz": komissiya_foiz, "boost_foiz": boost_foiz}
    ozgarish = [f"{nomlar[k]} {_raqam(mahsulot[k])} → {_raqam(v)}"
                for k, v in yangi.items() if abs(float(mahsulot[k] or 0) - v) > 1e-9]
    # Avto-saqlash har necha soniyada chaqiriladi - o'zgarish bo'lmasa jurnalga yozilmaydi
    g.jurnal_tafsilot = f"{mahsulot['nomi']}: " + "; ".join(ozgarish) if ozgarish else ""
    db.execute(
        """UPDATE mahsulotlar
           SET tannarx = ?, narx_usd = ?, kurs = ?, sotish_narxi = ?, logistika = ?, boshqa_harajat = ?,
               komissiya_foiz = ?, boost_foiz = ?
           WHERE id = ?""",
        (tannarx, narx_usd, kurs, sotish_narxi, logistika, boshqa_harajat, komissiya_foiz, boost_foiz, mahsulot_id),
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
        "komissiya_foiz": komissiya_foiz,
        "boost_foiz": boost_foiz,
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
        """SELECT id, nomi, qoldiq, tannarx, narx_usd, kurs, sotish_narxi, logistika, boshqa_harajat,
                  komissiya_foiz, boost_foiz, rasm
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


# ---------- kontragentlar ----------

# turi -> (nomi, balansga ta'siri, kassadagi harakat turi)
KONTRAGENT_TURLARI = {
    "tovar_oldim": ("Взял товар в долг", 1, None),
    "tolov": ("Оплатил", -1, "chiqim"),
    "pul_oldim": ("Получил деньги", 1, "kirim"),
    "spisanie": ("Списание", None, None),
}

KONTRAGENT_BALANS_SQL = (
    "(SELECT COALESCE(SUM(summa * belgi), 0) FROM kontragent_harakatlari h WHERE h.kontragent_id = k.id)"
)


def _kontragent(db, kontragent_id):
    return db.execute(
        f"SELECT k.*, {KONTRAGENT_BALANS_SQL} AS balans FROM kontragentlar k WHERE k.id = ?",
        (kontragent_id,),
    ).fetchone()


@app.route("/api/kontragentlar", methods=["GET"])
def list_kontragentlar():
    rows = get_db().execute(
        f"""SELECT k.id, k.nomi, k.telefon, {KONTRAGENT_BALANS_SQL} AS balans,
                   (SELECT MAX(sana) FROM kontragent_harakatlari h WHERE h.kontragent_id = k.id) AS oxirgi
            FROM kontragentlar k ORDER BY k.nomi COLLATE NOCASE"""
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/kontragentlar", methods=["POST"])
def add_kontragent():
    data = request.get_json(force=True)
    nomi = (data.get("nomi") or "").strip()
    if not nomi:
        return jsonify({"xato": "Укажите имя контрагента"}), 400
    db = get_db()
    cur = db.execute(
        "INSERT INTO kontragentlar (nomi, telefon, izoh) VALUES (?, ?, ?)",
        (nomi, (data.get("telefon") or "").strip() or None, (data.get("izoh") or "").strip() or None),
    )
    db.commit()
    g.jurnal_tafsilot = nomi
    return jsonify({"ok": True, "id": cur.lastrowid}), 201


@app.route("/api/kontragentlar/<int:kontragent_id>", methods=["GET"])
def get_kontragent(kontragent_id):
    db = get_db()
    k = _kontragent(db, kontragent_id)
    if k is None:
        return jsonify({"xato": "Контрагент не найден"}), 404
    rows = db.execute(
        "SELECT id, sana, turi, summa, belgi, izoh, kassa_id FROM kontragent_harakatlari "
        "WHERE kontragent_id = ? ORDER BY id DESC",
        (kontragent_id,),
    ).fetchall()
    return jsonify({"kontragent": dict(k), "harakatlar": [dict(r) for r in rows]})


@app.route("/api/kontragentlar/<int:kontragent_id>", methods=["DELETE"])
def delete_kontragent(kontragent_id):
    db = get_db()
    k = _kontragent(db, kontragent_id)
    if k is None:
        return jsonify({"xato": "Контрагент не найден"}), 404
    if db.execute(
        "SELECT 1 FROM kontragent_harakatlari WHERE kontragent_id = ? LIMIT 1", (kontragent_id,)
    ).fetchone():
        return jsonify({"xato": "Сначала удалите операции этого контрагента"}), 400
    db.execute("DELETE FROM kontragentlar WHERE id = ?", (kontragent_id,))
    db.commit()
    g.jurnal_tafsilot = k["nomi"]
    return jsonify({"ok": True})


@app.route("/api/kontragentlar/<int:kontragent_id>/harakat", methods=["POST"])
def add_kontragent_harakat(kontragent_id):
    data = request.get_json(force=True)
    turi = data.get("turi")
    izoh = (data.get("izoh") or "").strip() or None
    if turi not in KONTRAGENT_TURLARI:
        return jsonify({"xato": "Неверный тип операции"}), 400
    try:
        summa = float(data.get("summa"))
        if summa <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"xato": "Сумма должна быть положительным числом"}), 400

    db = get_db()
    k = _kontragent(db, kontragent_id)
    if k is None:
        return jsonify({"xato": "Контрагент не найден"}), 404

    nomi, belgi, kassa_turi = KONTRAGENT_TURLARI[turi]
    if turi == "spisanie":
        # Spisanie qarzni (qaysi tomonda bo'lsa ham) nolga yaqinlashtiradi
        balans = k["balans"]
        if abs(balans) < 0.01:
            return jsonify({"xato": "Долга нет — списывать нечего"}), 400
        if summa > abs(balans) + 0.01:
            return jsonify({"xato": f"Можно списать не больше {abs(balans):,.0f} сум".replace(",", " ")}), 400
        belgi = -1 if balans > 0 else 1

    kassa_id = None
    if kassa_turi and data.get("kassa"):
        kassa_izoh = f"{k['nomi']}: {nomi.lower()}" + (f" — {izoh}" if izoh else "")
        kassa_id = db.execute(
            "INSERT INTO kassa_harakatlari (turi, summa, izoh) VALUES (?, ?, ?)",
            (kassa_turi, summa, kassa_izoh),
        ).lastrowid
    db.execute(
        "INSERT INTO kontragent_harakatlari (kontragent_id, turi, summa, belgi, izoh, kassa_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (kontragent_id, turi, summa, belgi, izoh, kassa_id),
    )
    db.commit()
    g.jurnal_tafsilot = (
        f"{k['nomi']}: {nomi.lower()} {summa:,.0f} сум".replace(",", " ")
        + (" (через кассу)" if kassa_id else "")
    )
    return jsonify({"ok": True}), 201


@app.route("/api/kontragent-harakat/<int:harakat_id>", methods=["DELETE"])
def delete_kontragent_harakat(harakat_id):
    db = get_db()
    h = db.execute(
        "SELECT h.*, k.nomi AS kontragent FROM kontragent_harakatlari h "
        "JOIN kontragentlar k ON k.id = h.kontragent_id WHERE h.id = ?",
        (harakat_id,),
    ).fetchone()
    if h is None:
        return jsonify({"xato": "Операция не найдена"}), 404
    db.execute("DELETE FROM kontragent_harakatlari WHERE id = ?", (harakat_id,))
    if h["kassa_id"]:
        db.execute("DELETE FROM kassa_harakatlari WHERE id = ?", (h["kassa_id"],))
    db.commit()
    g.jurnal_tafsilot = (
        f"{h['kontragent']}: {KONTRAGENT_TURLARI[h['turi']][0].lower()} {h['summa']:,.0f} сум".replace(",", " ")
    )
    return jsonify({"ok": True})


# gunicorn ostida ham (faqat "python app.py" da emas) yangi ustunlar
# qo'shilsin. Ikki worker bir vaqtda ishga tushsa, biri ALTER'ni ikkinchisidan
# oldin bajarib qo'yishi mumkin - unda ikkinchisining xatosi zararsiz.
try:
    init_db()
except sqlite3.OperationalError:
    pass

if __name__ == "__main__":
    app.run(debug=True, port=5000)
