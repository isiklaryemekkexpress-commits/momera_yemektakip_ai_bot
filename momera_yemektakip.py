import asyncio
import base64
import json
import logging
import os
import re
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
SHEET_ID = os.environ.get("SHEET_ID")  # opsiyonel; verilirse ad aranmaz
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
    "Kullanıcılara Türkçe, kısa ve anlaşılır yanıtlar ver."
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
    + "\n\nGörevin: Kullanıcının gönderdiği fotoğraftaki bilgileri tabloya eklenecek satırlara çevirmek. "
    "Yalnızca şu biçimde geçerli bir JSON nesnesi döndür: "
    '{"ozet": "fotoğrafın bir cümlelik özeti", "satirlar": [{"Sütun adı": "değer"}]}. '
    "Sütun adlarını fotoğraftaki başlıklardan veya içerikten türet, Türkçe ve kısa tut. "
    "Fotoğrafta birden fazla kalem varsa her biri ayrı satır olsun. "
    "Okuyamadığın veya emin olmadığın değeri boş bırak, asla tahmin etme ya da uydurma. "
    "Kullanıcı açıklama yazdıysa ona göre davran."
)


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
    """(dosya_id, ilk_sayfa_adı) döndürür."""
    if _sheet_cache:
        return _sheet_cache["id"], _sheet_cache["title"]
    s = _session_get()
    sid = SHEET_ID
    if not sid:
        q = (
            f"name = '{SHEET_NAME}' and trashed = false and "
            "mimeType = 'application/vnd.google-apps.spreadsheet'"
        )
        r = s.get(
            "https://www.googleapis.com/drive/v3/files",
            params={"q": q, "fields": "files(id,name)"},
        )
        r.raise_for_status()
        files = r.json().get("files", [])
        if not files:
            raise RuntimeError(
                f"'{SHEET_NAME}' tablosu bulunamadı. Tablo service account ile paylaşıldı mı?"
            )
        sid = files[0]["id"]
    r = s.get(
        f"https://sheets.googleapis.com/v4/spreadsheets/{sid}",
        params={"fields": "sheets.properties.title"},
    )
    r.raise_for_status()
    title = r.json()["sheets"][0]["properties"]["title"]
    _sheet_cache.update({"id": sid, "title": title})
    return sid, title


def _cell(value):
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return value


def append_rows_sync(rows: list[dict]) -> int:
    """Satırları tabloya ekler; gerekirse başlık satırını ve yeni sütunları oluşturur."""
    s = _session_get()
    sid, title = _sheet_meta()
    base = f"https://sheets.googleapis.com/v4/spreadsheets/{sid}/values"

    zaman = datetime.now(TIMEZONE).strftime("%d.%m.%Y %H:%M")
    rows = [{"Eklenme Zamanı": zaman, **row} for row in rows]

    r = s.get(f"{base}/{quote(f"'{title}'!A1:ZZ1")}")
    r.raise_for_status()
    header = (r.json().get("values") or [[]])[0]

    degisti = False
    for row in rows:
        for key in row:
            if key not in header:
                header.append(key)
                degisti = True
    if degisti:
        r = s.put(
            f"{base}/{quote(f"'{title}'!A1")}",
            params={"valueInputOption": "RAW"},
            json={"values": [header]},
        )
        r.raise_for_status()

    values = [[_cell(row.get(h)) for h in header] for row in rows]
    r = s.post(
        f"{base}/{quote(f"'{title}'!A1")}:append",
        params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
        json={"values": values},
    )
    r.raise_for_status()
    return len(values)


async def append_rows(rows: list[dict]) -> int:
    return await asyncio.to_thread(append_rows_sync, rows)


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


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Yazılı mesajı Groq'a iletir ve yanıtı döner."""
    if not await izinli_mi(update):
        return
    user_text = update.message.text
    history = context.user_data.setdefault("gecmis", [])

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action=ChatAction.TYPING
    )
    messages = (
        [{"role": "system", "content": SYSTEM_PROMPT}]
        + history
        + [{"role": "user", "content": user_text}]
    )
    try:
        reply_text = await groq_chat(messages, GROQ_MODEL) or "Bir yanıt üretilemedi."
    except Exception:
        logger.exception("Groq çağrısında hata oluştu")
        await update.message.reply_text("Üzgünüm, bir hata oluştu. Lütfen tekrar deneyin.")
        return

    history += [
        {"role": "user", "content": user_text},
        {"role": "assistant", "content": reply_text},
    ]
    del history[:-HISTORY_LIMIT]

    for part in split_text(reply_text):
        await update.message.reply_text(part)


def _satir_onizleme(rows: list[dict]) -> str:
    lines = []
    for i, row in enumerate(rows, 1):
        alanlar = "; ".join(f"{k}: {v}" for k, v in row.items() if v not in (None, ""))
        lines.append(f"{i}. {alanlar or '(boş satır)'}")
    return "\n".join(lines)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fotoğrafı Groq ile okutur, sonucu gösterip onay ister."""
    if not await izinli_mi(update):
        return
    msg = update.message
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

    context.user_data["bekleyen"] = rows
    buttons = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Evet, ekle", callback_data="ekle"),
            InlineKeyboardButton("Hayır", callback_data="iptal"),
        ]]
    )
    text = (
        f"{result.get('ozet', 'Fotoğrafı okudum.')}\n\n"
        f"{_satir_onizleme(rows)}\n\nBu satırları tabloya ekleyeyim mi?"
    )
    await msg.reply_text(text[:TELEGRAM_MAX_LEN], reply_markup=buttons)


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Onay düğmelerini işler."""
    query = update.callback_query
    if ALLOWED_USER_IDS and query.from_user.id not in ALLOWED_USER_IDS:
        await query.answer("Yetkiniz yok.", show_alert=True)
        return
    await query.answer()
    rows = context.user_data.pop("bekleyen", None)
    if query.data == "iptal" or not rows:
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(
            "Tamam, eklemedim." if query.data == "iptal" else "Bekleyen kayıt yok."
        )
        return
    try:
        n = await append_rows(rows)
    except Exception:
        logger.exception("Tabloya yazılırken hata oluştu")
        context.user_data["bekleyen"] = rows  # tekrar denenebilsin
        await query.message.reply_text(
            "Tabloya yazarken hata oluştu. Biraz sonra tekrar 'Evet, ekle' düğmesine basabilirsiniz."
        )
        return
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(f"{n} satır '{SHEET_NAME}' tablosuna eklendi.")


def main():
    missing = [
        n for n in ("TELEGRAM_BOT_TOKEN", "GROQ_API_KEY", "GOOGLE_CREDENTIALS_JSON")
        if not os.environ.get(n)
    ]
    if missing:
        logger.warning("Eksik ortam değişkenleri: %s", ", ".join(missing))
    logger.info("Groq modeli: %s | Resim modeli: %s", GROQ_MODEL, GROQ_VISION_MODEL)

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_button, pattern="^(ekle|iptal)$"))

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
