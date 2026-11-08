import logging
import os
from google import genai
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

# Log ayarları
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# Bot Token ve Gemini API Anahtarınız
TELEGRAM_BOT_TOKEN = "9232669675:AAEH1x_CVSRIdCusgMLuBryIBeaMWhtSxyo"
GEMINI_API_KEY = "AQ.Ab8RN6II7TfR1atxdWaMHmz2a7JKYrDoUpwtXvdSUt1mEhQ8Pg"

# Gemini İstemcisi
client = genai.Client(api_key=GEMINI_API_KEY)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/start komutu verildiğinde çalışır."""
    await update.message.reply_text("Merhaba! Ben Momera Yemek Takip yapay zeka asistanınızım. Size nasıl yardımcı olabilirim?")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Kullanıcıdan gelen mesajları Gemini'ye iletir ve yanıtı döner."""
    user_text = update.message.text
    
    # Telegram ekranında 'yazıyor...' durumunu gösterir
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    
    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=user_text,
        )
        
        reply_text = response.text if response.text else "Bir yanıt üretilemedi."
        await update.message.reply_text(reply_text)

    except Exception as e:
        logging.error(f"Hata oluştu: {e}")
        await update.message.reply_text("Üzgünüm, bir hata oluştu.")

def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Bot başarıyla çalıştırıldı ve dinlemeye başladı...")
    app.run_polling()

if __name__ == "__main__":
    main()
