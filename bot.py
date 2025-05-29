import os 
from dotenv import load_dotenv
from telegram import Update , File
from telegram.ext import ApplicationBuilder,CommandHandler,ContextTypes,filters,MessageHandler
from telegram.constants import ChatAction

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

app = ApplicationBuilder().token(BOT_TOKEN).build()

# start handler
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🖐Welcome to the PDF-Toolkit Bot")

app.add_handler(CommandHandler('start', start))

# single file handler
async def file_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    file = None
    filename = None

    if update.message.document:
        file_id = update.message.document.file_id
        filename = update.message.document.file_name.lower()
        file = await context.bot.get_file(file_id)

    elif update.message.photo:
        file = await update.message.photo[-1].get_file()
        filename = f"{update.message.photo[-1].file_id}.jpg"

    else:
        await update.message.reply_text("Please send photo or document only.")
        return

    if not os.path.exists("downloads"):
        os.makedirs("downloads")

    file_path = f"downloads/{filename}"
    await file.download_to_drive(file_path)
    await update.message.reply_text(f"File '{filename}' saved successfully ✅")

app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, file_handler))

app.run_polling()
