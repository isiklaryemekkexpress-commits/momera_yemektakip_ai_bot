import logging
import os

from google import genai
from google.genai import types
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

# --- Ayarlar ---
# Önce Render > Environment değişkenlerine bakar; yoksa aşağıdaki tırnakların içine yazdığın değeri kullanır.
# DİKKAT: Anahtarları dosyaya yazıp GitHub'a yüklersen herkese açık olur (özellikle public repo'da).
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or "BURAYA_TELEGRAM_TOKEN"
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY") or "BURAYA_GEMINI_ANAHTARI"
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")  # opsiyonel
PORT = int(os.environ.get("PORT", "10000"))
# Render bu değişkeni otomatik verir (ör. https://momera-yemektakip-ai-bot.onrender.com)
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL")

SYSTEM_PROMPT = (
    "Sen Momera Yemek Takip yapay zeka asistanısın. "
    "Kullanıcılara Türkçe, kısa ve anlaşılır yanıtlar ver."
)

TELEGRAM_MAX_LEN = 4096

# --- Gemini istemcisi ---
# "AQ." ile başlayan anahtarlar Vertex AI üzerinden çalışır, otomatik algılanır.
# Gerekirse Render'da GEMINI_USE_VERTEX=true/false ile elle de belirleyebilirsin.
_vertex_env = os.environ.get("GEMINI_USE_VERTEX", "").lower()
if _vertex_env:
    USE_VERTEX = _vertex_env in ("1", "true", "yes")
else:
    USE_VERTEX = GEMINI_API_KEY.startswith("AQ.")

logger.info("Gemini modu: %s", "Vertex AI" if USE_VERTEX else "Gemini API")
client = genai.Client(vertexai=USE_VERTEX, api_key=GEMINI_API_KEY)


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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/start komutu verildiğinde çalışır."""
    await update.message.reply_text(
        "Merhaba! Ben Momera Yemek Takip yapay zeka asistanınızım. "
        "Size nasıl yardımcı olabilirim?"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Kullanıcıdan gelen mesajı Gemini'ye iletir ve yanıtı döner."""
    user_text = update.message.text

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action=ChatAction.TYPING
    )

    try:
        # Asenkron çağrı: bot, Gemini yanıtını beklerken donmaz
        response = await client.aio.models.generate_content(
            model=GEMINI_MODEL,
            contents=user_text,
            config=types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT),
        )
        reply_text = response.text or "Bir yanıt üretilemedi."
    except Exception:
        logger.exception("Gemini çağrısında hata oluştu")
        await update.message.reply_text("Üzgünüm, bir hata oluştu. Lütfen tekrar deneyin.")
        return

    for part in split_text(reply_text):
        await update.message.reply_text(part)


def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
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
