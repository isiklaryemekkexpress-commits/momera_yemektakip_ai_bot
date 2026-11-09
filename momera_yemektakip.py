import asyncio
import base64
import json
import logging
import os
import re
import time
from datetime import datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# --- Log ayarları ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
# httpx logları bot token'ını URL içinde yazdığı için sessize alınıyor
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# --- Ayarlar (hepsi Render > Environment'tan okunur) ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
# Groq'un resim okuyabilen güncel modeli. Değişirse Render'da GROQ_MODEL ile güncelle.
GROQ_MODEL = os.environ.get("GROQ_MODEL", "qwen/qwen3.8-27b")
GROQ_VISION_MODEL = os.environ.get("GROQ_VISION_MODEL", GROQ_MODEL)
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

SHEET_NAME = os.environ.get("SHEET_NAME", "yemekkexpress_yemektakip_AI")
SHEET_ID = "1ryMqkQPGOvFcl5k9t46pF98dHM6WMntPVi4ZN9RB0nw"
SHEET_TAB_NAME = "ListBot"  # Yalnızca bu sekmeye yazılır
SOURCE_TAB_NAME = "Günlük Takip"  # Ürün adları C5:C ve türleri B5:B
# Boş bırakılırsa herkes kullanabilir. Doluysa virgülle ayrılmış Telegram kullanıcı ID'leri.
ALLOWED_USER_IDS = {
    int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x
}

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")  # opsiyonel
PORT = int(os.environ.get("PORT", "10000"))
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL")

TELEGRAM_MAX_LEN = 4096
MAX_IMAGE_BYTES = 3_500_000
HISTORY_LIMIT = 10
TIMEZONE = ZoneInfo("Europe/Istanbul")

SHEETS_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

BASE_SYSTEM_PROMPT = (
    "Sen Momera Yemek Takip yapay zeka asistanısın. "
    "Kullanıcılara Türkçe, kısa ve anlaşılır yanıtlar ver. "
    f"Bir Google Sheets tablosuna ('{SHEET_NAME}') bağlısın: tablonun içeriğini okuyabilir, "
    "sorulara tablodaki verilere dayanarak cevap verebilir, fotoğraftaki bilgileri tabloya "
    "satır olarak ekleyebilir ve kullanıcı isterse tabloya yazı yazabilirsin. "
    "Her yazma işlemi izin kurallarına ve çoğunlukla kullanıcı onayına tabidir. "
    "Satır ya da veri silemezsin."
)


