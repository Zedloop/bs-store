"""
PythonAnywhere uchun BIRLASHTIRILGAN WSGI kirish nuqtasi.

Bu faylning MAZMUNINI (pastdagi kodni to'liq) PythonAnywhere'ning "Web" bo'limidagi
WSGI konfiguratsiya fayliga (masalan /var/www/zedloop_pythonanywhere_com_wsgi.py)
TO'LIQ ALMASHTIRIB qo'ying - eski mazmunini o'chirib, shuni joylashtiring.

Nega bunday murakkab: ikkita alohida loyiha (ERP, review-agent) bitta Python
jarayonida ishlaydi, lekin ikkalasida ham bir xil nomli fayllar bor (masalan
config.py, main.py, webapp.py, uzum_auth.py). Agar ularni oddiy import qilsak,
ikkinchisi birinchisining modullarini "bosib" qo'yadi (API kalitlari aralashib
ketishi mumkin edi). Shuning uchun har bir loyiha o'z-o'zidan ALOHIDA,
izolyatsiya qilingan holda yuklanadi.

Eslatma: promo-agent endi bu serverda emas - u alohida VPS'da (Hetzner)
ishlaydi, chunki PythonAnywhere'ning bepul tarifi Uzum'ning marketing
API'siga (api.uzum.uz) chiqishni bloklaydi.
"""
import importlib
import os
import sys
from pathlib import Path

from werkzeug.middleware.dispatcher import DispatcherMiddleware

# PythonAnywhere'dagi username - papkalar shu foydalanuvchi uyida yotadi
PA_USERNAME = "zedloop"
HOME = Path(f"/home/{PA_USERNAME}")

PROJECTS = {
    "erp": HOME / "bs-store",
    "review": HOME / "uzum-review-agent",
}


def _local_module_names(project_dir):
    """Shu papkadagi barcha .py fayllar va __init__.py'li ichki papkalarning
    modul nomlarini aniqlaydi - shularni importdan oldin/keyin xotiradan
    tozalaymiz, boshqa loyiha bilan to'qnashmasligi uchun."""
    names = set()
    for entry in os.listdir(project_dir):
        full = project_dir / entry
        if entry.endswith(".py") and entry != "__init__.py":
            names.add(entry[:-3])
        elif full.is_dir() and (full / "__init__.py").exists():
            names.add(entry)
    return names


def load_app(project_dir, module_name, attr="app"):
    project_dir = str(project_dir)
    local_names = _local_module_names(Path(project_dir))

    # Boshqa loyihadan qolgan, nom to'qnashadigan modullarni vaqtincha
    # xotiradan olib tashlaymiz (keyin qaytarib qo'yamiz)
    saved = {name: sys.modules.pop(name) for name in local_names if name in sys.modules}

    sys.path.insert(0, project_dir)
    try:
        module = importlib.import_module(module_name)
        flask_app = getattr(module, attr)
    finally:
        sys.path.remove(project_dir)
        # Shu loyiha uchun import qilingan modullarni ham tozalaymiz -
        # keyingi loyiha ularni emas, o'zinikini topishi uchun
        for name in local_names:
            sys.modules.pop(name, None)
        sys.modules.update(saved)

    return flask_app


erp_app = load_app(PROJECTS["erp"], "app", "app")
review_app = load_app(PROJECTS["review"], "webapp", "app")

application = DispatcherMiddleware(erp_app, {
    "/review": review_app,
})
