import asyncio
import base64
import difflib
import unicodedata
import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo
from contextlib import asynccontextmanager
import logging
import os
from pathlib import Path

from openai import AsyncOpenAI
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
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

# ============================================================
# AYARLAR — model ve davranış ayarları burada, kodun içinde.
# Render > Environment'ta yalnızca gizli anahtarlar durur:
#   TELEGRAM_BOT_TOKEN, GROQ_API_KEY, EVREN_API_KEY (WEBHOOK_SECRET opsiyonel)
# ============================================================

# Sağlayıcılar. Anahtarı Render'da tanımlı olanlar kullanılır.
# DİKKAT: Groq (groq.com, "gsk_") ile Grok (xAI, "xai-") farklı şirketlerdir.
PROVIDERS = {
    "groq": {"base_url": "https://api.groq.com/openai/v1", "key_env": "GROQ_API_KEY"},
    "evren": {"base_url": "https://evren-llmapi.ssyz.org.tr/v1", "key_env": "EVREN_API_KEY"},
    "grok": {"base_url": "https://api.x.ai/v1", "key_env": "GROK_API_KEY"},
}

# Model sıraları: (sağlayıcı, model, max_tokens). Yukarıdan aşağı denenir;
# biri hata verirse (limit, dolu, yanıt yarım kaldı) sıradakine geçilir.
# Groq ücretsiz katmanında dakikada 1000 çıktı token sınırı var, bu yüzden
# max_tokens 1000'in altında tutuluyor; uzun listelerde EVREN devreye girer.
TEXT_CHAIN = [
    ("groq", "qwen/qwen3.8-27b", 900),
    ("evren", "deepseek-v4.1-flash", 4096),
]
# Groq resim için kullanılmıyor: ücretsiz katmandaki 900 token sınırı
# 40+ satırlık listeye yetmiyor ve model listeyi kısaltıyor.
VISION_CHAIN = [
    ("evren", "gemma-4-31b", 4096),      # el yazısı çözümleme
    ("evren", "qwen3-vl-30b", 4096),     # tablo / belge okuma
    ("evren", "deepseek-v4.1-flash", 4096),
]

# --- Google Sheets ---
# Tablonun adresindeki ID: docs.google.com/spreadsheets/d/<BURASI>/edit
SPREADSHEET_ID = "1ryMqkQPGOvFcl5k9t46pF98dHM6WMntPVi4ZN9RB0nw"
LISTBOT_SHEET = "LISTBOT"
MATRIX_SHEET = "EslesmeMatrisi"  # A: LISTBOT'a kaydedilecek ad, B: resimde okunan ad
FUZZY_THRESHOLD = 0.85  # yaklaşık eşleşme için benzerlik eşiği (0-1)
TIMEZONE = ZoneInfo("Europe/Istanbul")

# Kullanıcı bunlardan birini yazarsa liste kaydedilir (kod kontrol eder, yapay zeka değil)
APPROVAL_WORDS = {"onay", "onayla", "onaylıyorum", "onaylandı", "kaydet", "evet kaydet", "tamam kaydet"}

# Bir modelin yanıtı için en fazla bekleme süresi (saniye); aşılırsa sıradaki modele geçilir
AI_TIMEOUT = 180

# Sohbet geçmişinde tutulacak en fazla mesaj sayısı (kullanıcı + bot)
MAX_HISTORY = 20

# Kural dosyası (GitHub'da kodla aynı klasörde)
PROMPT_FILE = "prompt.md"

TELEGRAM_MAX_LEN = 4096

# Yapay zeka bu süre içinde cevap vermezse "devam ediyorum" mesajı atılır (saniye)
PROGRESS_INTERVAL = 30
PROGRESS_TEXT = "Çalışmaya devam ediyorum, lütfen bekleyin..."

# --- Gizli anahtarlar (Render > Environment) ---
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")  # opsiyonel

# --- Render'ın kendisinin otomatik verdiği değerler (elle girilmez) ---
PORT = int(os.environ.get("PORT", "10000"))
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL")

DEFAULT_PROMPT = (
    "Sen Momera Yemek Takip yapay zeka asistanısın. "
    "Kullanıcılara Türkçe, kısa ve anlaşılır yanıtlar ver."
)