def load_rules() -> str:
    """Konuşma kurallarını kurallar.md dosyasından okur."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kurallar.md")
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        logger.warning("kurallar.md bulunamadı, yalnızca temel istem kullanılacak.")
        return ""


RULES = load_rules()
SYSTEM_PROMPT = BASE_SYSTEM_PROMPT + ("\n\nKurallar:\n" + RULES if RULES else "")

EXTRACT_PROMPT = (
    SYSTEM_PROMPT
    + "\n\nFotoğraftaki yemek ve miktar bilgilerini çıkar. "
    "Yalnızca geçerli JSON ver: "
    '{"ozet":"kısa özet","satirlar":[{"Yemek Adı":"...","Gelen Adet":"...","Satılan Adet":"...","Fire":"...","Stok":"...","Devreden":"..."}]}. '
    "Fotoğrafta bulunmayan alanları boş bırak. Her yemek ayrı satırdır. "
    "Yemek isimlerini tahmin ederek düzeltme; kod kaynak tablodan doğrulayacak. "
    "Tarih, No ve Eklenme Zamanı alanlarını üretme."
)


# ------------------------------------------------------------ Yazma izinleri
def _norm(s) -> str:
    """Türkçe karakterleri sadeleştirip küçük harfe çevirir (karşılaştırma için)."""
    return str(s).strip().translate(str.maketrans("ıİşŞğĞüÜöÖçÇ", "iissgguuoocc")).lower()


def _liste(value: str) -> list[str]:
    return [x.strip() for x in re.split(r"[,;]", value) if x.strip()]


def _bos_mu(value: str) -> bool:
    return _norm(value) in ("", "yok", "hicbiri", "hepsi", "tumu", "-")


def load_permissions(rules: str) -> dict:
    """kurallar.md içindeki '## Yazma izinleri' bölümünü okur. Bu sınırlar kodda uygulanır."""
    perm = {
        "yazma": True,
        "izinli_islemler": {"satir_ekle", "hucre_yaz"},
        "korumali_sutunlar": set(),
        "yazilabilir_sutunlar": set(),  # boşsa sütun kısıtı yok
        "korumali_satirlar": {1},
        "onay": True,
        "en_fazla_satir": 10,
    }
    in_section = False
    for raw in rules.splitlines():
        line = raw.strip()
        if line.startswith("#"):
            in_section = _norm(line.lstrip("# ")).startswith("yazma izinleri")
            continue
        if not in_section:
            continue
        line = line.lstrip("-* ").strip()
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = _norm(key).replace(" ", "_")
        value = value.strip()
        if key == "yazma":
            perm["yazma"] = _norm(value) in ("acik", "evet", "true", "on", "1")
        elif key == "izinli_islemler":
            perm["izinli_islemler"] = (
                set()
                if _norm(value) in ("yok", "hicbiri", "-")
                else {_norm(x).replace(" ", "_") for x in _liste(value)}
            )
        elif key == "korumali_sutunlar":
            perm["korumali_sutunlar"] = set() if _bos_mu(value) else {_norm(x) for x in _liste(value)}
        elif key == "yazilabilir_sutunlar":
            perm["yazilabilir_sutunlar"] = set() if _bos_mu(value) else {_norm(x) for x in _liste(value)}
        elif key == "korumali_satirlar":
            perm["korumali_satirlar"] = {int(x) for x in _liste(value) if x.isdigit()}
        elif key == "onay":
            perm["onay"] = _norm(value) not in ("gerekmez", "yok", "hayir", "kapali")
        elif key == "en_fazla_satir" and value.isdigit():
            perm["en_fazla_satir"] = max(1, int(value))
    return perm


PERMS = load_permissions(RULES)


def izin_ozeti() -> str:
    if not PERMS["yazma"]:
        return "Yazma işlemleri kapalı; hiçbir yazma işlemi üretme."
    parcalar = [
        "Yazma açık",
        "izinli işlemler: " + (", ".join(sorted(PERMS["izinli_islemler"])) or "yok"),
        "onay: " + ("gerekli" if PERMS["onay"] else "gerekmez"),
        f"tek seferde en fazla {PERMS['en_fazla_satir']} satır",
    ]
    if PERMS["korumali_sutunlar"]:
        parcalar.append("korumalı sütunlar: " + ", ".join(sorted(PERMS["korumali_sutunlar"])))
    if PERMS["yazilabilir_sutunlar"]:
        parcalar.append("yalnızca şu sütunlara yazılabilir: " + ", ".join(sorted(PERMS["yazilabilir_sutunlar"])))
    if PERMS["korumali_satirlar"]:
        parcalar.append("korumalı satırlar: " + ", ".join(str(x) for x in sorted(PERMS["korumali_satirlar"])))
    return "; ".join(parcalar)


# ---------------------------------------------------------------- Yardımcılar
def split_text(text: str, limit: int = TELEGRAM_MAX_LEN) -> list[str]:
    """Telegram 4096 karakter sınırı için uzun yanıtları parçalara böler."""
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        parts.append(text)
    return parts


async def izinli_mi(update: Update) -> bool:
    """ALLOWED_USER_IDS tanımlıysa yalnızca listedeki kullanıcılara izin verir."""
    if not ALLOWED_USER_IDS:
        return True
    user = update.effective_user
    if user and user.id in ALLOWED_USER_IDS:
        return True
    logger.warning("Yetkisiz kullanıcı engellendi: %s", user.id if user else "?")
    if update.effective_message:
        await update.effective_message.reply_text("Bu botu kullanma yetkiniz yok.")
    return False


# ------------------------------------------------------------------- Groq
async def groq_chat(messages: list[dict], model: str, json_mode: bool = False) -> str:
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        # Ücretsiz katmanda dakikada 1000 çıktı tokenı sınırı var; isteği buna göre kısıtla.
        "max_completion_tokens": 900 if json_mode else 500,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    async with httpx.AsyncClient(timeout=120) as http:
        resp = await http.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            json=payload,
        )
    if resp.status_code >= 400:
        logger.error("Groq hatası %s: %s", resp.status_code, resp.text[:500])
        resp.raise_for_status()
    text = resp.json()["choices"][0]["message"].get("content") or ""
    # Bazı modeller düşünme bloğunu da döndürür; kullanıcıya göstermiyoruz.
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()


def parse_json(text: str) -> dict:
    text = text.strip()
    fence = chr(96) * 3
    if text.startswith(fence):
        text = text.strip(chr(96)).strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()
    return json.loads(text)


# ------------------------------------------------------------- Google Sheets
_session = None
_sheet_cache: dict = {}


def _session_get() -> AuthorizedSession:
    global _session
    if _session is None:
        info = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=SHEETS_SCOPES
        )
        _session = AuthorizedSession(creds)
    return _session


def _sheet_meta() -> tuple[str, str]:
    """Google Sheets dosyasında yalnızca ListBot sekmesini seçer."""
    if _sheet_cache:
        return _sheet_cache["id"], _sheet_cache["title"]

    s = _session_get()
    sid = SHEET_ID
    r = s.get(
        f"https://sheets.googleapis.com/v4/spreadsheets/{sid}",
        params={"fields": "sheets.properties(title)"},
    )
    r.raise_for_status()
    for sheet in r.json().get("sheets", []):
        title = sheet["properties"]["title"]
        if title == SHEET_TAB_NAME:
            _sheet_cache.update({"id": sid, "title": title})
            return sid, title
    raise RuntimeError(
        f"'{SHEET_TAB_NAME}' sekmesi bulunamadı. "
        "Sekme adının Google Sheets'te birebir aynı olduğundan emin olun."
    )


def _cell(value):
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return value


CELL_RE = re.compile(r"^([A-Za-z]{1,2})([1-9][0-9]{0,3})$")
MAX_COL = 702  # ZZ
MAX_ROW = 5000


def col_to_index(letters: str) -> int:
    n = 0
    for ch in letters.upper():
        n = n * 26 + (ord(ch) - 64)
    return n


def index_to_col(n: int) -> str:
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _values_url(rng: str) -> str:
    sid, title = _sheet_meta()
    return (
        f"https://sheets.googleapis.com/v4/spreadsheets/{sid}/values/"
        + quote(f"'{title}'!{rng}")
    )


def header_sync() -> list[str]:
    r = _session_get().get(_values_url("A1:ZZ1"))
    r.raise_for_status()
    return (r.json().get("values") or [[]])[0]


def read_cell_sync(cell: str) -> str:
    r = _session_get().get(_values_url(cell))
    r.raise_for_status()
    values = r.json().get("values") or []
    return str(values[0][0]) if values and values[0] else ""


def _to_cell_value(value):
    """Sayı gibi görünen metni sayıya çevirir; gerisini metin olarak bırakır (RAW)."""
    if isinstance(value, (int, float)):
        return value
    text = str(value).strip()
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?\d+[.,]\d+", text):
        return float(text.replace(",", "."))
    return text


def write_cell_sync(cell: str, value) -> None:
    r = _session_get().put(
        _values_url(cell),
        params={"valueInputOption": "RAW"},
        json={"values": [[_to_cell_value(value)]]},
    )
    r.raise_for_status()


LISTBOT_HEADERS = [
    "Tarih", "No", "Yemek Türü", "Yemek Adı", "Stok",
    "Gelen Adet", "Satılan Adet", "Fire", "Devreden", "Eklenme Zamanı",
]


def _values_read_sync(tab: str, rng: str) -> list[list]:
    sid, _ = _sheet_meta()
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{sid}/values/{quote(f"'{tab}'!{rng}")}" 
    r = _session_get().get(url)
    r.raise_for_status()
    return r.json().get("values") or []


def _product_key(value: str) -> str:
    # Yalnızca boşluk ve harf büyüklüğü farklarını yok say; belirsiz eşleşme yapma.
    return " ".join(_norm(value).split())


def _product_catalog_sync() -> dict:
    data = _values_read_sync(SOURCE_TAB_NAME, "B5:C5000")
    catalog = {}
    for row in data:
        if len(row) < 2 or not str(row[1]).strip():
            continue
        kind, name = str(row[0]).strip(), str(row[1]).strip()
        key = _product_key(name)
        # Aynı isim tekrar ediyorsa ilk görülen ürün kaydını koru.
        # Böylece farklı türle tekrarlansa bile kayıt işlemi durmaz.
        if key not in catalog:
            catalog[key] = (kind, name)
    if not catalog:
        raise ValueError("Günlük Takip!C5:C ürün listesi boş veya okunamadı.")
    return catalog


def _normalize_listbot_row(row: dict, catalog: dict, date: str, timestamp: str) -> dict:
    # Sütun adlarını standartlaştır, tanınmayan alanları reddet.
    aliases = { _norm(k): k for k in LISTBOT_HEADERS }
    aliases.update({"urun": "Yemek Adı", "urun adi": "Yemek Adı", "yemek": "Yemek Adı", "miktar": "Gelen Adet"})
    clean = {}
    for k, v in row.items():
        key = aliases.get(_norm(k))
        if not key:
            raise ValueError(f"Tanınmayan sütun: {k}")
        clean[key] = v
    requested = str(clean.get("Yemek Adı") or "").strip()
    match = catalog.get(_product_key(requested))
    if not match:
        raise ValueError(f"Ürün Günlük Takip C sütununda birebir bulunamadı: {requested}")
    kind, canonical = match
    clean["Yemek Türü"] = kind
    clean["Yemek Adı"] = canonical
    clean["Tarih"] = date
    clean["Eklenme Zamanı"] = timestamp
    clean.pop("No", None)  # ListBot'ta sıra numarası otomatik verilir
    return clean


def _format_listbot_header_sync(s, sid: str) -> None:
    """ListBot'un başlık satırını örnekteki mavi/beyaz görünüme yaklaştırır."""
    r = s.get(f"https://sheets.googleapis.com/v4/spreadsheets/{sid}",
              params={"fields": "sheets.properties(sheetId,title)"})
    r.raise_for_status()
    sheet_id = next((sh["properties"]["sheetId"] for sh in r.json().get("sheets", [])
                     if sh["properties"]["title"] == SHEET_TAB_NAME), None)
    if sheet_id is None:
        raise RuntimeError("ListBot sekmesi bulunamadı")
    requests = [{"repeatCell": {
        "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1,
                  "startColumnIndex": 0, "endColumnIndex": 10},
        "cell": {"userEnteredFormat": {
            "backgroundColor": {"red": 0.14, "green": 0.38, "blue": 0.89},
            "textFormat": {"foregroundColor": {"red": 1, "green": 1, "blue": 1},
                           "bold": True, "fontSize": 12},
            "verticalAlignment": "MIDDLE", "wrapStrategy": "WRAP"}},
        "fields": "userEnteredFormat"
    }}, {"updateDimensionProperties": {
        "range": {"sheetId": sheet_id, "dimension": "ROWS", "startIndex": 0, "endIndex": 1},
        "properties": {"pixelSize": 52}, "fields": "pixelSize"
    }}]
    widths = [115, 60, 160, 240, 90, 120, 120, 85, 110, 175]
    for idx, width in enumerate(widths):
        requests.append({"updateDimensionProperties": {
            "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                      "startIndex": idx, "endIndex": idx + 1},
            "properties": {"pixelSize": width}, "fields": "pixelSize"}})
    resp = s.post(f"https://sheets.googleapis.com/v4/spreadsheets/{sid}:batchUpdate",
                  json={"requests": requests})
    resp.raise_for_status()


