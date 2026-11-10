import logging
import os

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

# --- Genel ayarlar (Render > Environment sekmesinden gelir, koda yazılmaz) ---
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")  # opsiyonel
PORT = int(os.environ.get("PORT", "10000"))
# Render bu değişkeni otomatik verir (ör. https://momera-yemektakip-ai-bot.onrender.com)
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL")

SYSTEM_PROMPT = (
    "Sen Momera Yemek Takip yapay zeka asistanısın. "
    "Kullanıcılara Türkçe, kısa ve anlaşılır yanıtlar ver."
)

TELEGRAM_MAX_LEN = 4096

# --- Yapay zeka sağlayıcısı seçimi ---
# Render'da LLM_PROVIDER = evren  ya da  grok  yazarak seçersin (varsayılan: evren).
# İkisi de OpenAI uyumlu olduğu için aynı kütüphane ile çalışır.
# LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "grok").strip().lower()
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "evren").strip().lower()


def build_client() -> tuple[AsyncOpenAI, str]:
    """Seçilen sağlayıcıya göre istemciyi ve model adını döndürür."""
    if LLM_PROVIDER == "evren":
        api_key = os.environ["EVREN_API_KEY"]
        client = AsyncOpenAI(
            api_key=api_key,
            base_url=os.environ.get("EVREN_BASE_URL", "https://evren-llmapi.ssyz.org.tr/v1"),
            # EVREN anahtarı X-API-Key başlığıyla beklenir
            default_headers={"X-API-Key": api_key},
        )
        # "auto": platform modeli kendisi seçer. İstersen model adı yaz (örn. glm-5.3)
        model = os.environ.get("EVREN_MODEL", "auto")
    elif LLM_PROVIDER == "grok":
        client = AsyncOpenAI(
            api_key=os.environ["GROK_API_KEY"],
            base_url=os.environ.get("GROK_BASE_URL", "https://api.x.ai/v1"),
        )
        # Model adını console.x.ai'daki güncel listeden kontrol et
        model = os.environ.get("GROK_MODEL", "grok-4")
    else:
        raise RuntimeError(
            f"Geçersiz LLM_PROVIDER: {LLM_PROVIDER!r}. 'evren' ya da 'grok' olmalı."
        )
    return client, model


client, MODEL = build_client()
logger.info("Yapay zeka sağlayıcısı: %s, model: %s", LLM_PROVIDER, MODEL)


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
    """Kullanıcıdan gelen mesajı seçili yapay zekaya iletir ve yanıtı döner."""
    user_text = update.message.text

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action=ChatAction.TYPING
    )

    try:
        response = await client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_text},
            ],
        )
        reply_text = response.choices[0].message.content or "Bir yanıt üretilemedi."
    except Exception:
        logger.exception("Yapay zeka çağrısında hata oluştu (sağlayıcı: %s)", LLM_PROVIDER)
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