def load_system_prompt() -> str:
    path = Path(__file__).parent / PROMPT_FILE
    try:
        text = path.read_text(encoding="utf-8").strip()
        logger.info("Kural dosyası yüklendi: %s (%d karakter)", path.name, len(text))
        return text
    except FileNotFoundError:
        logger.warning("Kural dosyası bulunamadı (%s), varsayılan kurallar kullanılıyor.", path)
        return DEFAULT_PROMPT


SYSTEM_PROMPT = load_system_prompt()

def build_clients() -> dict[str, AsyncOpenAI]:
    """Render'da anahtarı olan her sağlayıcı için istemci oluşturur."""
    clients = {}
    for name, cfg in PROVIDERS.items():
        key = os.environ.get(cfg["key_env"])
        if not key:
            continue
        clients[name] = AsyncOpenAI(
            api_key=key,
            base_url=cfg["base_url"],
            timeout=AI_TIMEOUT,
            max_retries=1,
            # EVREN anahtarı ayrıca X-API-Key başlığıyla da beklenir
            default_headers={"X-API-Key": key} if name == "evren" else None,
        )
    return clients


CLIENTS = build_clients()
TEXT_CHAIN = [c for c in TEXT_CHAIN if c[0] in CLIENTS]
VISION_CHAIN = [c for c in VISION_CHAIN if c[0] in CLIENTS]
if not TEXT_CHAIN or not VISION_CHAIN:
    raise RuntimeError(
        "Kullanılabilir yapay zeka anahtarı yok. Render > Environment'a "
        "GROQ_API_KEY veya EVREN_API_KEY ekleyin."
    )
logger.info("Metin modelleri: %s", [f"{p}/{m}" for p, m, _ in TEXT_CHAIN])
logger.info("Resim modelleri: %s", [f"{p}/{m}" for p, m, _ in VISION_CHAIN])


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


# --- Sohbet geçmişi ---
# Onay akışı (tabloyu göster -> ID ile düzeltme -> onay) için bot önceki
# mesajları hatırlamalı. Geçmiş her sohbet için bellekte tutulur;
# Render yeniden başlarsa sıfırlanır.
def get_history(context: ContextTypes.DEFAULT_TYPE) -> list[dict]:
    return context.chat_data.setdefault("history", [])


def add_to_history(context: ContextTypes.DEFAULT_TYPE, role: str, content: str):
    history = get_history(context)
    history.append({"role": role, "content": content})
    del history[:-MAX_HISTORY]


class IncompleteReply(Exception):
    """Model yanıtı token sınırına takılıp yarım kaldı."""


async def ask_llm(provider: str, model: str, max_tokens: int,
                  history: list[dict], user_content) -> str:
    """Kurallar + geçmiş + yeni mesajı yapay zekaya gönderir."""
    response = await CLIENTS[provider].chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            *history,
            {"role": "user", "content": user_content},
        ],
    )
    choice = response.choices[0]
    if choice.finish_reason == "length":
        raise IncompleteReply(f"yanıt {max_tokens} token sınırında yarım kaldı")
    if not choice.message.content:
        raise IncompleteReply("boş yanıt")
    return choice.message.content


async def ask_chain(chain: list[tuple], history: list[dict], user_content) -> str:
    """Modelleri sırayla dener; ilk tam yanıtı döndürür."""
    last_error = None
    for provider, model, max_tokens in chain:
        try:
            reply = await ask_llm(provider, model, max_tokens, history, user_content)
            logger.info("Yanıt alındı: %s/%s", provider, model)
            return reply
        except Exception as e:
            logger.warning("%s/%s başarısız: %s — sıradakine geçiliyor", provider, model, e)
            last_error = e
    raise last_error