def append_rows_sync(rows: list[dict]) -> int:
    """Yalnızca ListBot'a, sabit sütun sırasıyla ve doğrulanmış ürün adıyla ekler."""
    s = _session_get()
    sid, title = _sheet_meta()
    now = datetime.now(TIMEZONE)
    date, timestamp = now.strftime("%d.%m.%Y"), now.strftime("%d.%m.%Y %H:%M:%S")
    catalog = _product_catalog_sync()
    prepared = [_normalize_listbot_row(row, catalog, date, timestamp) for row in rows]
    current = _values_read_sync(title, "A1:J5000")
    if current and any(str(x).strip() for x in current[0]):
        if list(current[0]) != LISTBOT_HEADERS:
            raise ValueError("ListBot 1. satır başlıkları beklenen A:J düzeninde değil. Mevcut veriler korunuyor; elle kontrol edin.")
    else:
        r = s.put(_values_url("A1:J1"), params={"valueInputOption": "RAW"}, json={"values": [LISTBOT_HEADERS]})
        r.raise_for_status()
        _format_listbot_header_sync(s, sid)
    existing_numbers = []
    for row in current[1:]:
        if len(row) > 1 and str(row[1]).strip().isdigit():
            existing_numbers.append(int(row[1]))
    next_no = max(existing_numbers, default=0) + 1
    values = []
    for i, row in enumerate(prepared):
        row["No"] = next_no + i
        values.append([_cell(row.get(h, "")) for h in LISTBOT_HEADERS])
    # A:J ile sınırlandır; başlık satırını asla değiştirme.
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{sid}/values/{quote(f"'{title}'!A1:J5000")}:append"
    r = s.post(url, params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"}, json={"values": values})
    r.raise_for_status()
    return len(values)


TABLE_CONTEXT_CHARS = int(os.environ.get("TABLE_CONTEXT_CHARS", "6000"))
_table_cache = {"t": 0.0, "text": ""}


def read_table_sync() -> str:
    """Tablonun içeriğini satır numaralarıyla, TAB ile ayrılmış metin olarak döndürür."""
    s = _session_get()
    sid, title = _sheet_meta()
    rng = quote(f"'{title}'!A1:ZZ5000")
    r = s.get(f"https://sheets.googleapis.com/v4/spreadsheets/{sid}/values/{rng}")
    r.raise_for_status()
    values = r.json().get("values") or []
    if not values:
        return ""
    header, rows = values[0], values[1:]
    head_line = "Satır\t" + "\t".join(f"[{index_to_col(i)}] {h}" for i, h in enumerate(header, start=1))
    numbered = [
        f"{i}\t" + "\t".join(str(c) for c in row)
        for i, row in enumerate(rows, start=2)
    ]
    # Yer sınırı için en yeni satırlardan geriye doğru, sığdığı kadarını al.
    budget = TABLE_CONTEXT_CHARS - len(head_line)
    secilen = []
    for line in reversed(numbered):
        if budget - len(line) - 1 < 0:
            break
        secilen.append(line)
        budget -= len(line) + 1
    secilen.reverse()
    atlanan = len(numbered) - len(secilen)
    out = "\n".join([head_line] + secilen)
    if atlanan > 0:
        out += (
            f"\n(Not: Tabloda toplam {len(numbered)} veri satırı var; "
            f"yer sınırı nedeniyle en eski {atlanan} satır gösterilmiyor.)"
        )
    return out


async def table_context() -> str:
    """Tablo içeriğini 20 saniyelik önbellekle okur."""
    now = time.monotonic()
    if _table_cache["t"] and now - _table_cache["t"] < 20:
        return _table_cache["text"]
    text = await asyncio.to_thread(read_table_sync)
    _table_cache.update(t=now, text=text)
    return text


async def append_rows(rows: list[dict]) -> int:
    n = await asyncio.to_thread(append_rows_sync, rows)
    _table_cache["t"] = 0.0  # yeni satırlar bir sonraki soruda görünsün
    return n


async def write_cell(cell: str, value) -> None:
    await asyncio.to_thread(write_cell_sync, cell, value)
    _table_cache["t"] = 0.0


# --------------------------------------------------------- Telegram işleyicileri
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/start komutu verildiğinde çalışır."""
    if not await izinli_mi(update):
        return
    await update.message.reply_text(
        "Merhaba! Ben Momera Yemek Takip yapay zeka asistanınızım.\n"
        "Bana yazabilir ya da bir fotoğraf gönderebilirsiniz. "
        "Fotoğraftaki bilgileri okuyup onayınızla tabloya eklerim."
    )


async def dogrula(action: dict) -> tuple[dict | None, str]:
    """İşlemi kurallar.md'deki izinlere göre denetler. (temiz_işlem, hata_mesajı) döndürür."""
    if not PERMS["yazma"]:
        return None, "Yazma işlemleri şu an kapalı (kurallar.md)."
    tip = _norm(action.get("tip", "")).replace(" ", "_")
    if tip not in PERMS["izinli_islemler"]:
        return None, f"'{action.get('tip')}' işlemine izin verilmiyor (kurallar.md)."

    if tip == "satir_ekle":
        rows = [r for r in action.get("satirlar", []) if isinstance(r, dict) and r]
        if not rows:
            return None, "Eklenecek bir satır bulunamadı."
        if len(rows) > PERMS["en_fazla_satir"]:
            return None, f"Tek seferde en fazla {PERMS['en_fazla_satir']} satır eklenebilir."
        izinsiz = set()
        for row in rows:
            for k in row:
                kn = _norm(k)
                if kn in PERMS["korumali_sutunlar"] or (
                    PERMS["yazilabilir_sutunlar"] and kn not in PERMS["yazilabilir_sutunlar"]
                ):
                    izinsiz.add(str(k))
        if izinsiz:
            return None, "Şu sütunlara yazma izni yok: " + ", ".join(sorted(izinsiz))
        return {"tip": "satir_ekle", "satirlar": rows}, ""

    if tip == "hucre_yaz":
        m = CELL_RE.match(str(action.get("hucre", "")).strip())
        if not m:
            return None, "Hücre adı geçersiz. Örnek: A2"
        col, row = m.group(1).upper(), int(m.group(2))
        if col_to_index(col) > MAX_COL or row > MAX_ROW:
            return None, "Bu hücreye yazılamaz."
        if row in PERMS["korumali_satirlar"]:
            return None, f"{row}. satır korumalı, değiştirilemez (kurallar.md)."
        deger = action.get("deger")
        if isinstance(deger, (dict, list)) or deger is None or len(str(deger)) > 500:
            return None, "Yazılacak değer geçersiz."
        ihtiyac_var = bool(PERMS["korumali_sutunlar"] or PERMS["yazilabilir_sutunlar"])
        header_adi = ""
        try:
            header = await asyncio.to_thread(header_sync)
            idx = col_to_index(col)
            header_adi = str(header[idx - 1]) if idx - 1 < len(header) else ""
        except Exception:
            logger.exception("Başlık satırı okunamadı")
            if ihtiyac_var:
                return None, "Tablo okunamadığı için sütun izni denetlenemedi; işlem yapılmadı."
        kimlikler = {_norm(col)} | ({_norm(header_adi)} if header_adi else set())
        if kimlikler & PERMS["korumali_sutunlar"]:
            return None, f"{col} sütunu korumalı, değiştirilemez (kurallar.md)."
        if PERMS["yazilabilir_sutunlar"] and not (kimlikler & PERMS["yazilabilir_sutunlar"]):
            return None, f"{col} sütununa yazma izni yok (kurallar.md)."
        try:
            eski = await asyncio.to_thread(read_cell_sync, f"{col}{row}")
        except Exception:
            eski = ""
        return {"tip": "hucre_yaz", "hucre": f"{col}{row}", "deger": deger, "eski": eski}, ""

    return None, "Bu işlem desteklenmiyor."


def _satir_onizleme(rows: list[dict]) -> str:
    lines = []
    for i, row in enumerate(rows, 1):
        alanlar = "; ".join(f"{k}: {v}" for k, v in row.items() if v not in (None, ""))
        lines.append(f"{i}. {alanlar or '(boş satır)'}")
    return "\n".join(lines)


def _onizleme(action: dict) -> str:
    if action["tip"] == "satir_ekle":
        return "Tabloya eklenecek satırlar:\n" + _satir_onizleme(action["satirlar"])
    return (
        f"{action['hucre']} hücresi değiştirilecek.\n"
        f"Eski değer: {action.get('eski') or '(boş)'}\n"
        f"Yeni değer: {action['deger']}"
    )


async def islem_calistir(msg, action: dict) -> bool:
    """İşlemi yeniden denetleyip uygular. Tekrar denenebilecek bir hata olursa False döner."""
    temiz, hata = await dogrula(action)
    if temiz is None:
        await msg.reply_text(hata)
        return True
    try:
        if temiz["tip"] == "satir_ekle":
            n = await append_rows(temiz["satirlar"])
            text = f"{n} satır '{SHEET_NAME}' tablosuna eklendi."
        else:
            await write_cell(temiz["hucre"], temiz["deger"])
            text = f"{temiz['hucre']} hücresine '{temiz['deger']}' yazıldı."
    except Exception:
        logger.exception("Tabloya yazılırken hata oluştu")
        await msg.reply_text(
            "Tabloya yazarken hata oluştu. Biraz sonra tekrar onay düğmesine basabilirsiniz."
        )
        return False
    await msg.reply_text(text)
    return True


async def islem_hazirla(msg, context: ContextTypes.DEFAULT_TYPE, action: dict):
    """İşlemi denetler; izin varsa onay ister (ya da onay gerekmiyorsa doğrudan uygular)."""
    temiz, hata = await dogrula(action)
    if temiz is None:
        await msg.reply_text(hata)
        return
    if not PERMS["onay"]:
        await islem_calistir(msg, temiz)
        return
    context.user_data["bekleyen"] = temiz
    buttons = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Evet, uygula", callback_data="onay"),
            InlineKeyboardButton("Hayır", callback_data="iptal"),
        ]]
    )
    await msg.reply_text(
        (_onizleme(temiz) + "\n\nOnaylıyor musunuz?")[:TELEGRAM_MAX_LEN],
        reply_markup=buttons,
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Yazılı mesajı Groq'a iletir; cevabı gösterir, gerekirse yazma işlemini hazırlar."""
    if not await izinli_mi(update):
        return
    user_text = update.message.text
    history = context.user_data.setdefault("gecmis", [])

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action=ChatAction.TYPING
    )
    try:
        tablo = await table_context()
        tablo_notu = (
            "Tablonun güncel içeriği (TAB ile ayrılmış; ilk sütun tablodaki satır numarası, "
            "sütun başlıklarındaki köşeli parantez sütun harfidir):\n" + tablo
            if tablo
            else "Tablo şu an boş."
        )
    except Exception:
        logger.exception("Tablo okunamadı")
        tablo_notu = (
            "Tablo şu an okunamadı. Tablo verisi gerektiren sorularda bunu söyle, tahmin etme."
        )
    sistem = (
        SYSTEM_PROMPT
        + "\n\n"
        + tablo_notu
        + "\n\nTabloyla ilgili sorularda yalnızca bu veriye dayan; veride olmayan bir şeyi uydurma. "
        "Toplam ya da ortalama gibi hesapları satırları tek tek toplayarak dikkatle yap. "
        "Verinin sonunda 'gösterilmiyor' notu varsa, hesabın yalnızca gösterilen satırlara ait olduğunu açıkça belirt."
        "\n\nYanıtını YALNIZCA geçerli bir JSON nesnesi olarak ver: "
        '{"cevap": "kullanıcıya gösterilecek metin", "islem": null}. '
        "Kullanıcı tabloya bir şey yazmak istediğinde islem alanını doldur: "
        'yeni satır için {"tip": "satir_ekle", "satirlar": [{"Sütun adı": "değer"}]}; '
        'tek bir hücreyi değiştirmek için {"tip": "hucre_yaz", "hucre": "A2", "deger": "değer"}. '
        "Yalnızca kullanıcı açıkça isterse islem üret; soru sorulduğunda islem null olsun. "
        "Silme gibi desteklenmeyen işlemlerde islem null olsun ve cevapta bunu yapamadığını söyle. "
        "cevap alanına işlemi yaptığını yazma; ne yapılacağını kısaca özetle, onayı sistem ister. "
        "Yeni satır eklerken Yemek Adı alanını mutlaka belirt. "
        "Yemek Türü, No, Tarih ve Eklenme Zamanı otomatik belirlenir. "
        "Ürün adı Günlük Takip C sütununda doğrulanır; eşleşmeyen kayıt eklenmez. "
        "Geçerli yazma izinleri: " + izin_ozeti() + "."
    )
    messages = (
        [{"role": "system", "content": sistem}]
        + history
        + [{"role": "user", "content": user_text}]
    )
    try:
        raw = await groq_chat(messages, GROQ_MODEL, json_mode=True)
    except Exception:
        logger.exception("Groq çağrısında hata oluştu")
        await update.message.reply_text("Üzgünüm, bir hata oluştu. Lütfen tekrar deneyin.")
        return

    try:
        data = parse_json(raw)
        cevap = str(data.get("cevap") or "").strip()
        islem = data.get("islem")
    except Exception:
        cevap, islem = raw.strip(), None
    cevap = cevap or ("Bir yanıt üretilemedi." if not isinstance(islem, dict) else "")

    history += [
        {"role": "user", "content": user_text},
        {"role": "assistant", "content": cevap or "(işlem hazırlandı)"},
    ]
    del history[:-HISTORY_LIMIT]

    for part in split_text(cevap) if cevap else []:
        await update.message.reply_text(part)
    if isinstance(islem, dict) and islem:
        await islem_hazirla(update.message, context, islem)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fotoğrafı Groq ile okutur, sonucu gösterip onay ister."""
    if not await izinli_mi(update):
        return
    msg = update.message
    if not PERMS["yazma"] or "satir_ekle" not in PERMS["izinli_islemler"]:
        await msg.reply_text("Fotoğraftan tabloya ekleme şu an kapalı (kurallar.md).")
        return
    if msg.photo:
        tg_file = await msg.photo[-1].get_file()
        mime = "image/jpeg"
    else:
        tg_file = await msg.document.get_file()
        mime = msg.document.mime_type or "image/jpeg"

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action=ChatAction.TYPING
    )
    data = bytes(await tg_file.download_as_bytearray())
    if len(data) > MAX_IMAGE_BYTES:
        await msg.reply_text(
            "Fotoğraf çok büyük. Lütfen dosya olarak değil, normal fotoğraf olarak gönderin."
        )
        return

    b64 = base64.b64encode(data).decode()
    caption = msg.caption or "Fotoğraftaki bilgileri tabloya aktar."
    messages = [
        {"role": "system", "content": EXTRACT_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": caption},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ],
        },
    ]
    try:
        raw = await groq_chat(messages, GROQ_VISION_MODEL, json_mode=True)
        result = parse_json(raw)
        rows = [r for r in result.get("satirlar", []) if isinstance(r, dict) and r]
    except Exception:
        logger.exception("Fotoğraf okunurken hata oluştu")
        await msg.reply_text(
            "Fotoğrafı okuyamadım. Daha net ve yakından çekip tekrar gönderebilir misiniz?"
        )
        return

    if not rows:
        await msg.reply_text(
            f"Tabloya eklenecek bir bilgi bulamadım. {result.get('ozet', '')}".strip()
        )
        return

    if result.get("ozet"):
        await msg.reply_text(str(result["ozet"])[:TELEGRAM_MAX_LEN])
    await islem_hazirla(msg, context, {"tip": "satir_ekle", "satirlar": rows})


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Onay düğmelerini işler."""
    query = update.callback_query
    if ALLOWED_USER_IDS and query.from_user.id not in ALLOWED_USER_IDS:
        await query.answer("Yetkiniz yok.", show_alert=True)
        return
    await query.answer()
    action = context.user_data.pop("bekleyen", None)
    if query.data == "iptal" or not action:
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(
            "Tamam, yapmadım." if query.data == "iptal" else "Bekleyen işlem yok."
        )
        return
    await query.edit_message_reply_markup(reply_markup=None)
    if not await islem_calistir(query.message, action):
        context.user_data["bekleyen"] = action  # tekrar denenebilsin


def main():
    missing = [
        n for n in ("TELEGRAM_BOT_TOKEN", "GROQ_API_KEY", "GOOGLE_CREDENTIALS_JSON")
        if not os.environ.get(n)
    ]
    if missing:
        logger.warning("Eksik ortam değişkenleri: %s", ", ".join(missing))
    logger.info("Groq modeli: %s | Resim modeli: %s", GROQ_MODEL, GROQ_VISION_MODEL)
    logger.info("Yazma izinleri: %s", izin_ozeti())

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_button, pattern="^(onay|iptal)$"))

    if RENDER_EXTERNAL_URL:
        # Render (Web Service): webhook modu, Render'ın verdiği PORT'u dinler
        logger.info("Webhook modunda başlatılıyor: %s/webhook", RENDER_EXTERNAL_URL)
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path="webhook",
            webhook_url=f"{RENDER_EXTERNAL_URL}/webhook",
            secret_token=WEBHOOK_SECRET,
            drop_pending_updates=True,
        )
    else:
        # Kendi bilgisayarında test ederken: polling modu
        logger.info("Polling modunda başlatılıyor...")
        app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
