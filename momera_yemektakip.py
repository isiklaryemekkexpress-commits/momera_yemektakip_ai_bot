import asyncio
import base64
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
VISION_CHAIN = [
    ("groq", "qwen/qwen3.8-27b", 900),
    ("evren", "gemma-4-31b", 4096),      # el yazısı çözümleme
    ("evren", "qwen3-vl-30b", 4096),     # tablo / belge okuma
    ("evren", "deepseek-v4.1-flash", 4096),
]

# --- Google Sheets ---
# Tablonun adresindeki ID: docs.google.com/spreadsheets/d/<BURASI>/edit
SPREADSHEET_ID = ""  # TODO: Google Sheet ID'sini buraya yazın
LISTBOT_SHEET = "LISTBOT"
TIMEZONE = ZoneInfo("Europe/Istanbul")

# Kullanıcı bunlardan birini yazarsa liste kaydedilir (kod kontrol eder, yapay zeka değil)
APPROVAL_WORDS = {"onay", "onayla", "onaylıyorum", "onaylandı", "kaydet", "evet kaydet", "tamam kaydet"}

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
- Resimdeki her yemek satırını tek tek yaz; özet verme.

""" + TABLE_FORMAT

EDIT_INSTRUCTION = """[Sistem notu: Onay bekleyen bir liste var. Kullanıcı düzeltme istediyse uygula ve güncel tablonun TAMAMINI aşağıdaki biçimde tekrar göster. Kayıt yapıldığını ya da liste onaylandığını söyleme.]

""" + TABLE_FORMAT


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


def append_to_listbot(date: str, rows: list[dict]) -> int:
    """Satırları LISTBOT sekmesine ekler. Sütunları başlık satırına göre eşleştirir."""
    if not SPREADSHEET_ID:
        raise RuntimeError("Kodda SPREADSHEET_ID boş.")
    ws = sheets_client().open_by_key(SPREADSHEET_ID).worksheet(LISTBOT_SHEET)
    header = ws.row_values(1)
    if not header:
        header = ["Tarih", "Yemek Adı", "Adet", "NOTE"]
        ws.append_row(header)
    keys = [HEADER_ALIASES.get(h.strip().lower()) for h in header]
    values = []
    for r in rows:
        data = {"date": date, "name": r["name"], "qty": r["qty"], "note": r["note"]}
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


def remember_table(context: ContextTypes.DEFAULT_TYPE, reply_text: str):
    """Yanıtta tablo varsa onay bekleyen liste olarak saklar."""
    parsed = parse_table(reply_text)
    if parsed:
        context.chat_data["pending"] = parsed


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
    msg = f"✅ Kayıt tamamlandı: {count} satır {date} tarihiyle LISTBOT tablosuna eklendi."
    add_to_history(context, "assistant", f"[Sistem] {msg}")
    await update.message.reply_text(msg)


# --- Metin mesajları ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Kullanıcıdan gelen metni geçmişle birlikte yapay zekaya iletir."""
    text = update.message.text.strip()
    if text.lower().rstrip(".!") in APPROVAL_WORDS:
        await save_pending(update, context)
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

    remember_table(context, reply_text)
    add_to_history(context, "user", user_text)
    add_to_history(context, "assistant", reply_text)
    await send_reply(update, reply_text)


# --- Resim mesajları ---
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Resim gelince: 1) hemen 'başlıyorum' der, 2) resmi okutup tabloyu gönderir."""
    msg = update.message

    # 1. cevap: hemen gönderilir
    await msg.reply_text("Tamam, resmi aldım, işlemlere başlıyorum.")

    # Resmi indir: normal fotoğraf ya da "dosya olarak" gönderilmiş resim
    if msg.photo:
        tg_file = await msg.photo[-1].get_file()  # en yüksek çözünürlük
        mime = "image/jpeg"
    else:
        tg_file = await msg.document.get_file()
        mime = msg.document.mime_type or "image/jpeg"

    img_bytes = await tg_file.download_as_bytearray()
    img_b64 = base64.b64encode(img_bytes).decode()

    caption = msg.caption or ""
    user_text = f"{message_header(update)}\nKullanıcının notu: {caption or '(yok)'}"
    user_content = [
        {"type": "text", "text": f"{user_text}\n\n{PHOTO_INSTRUCTION}"},
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{img_b64}"}},
    ]

    try:
        async with keep_alive(update, context):
            reply_text = await ask_chain(VISION_CHAIN, get_history(context), user_content)
    except Exception:
        logger.exception("Hiçbir resim modeli yanıt veremedi")
        await msg.reply_text(
            "Üzgünüm, resmi işlerken bir hata oluştu. Lütfen tekrar deneyin."
        )
        return

    # Yeni resim önceki bekleyen listeyi değiştirir
    context.chat_data.pop("pending", None)
    remember_table(context, reply_text)

    # Geçmişe resmin kendisi değil, sadece metni yazılır (boyut büyümesin diye)
    add_to_history(context, "user", f"{user_text}\n[Resim gönderildi]")
    add_to_history(context, "assistant", reply_text)

    # 2. cevap: onaya sunulan tablo
    await send_reply(update, reply_text)


def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

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