@asynccontextmanager
async def keep_alive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """İşlem sürerken 'yazıyor...' gösterir; her PROGRESS_INTERVAL saniyede
    bir 'çalışmaya devam ediyorum' mesajı atar. İşlem bitince durur."""
    chat_id = update.effective_chat.id

    async def typing_loop():
        # Telegram 'yazıyor...' göstergesi ~5 sn sürer, sürekli yenilenir
        while True:
            try:
                await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            except Exception:
                pass
            await asyncio.sleep(4)

    async def progress_loop():
        while True:
            await asyncio.sleep(PROGRESS_INTERVAL)
            try:
                await update.message.reply_text(PROGRESS_TEXT)
            except Exception:
                logger.warning("İlerleme mesajı gönderilemedi")

    tasks = [asyncio.create_task(typing_loop()), asyncio.create_task(progress_loop())]
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def send_reply(update: Update, text: str):
    for part in split_text(text):
        await update.message.reply_text(part)


def today_str() -> str:
    return datetime.now(TIMEZONE).strftime("%d.%m.%Y")


def message_header(update: Update) -> str:
    """Mesaj tarihini yapay zekaya iletir (kayıt tarihi için gerekli)."""
    msg = update.message
    tarih = msg.date.astimezone(TIMEZONE).strftime("%d.%m.%Y %H:%M")
    gonderen = msg.from_user.full_name if msg.from_user else "Bilinmiyor"
    return f"[Mesaj tarihi: {tarih} | Bugünün tarihi: {today_str()} | Gönderen: {gonderen}]"


# Önizleme tablosunun biçimi. Kod onay gelince bu biçimi okuyup Sheets'e yazar,
# bu yüzden yapay zeka her tabloyu tam olarak böyle göstermelidir.
TABLE_FORMAT = """Tabloyu tam olarak şu biçimde göster (her satır ayrı, sütunlar " | " ile ayrılmış):

📋 LISTBOT tablosuna kaydedilecek liste
📅 Kayıt tarihi: GG.AA.YYYY

ID | Yemek Adı | Adet | NOTE
01 | <yemek adı> | <adet> | <hesap / not>
02 | ...

Toplam: <satır sayısı> satır
Düzeltme isteğiniz var mı? ID numarasıyla yazabilirsiniz (örn. "05'in adedi 11 olsun", "tarih 09.11.2026 olsun"). Liste doğruysa "onay" yazın; liste LISTBOT tablosuna yukarıdaki tarihle kaydedilecek.

Kayıt tarihi kuralı: Kullanıcı başka bir tarih söylemedikçe veya düzeltmedikçe bugünün tarihini kullan."""

PHOTO_INSTRUCTION = """Kullanıcı yemek listesi resmi gönderdi. Kurallardaki 'Resim gönderildiğinde yapılacaklar' ve 'Kayıt öncesi onay' adımlarını uygula.

ÖNEMLİ:
- Şu an SADECE önizleme hazırlıyorsun. Kayıt, kullanıcı "onay" yazınca sistem tarafından yapılır.
- "kaydedildi", "işlendi", "yüklendi", "onaylandı" gibi ifadeler KULLANMA.
- Resimdeki her yemek satırını tek tek yaz; özet verme, satır atlama.

RESMİ OKUMA KURALLARI:
- Form yukarıdan aşağıya numaralı satırlardan oluşur (1, 2, 3 … 45 gibi). Formun TAMAMINI en üst satırdan en alt satıra kadar oku; sadece bir bölümünü değil.
- ID olarak satırın solundaki basılı satır numarasını kullan (örn. 01, 02, 17, 34).
- Basılı satır numarasının sağında sayı (adet), onun sağında yemek adı bulunur.
- Yemek adı ve adedi boş olan satırları ATLA, tabloya yazma.
- Sol kenardaki dikey yazılar (örn. "ETLİ YEMEKLER", "ÇORBA", "TATLI-MEZE", "PİLAV-MAKARNA") bölüm başlığıdır, yemek DEĞİLDİR; tabloya yazma.
- Sayfadaki el yazısı satırlar (yemek adı + sayı) da birer yemek satırıdır; bunları da ekle ve NOTE'a "el yazısı" yaz.
- Yazının yanındaki el yazısı düzeltmeleri (örn. "+5 fazla gönderildi", "-10 eksik") NOTE'a yaz ve adedi kurallara göre hesapla.
- Okuyamadığın kelimeyi uydurma; başına "?" koy ve NOTE'a "okunamadı" yaz.
- Yanıtı bitirmeden önce resimdeki dolu satır sayısıyla tablodaki satır sayısını karşılaştır; eksik varsa ekle.

""" + TABLE_FORMAT

