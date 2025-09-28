import os
import tempfile
import shutil
import logging
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
from telegram import Update, Bot, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters
)
from reportlab.pdfgen import canvas
from PIL import Image
from PyPDF2 import PdfReader, PdfWriter
import pdfplumber
from docx import Document

# ----------------- CONFIG -----------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")  # mandatory (string)
RENDER_URL = os.getenv("RENDER_URL")  # eg: https://pdftoolkit-bot.onrender.com
PORT = int(os.environ.get("PORT", 8000))

if not BOT_TOKEN or not CHANNEL_ID or not RENDER_URL:
    raise SystemExit("Please set BOT_TOKEN, CHANNEL_ID and RENDER_URL in environment or .env")

try:
    CHANNEL_ID = int(CHANNEL_ID)
except Exception as e:
    raise SystemExit("CHANNEL_ID must be an integer (channel chat id), e.g. -1001234567890")

# ----------------- Logging -----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ----------------- App & Telegram objects (constructed, not started) -----------------
fastapi_app = FastAPI()
bot = Bot(token=BOT_TOKEN)
application = Application.builder().token(BOT_TOKEN).build()

# ----------------- Session store (in-memory) -----------------
user_sessions = {}
def ensure_user_session(user_id: int):
    s = user_sessions.setdefault(user_id, {})
    s.setdefault("images", [])
    s.setdefault("pdfs", [])
    s.setdefault("collecting_images", False)
    s.setdefault("collecting_pdfs", False)
    return s

# ----------------- Helpers -----------------
async def download_file(bot_obj: Bot, file_id: str, dest_path: str):
    f = await bot_obj.get_file(file_id)
    await f.download_to_drive(dest_path)

def images_to_pdf_reportlab(image_paths, pdf_path):
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

def extract_text_from_pdf(path):
    text = ""
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text += page_text + "\n"
    return text

def pdf_to_word(pdf_path, output_path):
    text = extract_text_from_pdf(pdf_path)
    doc = Document()
    for para in text.split("\n"):
        if para.strip():
            doc.add_paragraph(para)
    doc.save(output_path)

# ----------------- Keyboard -----------------
MAIN_BUTTONS = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton("🖼️ Add Image"), KeyboardButton("📄 Create PDF")],
        [KeyboardButton("📥 Add PDF")],
        [KeyboardButton("🔗 Merge PDFs"), KeyboardButton("✂️ Split PDF")],
        [KeyboardButton("🔍 Extract Text"), KeyboardButton("📝 PDF → Word")],
        [KeyboardButton("🛑 Cancel")]
    ],
    resize_keyboard=True,
    one_time_keyboard=False
)

# ----------------- Handlers -----------------
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user_session(update.effective_user.id)
    await update.message.reply_text("👋 Welcome to PDFTools bot.\nUse the buttons below to interact.", reply_markup=MAIN_BUTTONS)

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = (update.message.text or "").strip()
    session = ensure_user_session(user_id)

    if text == "🛑 Cancel":
        user_sessions.pop(user_id, None)
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
                await download_file(context.bot, file_id, local_path)
                paths.append(local_path)
            output_pdf = os.path.join(tmp_dir, "output.pdf")
            images_to_pdf_reportlab(paths, output_pdf)
            with open(output_pdf, "rb") as f:
                await context.bot.send_document(chat_id=update.effective_chat.id, document=f)
            # optionally store on channel too
            with open(output_pdf, "rb") as f2:
                await context.bot.send_document(chat_id=CHANNEL_ID, document=f2)
            session["images"] = []
            session["collecting_images"] = False
        finally:
            shutil.rmtree(tmp_dir)
        return

    if text == "📥 Add PDF":
        session["collecting_pdfs"] = True
        await update.message.reply_text("📁 Send PDF file now.")
        return

    if text == "🔍 Extract Text":
        if not session.get("pdfs"):
            await update.message.reply_text("⚠️ No PDF in session. Send one first (📥 Add PDF).")
            return
        await update.message.reply_text("⏳ Extracting text...")
        tmp_dir = tempfile.mkdtemp()
        try:
            pdf_file_id = session["pdfs"][-1]
            local_pdf = os.path.join(tmp_dir, "in.pdf")
            await download_file(context.bot, pdf_file_id, local_pdf)
            text = extract_text_from_pdf(local_pdf)
            if not text.strip():
                await update.message.reply_text("⚠️ No text found in PDF.")
            else:
                for chunk_start in range(0, len(text), 4000):
                    await update.message.reply_text(text[chunk_start:chunk_start+4000])
        finally:
            shutil.rmtree(tmp_dir)
        return

    if text == "📝 PDF → Word":
        if not session.get("pdfs"):
            await update.message.reply_text("⚠️ No PDF in session. Send one first (📥 Add PDF).")
            return
        await update.message.reply_text("⏳ Converting to Word...")
        tmp_dir = tempfile.mkdtemp()
        try:
            pdf_file_id = session["pdfs"][-1]
            local_pdf = os.path.join(tmp_dir, "in.pdf")
            out_docx = os.path.join(tmp_dir, "out.docx")
            await download_file(context.bot, pdf_file_id, local_pdf)
            pdf_to_word(local_pdf, out_docx)
            with open(out_docx, "rb") as f:
                await context.bot.send_document(chat_id=update.effective_chat.id, document=f)
        finally:
            shutil.rmtree(tmp_dir)
        return

    await update.message.reply_text("Use the keyboard buttons or /start.", reply_markup=MAIN_BUTTONS)

