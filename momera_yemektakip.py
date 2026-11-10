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

# --- Genel ayarlar (Render > Environment sekmesinden gelir, koda yazılmaz) ---
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")  # opsiyonel
PORT = int(os.environ.get("PORT", "10000"))
# Render bu değişkeni otomatik verir (ör. https://momera-yemektakip-ai-bot.onrender.com)
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL")

TELEGRAM_MAX_LEN = 4096
# Düşünen modellerde (DeepSeek, Qwen) düşünme + yanıt aynı bütçeyi kullanır; EVREN en az 2048 önerir
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "4096"))
# Sohbet geçmişinde tutulacak en fazla mesaj sayısı (kullanıcı + bot)
MAX_HISTORY = int(os.environ.get("MAX_HISTORY", "20"))

# --- Kural dosyası (prompt.md) ---
# Kurallar artık koddan değil, GitHub'daki prompt.md dosyasından okunur.
PROMPT_FILE = os.environ.get("PROMPT_FILE", "prompt.md")
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

# --- Yapay zeka sağlayıcısı seçimi ---
# Render'da LLM_PROVIDER = evren  ya da  grok  yazarak seçersin (varsayılan: evren).
# İkisi de OpenAI uyumlu olduğu için aynı kütüphane ile çalışır.
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "evren").strip().lower()


def build_client() -> tuple[AsyncOpenAI, str, str]:
    """Seçilen sağlayıcıya göre istemciyi, metin modelini ve resim modelini döndürür."""
    if LLM_PROVIDER == "evren":
        api_key = os.environ["EVREN_API_KEY"]
        client = AsyncOpenAI(
            api_key=api_key,
            base_url=os.environ.get("EVREN_BASE_URL", "https://evren-llmapi.ssyz.org.tr/v1"),
            # EVREN anahtarı X-API-Key başlığıyla beklenir
            default_headers={"X-API-Key": api_key},
        )
        model = os.environ.get("EVREN_MODEL", "auto")
        # Resim okuyabilen model ("auto" yalnızca metin destekler)
        vision_model = os.environ.get("EVREN_VISION_MODEL", "deepseek-v4.1-flash")
    elif LLM_PROVIDER == "grok":
        client = AsyncOpenAI(
            api_key=os.environ["GROK_API_KEY"],
            base_url=os.environ.get("GROK_BASE_URL", "https://api.x.ai/v1"),
        )
        model = os.environ.get("GROK_MODEL", "grok-4")
        vision_model = os.environ.get("GROK_VISION_MODEL", model)
    else:
        raise RuntimeError(
            f"Geçersiz LLM_PROVIDER: {LLM_PROVIDER!r}. 'evren' ya da 'grok' olmalı."
        )
    return client, model, vision_model


client, MODEL, VISION_MODEL = build_client()
logger.info(
    "Yapay zeka sağlayıcısı: %s, model: %s, resim modeli: %s",
    LLM_PROVIDER, MODEL, VISION_MODEL,
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
        reply_text = await ask_llm(MODEL, get_history(context), user_text)
    except Exception:
        logger.exception("Yapay zeka çağrısında hata oluştu (sağlayıcı: %s)", LLM_PROVIDER)
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
        reply_text = await ask_llm(VISION_MODEL, get_history(context), user_content)
    except Exception:
        logger.exception("Resim okuma çağrısında hata oluştu (model: %s)", VISION_MODEL)
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