EDIT_INSTRUCTION = """[Sistem notu: Onay bekleyen bir liste var. Kullanıcı düzeltme istediyse uygula ve güncel tablonun TAMAMINI aşağıdaki biçimde tekrar göster. Kayıt yapıldığını ya da liste onaylandığını söyleme.]

""" + TABLE_FORMAT


SAVED_EDIT_INSTRUCTION = """[Sistem görevi] Kullanıcının son mesajı, LISTBOT tablosuna DAHA ÖNCE KAYDEDİLMİŞ satırlarda değişiklik isteği mi?
Kayıtlı satırların NO değeri "{prefix}-ID" biçimindedir (ID 2 basamaklı, örn. 17 -> {prefix}-17). Kullanıcı tam NO yazarsa (örn. 261109-05) onu kullan.
Değiştirilebilir alanlar: "adet", "yemek adı", "note", "tarih". Silme için "action": "sil".

SADECE JSON döndür, başka hiçbir şey yazma:
{{"edits": [{{"no": "{prefix}-17", "field": "adet", "value": "12"}}, {{"no": "{prefix}-05", "action": "sil"}}]}}
Mesaj bir değişiklik isteği değilse: {{"edits": []}}"""


def extract_json(text: str):
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


async def try_saved_edit(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    """Kayıtlı satır düzeltmesi ise tabloda uygular ve True döner."""
    if not re.search(r"\d", text):
        return False
    prefix = context.chat_data.get("saved_prefix") or date_prefix(today_str())
    instruction = SAVED_EDIT_INSTRUCTION.format(prefix=prefix)
    try:
        reply = await ask_chain(TEXT_CHAIN, [], f"Kullanıcı mesajı: {text}\n\n{instruction}")
    except Exception:
        logger.exception("Düzeltme isteği çözümlenemedi")
        return False
    data = extract_json(reply) or {}
    edits = data.get("edits") or []
    if not edits:
        return False
    try:
        async with keep_alive(update, context):
            results = await asyncio.to_thread(update_listbot, edits)
    except Exception as e:
        logger.exception("LISTBOT güncellemesi başarısız")
        await update.message.reply_text(f"❌ Tablo güncellenemedi, değişiklik yapılmadı.\nHata: {e}")
        return True
    msg = "LISTBOT tablosu güncellendi:\n" + "\n".join(results)
    add_to_history(context, "user", text)
    add_to_history(context, "assistant", f"[Sistem] {msg}")
    await send_reply(update, msg)
    return True


# --- Tabloyu metinden okuma ---
ROW_RE = re.compile(r"^\s*\**\s*(\d{1,3})\s*\**\s*\|\s*(.*?)\s*\|\s*(.*?)\s*(?:\|\s*(.*?)\s*)?\|?\s*$")
DATE_RE = re.compile(r"Kayıt tarihi:\s*\**\s*(\d{1,2}[./]\d{1,2}[./]\d{4})", re.IGNORECASE)


def parse_table(text: str):
    """Önizleme mesajından (tarih, satırlar) çıkarır. Tablo yoksa None döner."""
    rows = []
    for line in text.splitlines():
        m = ROW_RE.match(line)
        if not m:
            continue
        _id, name, qty, note = m.groups()
        name = name.strip("* ")
        if not name or name.lower().startswith("yemek adı"):
            continue
        qty = qty.strip("* ")
        qty_digits = re.sub(r"[^\d]", "", qty)
        rows.append({
            "id": _id.zfill(2),
            "name": name,
            "qty": int(qty_digits) if qty_digits else "",
            "note": (note or "").strip("* "),
        })
    if not rows:
        return None
    m = DATE_RE.search(text)
    date = m.group(1).replace("/", ".") if m else today_str()
    return date, rows


# --- Google Sheets ---
HEADER_ALIASES = {
    "no": "no", "no.": "no", "numara": "no",
    "tarih": "date", "date": "date",
    "yemek adı": "name", "yemek adi": "name", "yemek": "name",
    "adet": "qty", "sayı": "qty", "sayi": "qty", "miktar": "qty",
    "note": "note", "not": "note", "notlar": "note", "açıklama": "note",
}
_gc = None


def sheets_client():
    global _gc
    if _gc is None:
        import gspread  # requirements.txt: gspread
        creds = os.environ.get("GOOGLE_CREDENTIALS_JSON")
        if not creds:
            raise RuntimeError("Render > Environment'ta GOOGLE_CREDENTIALS_JSON yok.")
        _gc = gspread.service_account_from_dict(json.loads(creds))
    return _gc


def date_prefix(date: str) -> str:
    """10.11.2026 -> 261110 (yymmdd)"""
    d, m, y = date.split(".")
    return f"{y[-2:]}{m.zfill(2)}{d.zfill(2)}"


def open_tab(name: str):
    if not SPREADSHEET_ID:
        raise RuntimeError("Kodda SPREADSHEET_ID boş.")
    sh = sheets_client().open_by_key(SPREADSHEET_ID)
    # Sekme adı büyük/küçük harf ve boşluk farkı gözetmeden bulunur (ListBot = LISTBOT)
    target = name.replace(" ", "").lower()
    for ws in sh.worksheets():
        if ws.title.replace(" ", "").lower() == target:
            return ws
    names = ", ".join(w.title for w in sh.worksheets())
    raise RuntimeError(f"'{name}' sekmesi bulunamadı. Mevcut sekmeler: {names}")


def open_listbot():
    return open_tab(LISTBOT_SHEET)


# --- Yemek adı eşleştirme (EslesmeMatrisi) ---
def fold(text: str) -> str:
    """Karşılaştırma için sadeleştirir: Türkçe karakter, büyük/küçük harf,
    boşluk ve noktalama farklarını yok sayar. 'PİRİNÇ PİLAV' -> 'pirincpilav'"""
    text = text.replace("İ", "i").replace("I", "i").lower()
    text = text.translate(str.maketrans("çğıöşü", "cgiosu"))
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", text)


WORD_INDEX: dict[tuple, str] = {}


def load_matrix():
    """EslesmeMatrisi'ni okur. Döndürür: (resmi adlar listesi, {sade ad: resmi ad}).
    B kolonundaki '/' ile ayrılmış her alternatif ayrı eşleşme sayılır."""
    rows = open_tab(MATRIX_SHEET).get_all_values()[1:]  # 1. satır başlık
    official, lookup = [], {}
    WORD_INDEX.clear()
    for r in rows:
        a = (r[0] if len(r) > 0 else "").strip()
        b = (r[1] if len(r) > 1 else "").strip()
        if not a:
            continue
        official.append(a)
        for alt in [a] + b.split("/"):
            if fold(alt):
                lookup.setdefault(fold(alt), a)
                WORD_INDEX.setdefault(tuple(sorted(fold(w) for w in alt.split() if fold(w))), a)
    return official, lookup


def match_name(read: str, lookup: dict) -> tuple[str, str]:
    """Okunan adı resmi ada çevirir. Döndürür: (ad, not).
    Bulunamazsa ad '?' ile başlar."""
    raw = read.lstrip("?").strip()
    key = fold(raw)
    if not key:
        return read, ""
    # 1) Birebir eşleşme (A ya da B kolonu)
    if key in lookup:
        return lookup[key], ""
    # 2) Aynı kelimeler farklı sırada (örn. "NOHUT ETLİ" = "Etli Nohut")
    words = sorted(fold(w) for w in raw.split() if fold(w))
    for alt, official in WORD_INDEX.items():
        if words == list(alt):
            return official, f"eşleşme: okunan '{raw}'"
    # 3) Biri diğerini içeriyorsa ve tek bir resmi ada çıkıyorsa (örn. SCHNITZEL)
    if len(key) >= 5:
        hits = {v for k, v in lookup.items() if key in k or (len(k) >= 5 and k in key)}
        if len(hits) == 1:
            return hits.pop(), f"eşleşme: okunan '{raw}'"
    # 4) Yazım hatasına dayanıklı yaklaşık eşleşme
    best = difflib.get_close_matches(key, list(lookup), n=1, cutoff=FUZZY_THRESHOLD)
    if best:
        return lookup[best[0]], f"yaklaşık eşleşme: okunan '{raw}'"
    return f"?{raw}", "eşleşme bulunamadı"


FIELD_TO_KEY = {
    "adet": "qty", "yemek adı": "name", "yemek adi": "name", "yemek": "name",
    "note": "note", "not": "note", "tarih": "date",
}


def update_listbot(edits: list[dict]) -> list[str]:
    """Kayıtlı satırları NO değerine göre bulup günceller veya siler.
    Her düzenleme için sonucu anlatan bir satır döndürür."""
    ws = open_listbot()
    header = ws.row_values(1)
    keys = [HEADER_ALIASES.get(h.strip().lower()) for h in header]
    if "no" not in keys:
        raise RuntimeError("LISTBOT'ta NO kolonu bulunamadı.")
    no_col = keys.index("no") + 1
    results, deletes = [], []
    no_values = ws.col_values(no_col)
    for e in edits:
        no = str(e.get("no", "")).strip()
        try:
            row = no_values.index(no) + 1
        except ValueError:
            results.append(f"⚠️ {no}: LISTBOT'ta bulunamadı, değişiklik yapılmadı.")
            continue
        if e.get("action") == "sil":
            deletes.append((row, no))
            continue
        key = FIELD_TO_KEY.get(str(e.get("field", "")).strip().lower())
        if key not in keys:
            results.append(f"⚠️ {no}: '{e.get('field')}' alanı tanınmadı.")
            continue
        col = keys.index(key) + 1
        old = ws.cell(row, col).value or ""
        new = e.get("value", "")
        ws.update_cell(row, col, new)
        results.append(f"✏️ {no}: {header[col - 1]} '{old}' → '{new}'")
    # Silmeleri alttan üste yap ki satır numaraları kaymasın
    for row, no in sorted(deletes, reverse=True):
        ws.delete_rows(row)
        results.append(f"🗑️ {no}: satır silindi")
    return results


def append_to_listbot(date: str, rows: list[dict]) -> int:
    """Satırları LISTBOT sekmesine ekler. Sütunları başlık satırına göre eşleştirir."""
    ws = open_listbot()
    header = ws.row_values(1)
    if not header:
        header = ["NO", "Tarih", "Yemek Adı", "Adet", "NOTE"]
        ws.append_row(header)
    keys = [HEADER_ALIASES.get(h.strip().lower()) for h in header]
    values = []
    prefix = date_prefix(date)
    for r in rows:
        data = {
            "no": f"{prefix}-{r['id']}",  # örn. 261110-05
            "date": date, "name": r["name"], "qty": r["qty"], "note": r["note"],
        }
        values.append([data.get(k, "") if k else "" for k in keys])
    ws.append_rows(values, value_input_option="USER_ENTERED")
    return len(values)


# --- Komutlar ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/start komutu verildiğinde çalışır."""
    await update.message.reply_text(
        "Merhaba! Ben Momera Yemek Takip yapay zeka asistanınızım. "
        "Yemek listesinin fotoğrafını gönderebilir ya da soru sorabilirsiniz."
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/yeni komutu: sohbet geçmişini ve bekleyen listeyi temizler."""
    context.chat_data["history"] = []
    context.chat_data.pop("pending", None)
    await update.message.reply_text("Sohbet geçmişi temizlendi. Yeni işleme başlayabilirsiniz.")


TABLE_FOOTER = (
    'Düzeltme isteğiniz var mı? ID numarasıyla yazabilirsiniz (örn. "05\'in adedi 11 olsun", '
    '"tarih 09.11.2026 olsun"). Liste doğruysa "onay" yazın; liste LISTBOT tablosuna '
    "yukarıdaki tarihle kaydedilecek."
)


def render_table(date: str, rows: list[dict], warning: str = "") -> str:
    lines = [
        "📋 LISTBOT tablosuna kaydedilecek liste",
        f"📅 Kayıt tarihi: {date}",
        "",
        "ID | Yemek Adı | Adet | NOTE",
    ]
    for r in rows:
        lines.append(f"{r['id']} | {r['name']} | {r['qty']} | {r['note']}".rstrip())
    unmatched = sum(1 for r in rows if r["name"].startswith("?"))
    lines += ["", f"Toplam: {len(rows)} satır"]
    if unmatched:
        lines.append(
            f"❓ {unmatched} yemek EslesmeMatrisi'nde bulunamadı (başında ? olanlar). "
            "Matrise ekleyip \"tekrar eşleştir\" yazabilir ya da ID ile adını düzeltebilirsiniz."
        )
    if warning:
        lines.append(warning)
    lines += ["", TABLE_FOOTER]
    return "\n".join(lines)


def apply_matching(rows: list[dict]) -> str:
    """Satırlardaki adları EslesmeMatrisi'ne göre düzeltir. Uyarı metni döndürür."""
    try:
        _, lookup = load_matrix()
    except Exception as e:
        logger.exception("EslesmeMatrisi okunamadı")
        return f"⚠️ EslesmeMatrisi okunamadı, adlar eşleştirilmedi ({e})."
    for r in rows:
        name, note = match_name(r["name"], lookup)
        r["name"] = name
        if note and note not in r["note"]:
            r["note"] = f"{r['note']}; {note}" if r["note"] else note
    return ""


async def process_table(context: ContextTypes.DEFAULT_TYPE, reply_text: str) -> str:
    """Yanıtta tablo varsa: adları eşleştirir, bekleyen liste olarak saklar ve
    kullanıcıya gösterilecek standart tabloyu döndürür. Tablo yoksa metni aynen döndürür."""
    parsed = parse_table(reply_text)
    if not parsed:
        return reply_text
    date, rows = parsed
    warning = await asyncio.to_thread(apply_matching, rows)
    context.chat_data["pending"] = (date, rows)
    return render_table(date, rows, warning)


# --- Onay ve kayıt (yapay zeka değil, kod yapar) ---
async def save_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending = context.chat_data.get("pending")
    if not pending:
        await update.message.reply_text(
            "Onay bekleyen bir liste yok. Önce yemek listesinin fotoğrafını gönderin."
        )
        return
    date, rows = pending
    await update.message.reply_text(
        f"Onay alındı. {len(rows)} satır {date} tarihiyle LISTBOT tablosuna kaydediliyor..."
    )
    try:
        count = await asyncio.to_thread(append_to_listbot, date, rows)
    except Exception as e:
        logger.exception("LISTBOT kaydı başarısız")
        await update.message.reply_text(
            f"❌ Kayıt yapılamadı, LISTBOT tablosuna hiçbir şey eklenmedi.\nHata: {e}\n"
            "Liste hâlâ onay bekliyor; sorun giderilince tekrar \"onay\" yazabilirsiniz."
        )
        return
    context.chat_data.pop("pending", None)
    context.chat_data["saved_prefix"] = date_prefix(date)
    msg = (
        f"✅ Kayıt tamamlandı: {count} satır {date} tarihiyle LISTBOT tablosuna eklendi.\n"
        f"Satırlar NO kolonunda {date_prefix(date)}-ID olarak kayıtlı. Düzeltme gerekirse "
        "yazmanız yeterli (örn. \"17'nin adedi 12 olsun\"), doğrudan tabloda güncellerim."
    )
    add_to_history(context, "assistant", f"[Sistem] {msg}")
    await update.message.reply_text(msg)


# --- Metin mesajları ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Kullanıcıdan gelen metni geçmişle birlikte yapay zekaya iletir."""
    text = update.message.text.strip()
    if text.lower().rstrip(".!") in APPROVAL_WORDS:
        await save_pending(update, context)
        return

    # Matris güncellendiyse bekleyen listeyi yeniden eşleştir
    if fold(text) in {"tekrareslestir", "yenideneslestir", "eslestir"} and context.chat_data.get("pending"):
        date, rows = context.chat_data["pending"]
        warning = await asyncio.to_thread(apply_matching, rows)
        table = render_table(date, rows, warning)
        add_to_history(context, "assistant", table)
        await send_reply(update, table)
        return

    # Bekleyen liste yoksa: kayıtlı satırlarda düzeltme isteği olabilir
    if not context.chat_data.get("pending") and await try_saved_edit(update, context, text):
        return

    user_text = f"{message_header(update)}\n{text}"
    model_text = user_text
    if context.chat_data.get("pending"):
        model_text = f"{user_text}\n\n{EDIT_INSTRUCTION}"

    try:
        async with keep_alive(update, context):
            reply_text = await ask_chain(TEXT_CHAIN, get_history(context), model_text)
    except Exception:
        logger.exception("Hiçbir metin modeli yanıt veremedi")
        await update.message.reply_text("Üzgünüm, bir hata oluştu. Lütfen tekrar deneyin.")
        return

    reply_text = await process_table(context, reply_text)
    add_to_history(context, "user", user_text)
    add_to_history(context, "assistant", reply_text)
    await send_reply(update, reply_text)


# --- Resim mesajları ---
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Resim gelince: 1) hemen 'başlıyorum' der, 2) resmi okutup tabloyu gönderir.
    İndirme, matris okuma ve yapay zeka adımlarının hepsi 'devam ediyorum'
    mesajlarının kapsamındadır; herhangi bir adım hata verirse kullanıcıya bildirilir."""
    msg = update.message

    # 1. cevap: hemen gönderilir
    await msg.reply_text("Tamam, resmi aldım, işlemlere başlıyorum.")

    caption = msg.caption or ""
    user_text = f"{message_header(update)}\nKullanıcının notu: {caption or '(yok)'}"
    step = "resim indirilirken"
    try:
        async with keep_alive(update, context):
            # Resmi indir: normal fotoğraf ya da "dosya olarak" gönderilmiş resim
            if msg.photo:
                tg_file = await msg.photo[-1].get_file()  # en yüksek çözünürlük
                mime = "image/jpeg"
            else:
                tg_file = await msg.document.get_file()
                mime = msg.document.mime_type or "image/jpeg"
            img_bytes = await tg_file.download_as_bytearray()
            img_b64 = base64.b64encode(img_bytes).decode()
            logger.info("Resim indirildi (%d KB)", len(img_bytes) // 1024)

            step = "yemek listesi (EslesmeMatrisi) okunurken"
            vocab = ""
            try:
                official, _ = await asyncio.wait_for(asyncio.to_thread(load_matrix), timeout=30)
                vocab = (
                    "\n\nBilinen yemek adları (okurken yardımcı olsun diye; resimde ne yazıyorsa "
                    "onu yaz, eşleştirmeyi sistem yapar): " + ", ".join(official)
                )
            except Exception as e:
                logger.warning("EslesmeMatrisi okunamadı, ipucu listesi olmadan devam: %s", e)

            step = "resim yapay zekaya okutulurken"
            user_content = [
                {"type": "text", "text": f"{user_text}\n\n{PHOTO_INSTRUCTION}{vocab}"},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{img_b64}"}},
            ]
            reply_text = await ask_chain(VISION_CHAIN, get_history(context), user_content)

            step = "yemek adları eşleştirilirken"
            context.chat_data.pop("pending", None)  # yeni resim önceki listeyi değiştirir
            reply_text = await process_table(context, reply_text)
    except Exception as e:
        logger.exception("Resim işlenemedi (%s)", step)
        await msg.reply_text(
            f"Üzgünüm, {step} bir hata oluştu, lütfen tekrar deneyin.\nHata: {str(e)[:300]}"
        )
        return

    # Geçmişe resmin kendisi değil, sadece metni yazılır (boyut büyümesin diye)
    add_to_history(context, "user", f"{user_text}\n[Resim gönderildi]")
    add_to_history(context, "assistant", reply_text)

    # 2. cevap: onaya sunulan tablo
    await send_reply(update, reply_text)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Yakalanmamış her hatayı loglar ve kullanıcıya sessiz kalmak yerine bildirir."""
    logger.exception("Beklenmeyen hata", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                f"Üzgünüm, beklenmeyen bir hata oluştu: {str(context.error)[:300]}"
            )
        except Exception:
            pass


def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_error_handler(on_error)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("yeni", reset))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

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