async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    session = ensure_user_session(user_id)
    if not session.get("collecting_images"):
        await update.message.reply_text("⚠️ Click 🖼️ Add Image first.")
        return
    photo = update.message.photo[-1]
    # store via channel to keep file available longer
    msg = await context.bot.send_photo(chat_id=CHANNEL_ID, photo=photo.file_id)
    session.setdefault("images", []).append(msg.photo[-1].file_id)
    await update.message.reply_text("✅ Image saved. Send more or press 📄 Create PDF.")

async def document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    session = ensure_user_session(user_id)
    doc = update.message.document
    if not doc:
        return
    mt = doc.mime_type or ""
    if mt == "application/pdf":
        msg = await context.bot.send_document(chat_id=CHANNEL_ID, document=doc.file_id)
        session["pdfs"] = [msg.document.file_id]
        session["collecting_pdfs"] = False
        await update.message.reply_text("✅ PDF saved!")
    elif mt in ["image/jpeg", "image/png"]:
        msg = await context.bot.send_document(chat_id=CHANNEL_ID, document=doc.file_id)
        session.setdefault("images", []).append(msg.document.file_id)
        await update.message.reply_text("✅ Image saved. Send more or press 📄 Create PDF.")
    else:
        await update.message.reply_text("⚠️ Unsupported document type.")

# register handlers
application.add_handler(CommandHandler("start", start_handler))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
application.add_handler(MessageHandler(filters.PHOTO, photo_handler))
application.add_handler(MessageHandler(filters.Document.ALL, document_handler))

# ----------------- Webhook endpoint -----------------
@fastapi_app.post("/webhook")
async def telegram_webhook(req: Request):
    data = await req.json()
    logger.info("Received update: %s", data.get("update_id"))
    update = Update.de_json(data, bot)
    # push update for processing by PTB application
    await application.update_queue.put(update)
    return {"ok": True}

@fastapi_app.get("/")
async def root():
    return {"status": "Bot is running"}

# ----------------- Lifespan (preferred over on_event) -----------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    webhook_url = f"{RENDER_URL.rstrip('/')}/webhook"
    # set webhook
    await bot.set_webhook(webhook_url)
    logger.info("Webhook set to %s", webhook_url)

    # initialize and start application so it processes update_queue
    await application.initialize()
    await application.start()
    logger.info("Telegram Application initialized and started.")

    try:
        yield
    finally:
        # stop and shutdown application cleanly
        await application.stop()
        await application.shutdown()
        logger.info("Telegram Application stopped.")

fastapi_app.router.lifespan_context = lifespan

# ----------------- Run (only when running main.py directly) -----------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:fastapi_app", host="0.0.0.0", port=PORT, log_level="info")
