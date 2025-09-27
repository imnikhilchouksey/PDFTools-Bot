import os
import tempfile
import shutil
import logging
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, Bot
from telegram.constants import ChatAction
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
from reportlab.pdfgen import canvas
from PIL import Image
from PyPDF2 import PdfReader, PdfWriter
import pdfplumber
from docx import Document
from fastapi import FastAPI, Request

# ------------------------
# CONFIG
# ------------------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID")) if os.getenv("CHANNEL_ID") else None

if not BOT_TOKEN or not CHANNEL_ID:
    raise SystemExit("Please set BOT_TOKEN and CHANNEL_ID in .env (CHANNEL_ID must include -100 prefix)")

# ------------------------
# Logging
# ------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ------------------------
# User sessions
# ------------------------
user_sessions = {}

def ensure_user_session(user_id):
    s = user_sessions.setdefault(user_id, {})
    s.setdefault("images", [])
    s.setdefault("pdfs", [])
    s.setdefault("collecting_images", False)
    s.setdefault("collecting_pdfs", False)
    return s

# ------------------------
# PDF / Image helpers
# ------------------------
async def download_pdf(bot, file_id, dest_path):
    file_obj = await bot.get_file(file_id)
    await file_obj.download_to_drive(dest_path)

def images_to_pdf(image_paths, pdf_path):
    c = canvas.Canvas(pdf_path)
    for img_path in image_paths:
        img = Image.open(img_path)
        width, height = img.size
        c.setPageSize((width, height))
        c.drawImage(img_path, 0, 0, width=width, height=height)
        c.showPage()
    c.save()

def merge_pdfs(paths, output_path):
    writer = PdfWriter()
    for p in paths:
        reader = PdfReader(p)
        for page in reader.pages:
            writer.add_page(page)
    with open(output_path, "wb") as f:
        writer.write(f)

def split_pdf(path, pages, output_dir):
    reader = PdfReader(path)
    output_files = []
    for i, page in enumerate(reader.pages):
        if i+1 in pages:
            writer = PdfWriter()
            writer.add_page(page)
            out_file = os.path.join(output_dir, f"page_{i+1}.pdf")
            with open(out_file, "wb") as f:
                writer.write(f)
            output_files.append(out_file)
    return output_files

def extract_text_from_pdf(path):
    text = ""
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            text += page.extract_text() + "\n"
    return text

def pdf_to_word(pdf_path, output_path):
    text = extract_text_from_pdf(pdf_path)
    doc = Document()
    doc.add_paragraph(text)
    doc.save(output_path)

# ------------------------
# Keyboard buttons
# ------------------------
MAIN_BUTTONS = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton("🖼️ Add Image"), KeyboardButton("📄 Create PDF")],
        [KeyboardButton("📥 Add PDF")],
        [KeyboardButton("🔗 Merge PDFs"), KeyboardButton("✂️ Split PDF")],
        [KeyboardButton("🔍 Extract Text"), KeyboardButton("📝 PDF → Word")],
        [KeyboardButton("🛑 Cancel")]
    ],
    resize_keyboard=True
)

# ------------------------
# Handlers
# ------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user_session(update.message.from_user.id)
    await update.message.reply_text(
        "👋 Welcome to PDFTools.\nUse the buttons below to interact.",
        reply_markup=MAIN_BUTTONS
    )

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    text = (update.message.text or "").strip()
    session = ensure_user_session(user_id)

    if text == "🛑 Cancel":
        session.clear()
        await update.message.reply_text("🗑️ Session cleared.")
        return

    if text == "🖼️ Add Image":
        session["collecting_images"] = True
        session["images"] = []
        await update.message.reply_text("📸 Send images now.")
        return

    if text == "📄 Create PDF":
        if not session.get("images"):
            await update.message.reply_text("⚠️ No images. Press 🖼️ Add Image first.")
            return
        await update.message.reply_text("⏳ Creating PDF...")
        tmp_dir = tempfile.mkdtemp()
        try:
            paths = []
            for idx, file_id in enumerate(session["images"], start=1):
                local_path = os.path.join(tmp_dir, f"{idx}.jpg")
                await download_pdf(context.bot, file_id, local_path)
                paths.append(local_path)
            output_pdf = os.path.join(tmp_dir, "output.pdf")
            images_to_pdf_reportlab(paths, output_pdf)
            with open(output_pdf, "rb") as f:
                msg = await context.bot.send_document(chat_id=CHANNEL_ID, document=f)
                pdf_id = msg.document.file_id
            session["pdfs"] = [pdf_id]
            session["images"] = []
            session["collecting_images"] = False
            await context.bot.send_document(chat_id=update.effective_chat.id, document=pdf_id)
        finally:
            shutil.rmtree(tmp_dir)
        return

async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    session = ensure_user_session(user_id)
    if not session.get("collecting_images"):
        await update.message.reply_text("⚠️ Click 🖼️ Add Image first.")
        return
    photo = update.message.photo[-1]
    msg = await context.bot.send_photo(chat_id=CHANNEL_ID, photo=photo.file_id)
    session.setdefault("images", []).append(msg.photo[-1].file_id)
    await update.message.reply_text("✅ Image saved. Send more or press 📄 Create PDF.")

async def document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    session = ensure_user_session(user_id)
    doc = update.message.document
    if not doc:
        return
    if doc.mime_type == "application/pdf":
        msg = await context.bot.send_document(chat_id=CHANNEL_ID, document=doc.file_id)
        session["pdfs"] = [msg.document.file_id]
        session["collecting_pdfs"] = False
        await update.message.reply_text("✅ PDF saved!")
    elif doc.mime_type in ["image/jpeg", "image/png"]:
        if not session.get("collecting_images"):
            await update.message.reply_text("⚠️ Click 🖼️ Add Image first.")
            return
        msg = await context.bot.send_document(chat_id=CHANNEL_ID, document=doc.file_id)
        session.setdefault("images", []).append(msg.document.file_id)
        await update.message.reply_text("✅ Image saved. Send more or press 📄 Create PDF.")

# ------------------------
# FastAPI + Webhook setup
# ------------------------
fastapi_app = FastAPI()
bot = Bot(BOT_TOKEN)
application = ApplicationBuilder().token(BOT_TOKEN).build()

application.add_handler(CommandHandler("start", start))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
application.add_handler(MessageHandler(filters.PHOTO, photo_handler))
application.add_handler(MessageHandler(filters.Document.ALL, document_handler))

@fastapi_app.post(f"/webhook/{BOT_TOKEN}")
async def telegram_webhook(req: Request):
    data = await req.json()
    update = Update.de_json(data, bot)
    await application.update_queue.put(update)
    return {"ok": True}

@fastapi_app.post("/")
async def root(): 
    return {"status" : "Bot is running"}

@fastapi_app.get("/")
async def root():
    return {"status": "Bot is running"}

@fastapi_app.on_event("startup")
async def on_startup():
    url = f"https://pdftoolkit-bot.onrender.com/webhook/{BOT_TOKEN}"
    await bot.set_webhook(url)
    logger.info(f"Webhook set to {url}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(fastapi_app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))

# ------------------------ App setup ------------------------
def main():
    
    threading.Thread(target=run_fastapi, daemon=True).start()

    
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(MessageHandler(filters.Document.ALL, document_handler))  
    logger.info("Bot started with PDF & image functionalities + channel storage")
    app.run_polling()

if __name__ == "__main__":
    main()
