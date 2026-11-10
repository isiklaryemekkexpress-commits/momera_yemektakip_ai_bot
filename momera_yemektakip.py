import base64
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
#   TELEGRAM_BOT_TOKEN ve seçili sağlayıcının anahtarı
#   (grok -> GROK_API_KEY, evren -> EVREN_API_KEY), WEBHOOK_SECRET opsiyonel
# ============================================================

# Kullanılacak sağlayıcı: "grok" ya da "evren"
PROVIDER = "grok"

PROVIDERS = {
    "grok": {
        "base_url": "https://api.x.ai/v1",
        "key_env": "GROK_API_KEY",
        # Metin mesajları (düzeltme, onay, sorular) için model
        "text_model": "grok-4",
        # Resim okuma modelleri — sırayla denenir; ilki hata verirse sonrakine geçilir
        "vision_models": ["grok-4"],
    },
    "evren": {
        "base_url": "https://evren-llmapi.ssyz.org.tr/v1",
        "key_env": "EVREN_API_KEY",
        "text_model": "deepseek-v4.1-flash",
        "vision_models": ["gemma-4-31b", "qwen3-vl-30b", "deepseek-v4.1-flash"],
    },
}

CFG = PROVIDERS[PROVIDER]
TEXT_MODEL = CFG["text_model"]
VISION_MODELS = CFG["vision_models"]

# Yanıt için token bütçesi (düşünen modellerde düşünme de bu bütçeden harcanır)
MAX_TOKENS = 4096

# Sohbet geçmişinde tutulacak en fazla mesaj sayısı (kullanıcı + bot)
MAX_HISTORY = 20

# Kural dosyası (GitHub'da kodla aynı klasörde)
PROMPT_FILE = "prompt.md"

TELEGRAM_MAX_LEN = 4096

# --- Gizli anahtarlar (Render > Environment) ---
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
API_KEY = os.environ[CFG["key_env"]]
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

client = AsyncOpenAI(
    api_key=API_KEY,
    base_url=CFG["base_url"],
    # EVREN anahtarı ayrıca X-API-Key başlığıyla da beklenir
    default_headers={"X-API-Key": API_KEY} if PROVIDER == "evren" else None,
)
logger.info(
    "Sağlayıcı: %s — metin modeli: %s, resim modelleri: %s",
    PROVIDER, TEXT_MODEL, VISION_MODELS,
)


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


async def ask_llm(model: str, history: list[dict], user_content) -> str:
    """Kurallar + geçmiş + yeni mesajı yapay zekaya gönderir."""
    response = await client.chat.completions.create(
        model=model,
        max_tokens=MAX_TOKENS,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            *history,
            {"role": "user", "content": user_content},
        ],
    )
    return response.choices[0].message.content or "Bir yanıt üretilemedi."


async def ask_vision(history: list[dict], user_content) -> str:
    """Resim modellerini sırayla dener; ilk başarılı yanıtı döndürür."""
    last_error = None
    for model in VISION_MODELS:
        try:
            reply = await ask_llm(model, history, user_content)
            logger.info("Resim okundu (model: %s)", model)
            return reply
        except Exception as e:
            logger.warning("Resim modeli başarısız (%s): %s — sıradakine geçiliyor", model, e)
            last_error = e
    raise last_error


async def send_reply(update: Update, text: str):
    for part in split_text(text):
        await update.message.reply_text(part)


def message_header(update: Update) -> str:
    """Mesaj tarihini yapay zekaya iletir (kayıt tarihi için gerekli)."""
    msg = update.message
    tarih = msg.date.astimezone().strftime("%d.%m.%Y %H:%M")
    gonderen = msg.from_user.full_name if msg.from_user else "Bilinmiyor"
    return f"[Mesaj tarihi: {tarih} | Gönderen: {gonderen}]"


# --- Komutlar ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/start komutu verildiğinde çalışır."""
    await update.message.reply_text(
        "Merhaba! Ben Momera Yemek Takip yapay zeka asistanınızım. "
        "Yemek listesinin fotoğrafını gönderebilir ya da soru sorabilirsiniz."
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/yeni komutu: sohbet geçmişini temizler."""
    context.chat_data["history"] = []
    await update.message.reply_text("Sohbet geçmişi temizlendi. Yeni işleme başlayabilirsiniz.")


# --- Metin mesajları ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Kullanıcıdan gelen metni geçmişle birlikte yapay zekaya iletir."""
    user_text = f"{message_header(update)}\n{update.message.text}"

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action=ChatAction.TYPING
    )

    try:
        reply_text = await ask_llm(TEXT_MODEL, get_history(context), user_text)
    except Exception:
        logger.exception("Yapay zeka çağrısında hata oluştu (model: %s)", TEXT_MODEL)
        await update.message.reply_text("Üzgünüm, bir hata oluştu. Lütfen tekrar deneyin.")
        return

    add_to_history(context, "user", user_text)
    add_to_history(context, "assistant", reply_text)
    await send_reply(update, reply_text)


# --- Resim mesajları ---
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Resim gelince: 1) hemen 'başlıyorum' der, 2) resmi okutup tabloyu gönderir."""
    msg = update.message

    # 1. cevap: hemen gönderilir
    await msg.reply_text("Tamam, resmi aldım, işlemlere başlıyorum.")
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action=ChatAction.TYPING
    )

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
    user_text = (
        f"{message_header(update)}\n"
        "Kullanıcı yemek listesi resmi gönderdi. Kurallardaki "
        "'Resim gönderildiğinde yapılacaklar' sürecini uygula.\n"
        f"Kullanıcının notu: {caption or '(yok)'}"
    )
    user_content = [
        {"type": "text", "text": user_text},
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{img_b64}"}},
    ]

    try:
        reply_text = await ask_vision(get_history(context), user_content)
    except Exception:
        logger.exception("Hiçbir resim modeli yanıt veremedi: %s", VISION_MODELS)
        await msg.reply_text(
            "Üzgünüm, resmi işlerken bir hata oluştu. Lütfen tekrar deneyin."
        )
        return

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
