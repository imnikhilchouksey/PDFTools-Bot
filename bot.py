import os
import tempfile
import shutil
import logging
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
from telegram.constants import ChatAction
from reportlab.pdfgen import canvas
from PIL import Image
from PyPDF2 import PdfReader, PdfWriter
import pdfplumber
from docx import Document

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
# In-memory sessions
# ------------------------
user_sessions = {}

# ------------------------
# PDF / Image helpers
# ------------------------
def ensure_user_session(user_id):
    s = user_sessions.setdefault(user_id, {})
    s.setdefault("images", [])
    s.setdefault("pdfs", [])
    s.setdefault("collecting_images", False)
    s.setdefault("collecting_pdfs", False)
    return s

async def download_pdf(bot, file_id, dest_path):
    file_obj = await bot.get_file(file_id)
    await file_obj.download_to_drive(dest_path)

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
    resize_keyboard=True,
    one_time_keyboard=False
)

# ------------------------
# Handlers
# ------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user_session(update.message.from_user.id)
    await update.message.reply_text(
        "👋 Welcome to PDF-Toolkit.\nUse the buttons below to interact.",
        reply_markup=MAIN_BUTTONS
    )

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    text = (update.message.text or "").strip()
    session = ensure_user_session(user_id)

    # ---------------- Cancel ----------------
    if text == "🛑 Cancel":
        session.clear()
        await update.message.reply_text("🗑️ Session cleared. Start fresh with new actions.")
        return

    # ---------------- Images ----------------
    if text == "🖼️ Add Image":
        session["collecting_images"] = True
        session.setdefault("images", [])
        await update.message.reply_text("📸 Send images now.")
        return

    if text == "📄 Create PDF":
        if not session.get("images"):
            await update.message.reply_text("⚠️ You have not added any images. Press 🖼️ Add Image first.")
            return

        await update.message.reply_text("⏳ Creating PDF... (keeping original image quality)")
        tmp_dir = tempfile.mkdtemp(prefix=f"pdf_{user_id}_")
        try:
            temp_paths = []
            for idx, file_id in enumerate(session["images"], start=1):
                file_obj = await context.bot.get_file(file_id)
                local_path = os.path.join(tmp_dir, f"img_{idx}.jpg")
                await file_obj.download_to_drive(local_path)
                temp_paths.append(local_path)

            output_pdf = os.path.join(tmp_dir, "output.pdf")
            images_to_pdf_reportlab(temp_paths, output_pdf)

            with open(output_pdf, "rb") as fpdf:
                msg = await context.bot.send_document(chat_id=CHANNEL_ID, document=fpdf,
                                                      caption=f"PDF generated for user:{user_id}")
            pdf_file_id = msg.document.file_id
            session["pdfs"] = [pdf_file_id]  
            session["images"] = []
            session["collecting_images"] = False

            await context.bot.send_document(chat_id=update.effective_chat.id, document=pdf_file_id)
            await update.message.reply_text("✅ PDF created.")
        finally:
            try: shutil.rmtree(tmp_dir)
            except: pass
        return

    # ---------------- PDFs ----------------
    if text == "📥 Add PDF":
        session["collecting_pdfs"] = True
        session["pdfs"] = []  
        await update.message.reply_text("📄 Send your PDF now.")
        return

    # ---------------- Merge PDFs ----------------
    if text == "🔗 Merge PDFs":
        if len(session.get("pdfs", [])) < 2:
            await update.message.reply_text("⚠️ You need at least 2 PDFs to merge.")
            return
        tmp_dir = tempfile.mkdtemp(prefix=f"merge_{user_id}_")
        try:
            pdf_paths = []
            for idx, fid in enumerate(session["pdfs"], start=1):
                local_path = os.path.join(tmp_dir, f"{idx}.pdf")
                await download_pdf(context.bot, fid, local_path)
                pdf_paths.append(local_path)

            merged_pdf = os.path.join(tmp_dir, "merged.pdf")
            merge_pdfs(pdf_paths, merged_pdf)

            with open(merged_pdf, "rb") as fpdf:
                msg = await context.bot.send_document(chat_id=CHANNEL_ID, document=fpdf,
                                                      caption=f"Merged PDF for user:{user_id}")
            merged_fid = msg.document.file_id
            session["pdfs"] = [merged_fid] 

            await context.bot.send_document(chat_id=update.effective_chat.id, document=merged_fid)
            await update.message.reply_text("✅ PDFs merged successfully.")
        finally:
            try: shutil.rmtree(tmp_dir)
            except: pass
        return

    # ---------------- Split PDFs ----------------
    if text == "✂️ Split PDF":
        if not session.get("pdfs"):
            await update.message.reply_text("⚠️ You have no PDFs to split. Add PDFs first.")
            return

        fid = session["pdfs"][-1] 
        tmp_dir = tempfile.mkdtemp(prefix=f"split_{user_id}_")
        try:
            pdf_path = os.path.join(tmp_dir, "pdf_to_split.pdf")
            await download_pdf(context.bot, fid, pdf_path)
            reader = PdfReader(pdf_path)
            pages_to_split = list(range(1, len(reader.pages) + 1))
            output_files = split_pdf(pdf_path, pages_to_split, tmp_dir)

            split_fids = []
            for fpath in output_files:
                with open(fpath, "rb") as fp:
                    msg = await context.bot.send_document(chat_id=CHANNEL_ID, document=fp,
                                                          caption=f"Split PDF for user:{user_id}")
                    split_fid = msg.document.file_id
                    split_fids.append(split_fid)
                await context.bot.send_document(chat_id=update.effective_chat.id, document=split_fid,
                                                caption=f"Split page from your PDF")

            session["pdfs"] = split_fids  # Only keep the latest split PDFs
            await update.message.reply_text(f"✅ PDF split into {len(split_fids)} pages.")
        finally:
            try: shutil.rmtree(tmp_dir)
            except: pass
        return

    # ---------------- Extract Text ----------------
    if text == "🔍 Extract Text":
        if not session.get("pdfs"):
            await update.message.reply_text("⚠️ You have no PDFs to extract text from.")
            return

        fid = session["pdfs"][-1]  # Latest PDF
        tmp_dir = tempfile.mkdtemp(prefix=f"extract_{user_id}_")
        try:
            pdf_path = os.path.join(tmp_dir, "pdf.pdf")
            await download_pdf(context.bot, fid, pdf_path)
            text_content = extract_text_from_pdf(pdf_path)
            if not text_content.strip():
                text_content = "No text found in PDF."
            await update.message.reply_text(f"📄 Extracted text:\n{text_content[:4000]}")
        finally:
            try: shutil.rmtree(tmp_dir)
            except: pass
        return

    # ---------------- PDF → Word ----------------
    if text == "📝 PDF → Word":
        if not session.get("pdfs"):
            await update.message.reply_text("⚠️ You have no PDFs to convert.")
            return

        fid = session["pdfs"][-1] 
        tmp_dir = tempfile.mkdtemp(prefix=f"word_{user_id}_")
        try:
            pdf_path = os.path.join(tmp_dir, "pdf.pdf")
            await download_pdf(context.bot, fid, pdf_path)
            word_path = os.path.join(tmp_dir, "output.docx")
            pdf_to_word(pdf_path, word_path)

            with open(word_path, "rb") as fword:
                msg = await context.bot.send_document(chat_id=CHANNEL_ID, document=fword,
                                                      caption=f"Word file for user:{user_id}")
                word_fid = msg.document.file_id

            await context.bot.send_document(chat_id=update.effective_chat.id, document=word_fid)
            await update.message.reply_text("✅ PDF converted to Word successfully.")
        finally:
            try: shutil.rmtree(tmp_dir)
            except: pass
        return

    await update.message.reply_text("Use the buttons below (or /start).")

# ------------------------ PDF & Image Upload Handlers ----------------
async def pdf_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    session = ensure_user_session(user_id)
    if not session.get("collecting_pdfs"):
        await update.message.reply_text("⚠️ Click 📥 Add PDF first to upload a file.")
        return

    pdf_doc = update.message.document
    if not pdf_doc or pdf_doc.mime_type != "application/pdf":
        await update.message.reply_text("⚠️ Please send a valid PDF file.")
        return

    msg = await context.bot.send_document(chat_id=CHANNEL_ID, document=pdf_doc.file_id,
                                          caption=f"user:{user_id} PDF")
    pdf_file_id = msg.document.file_id
    session["pdfs"] = [pdf_file_id]  
    session["collecting_pdfs"] = False
    await update.message.reply_text("✅ PDF saved! You can now Split, Merge, Extract Text or convert to Word.")

async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    session = ensure_user_session(user_id)
    if not session.get("collecting_images"):
        session["collecting_images"] = True

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_PHOTO)
    photo = update.message.photo[-1]
    try:
        msg = await context.bot.send_photo(chat_id=CHANNEL_ID, photo=photo.file_id,
                                           caption=f"user:{user_id} (image)")
        channel_file_id = msg.photo[-1].file_id
        session.setdefault("images", []).append(channel_file_id)
        await update.message.reply_text("✅ Image saved. Send more or press 📄 Create PDF.")
    except Exception as e:
        logger.exception("Failed to save image")
        await update.message.reply_text(f"❌ Failed to save image: {e}")

async def document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    session = ensure_user_session(user_id)
    
    doc = update.message.document
    if not doc:
        return

    if doc.mime_type in ["image/jpeg", "image/png"]:
        if not session.get("collecting_images"):
            await update.message.reply_text("⚠️ Click 🖼️ Add Image first to upload an image file.")
            return
        try:
            msg = await context.bot.send_document(chat_id=CHANNEL_ID, document=doc.file_id,
                                                  caption=f"user:{user_id} (image file)")
            channel_file_id = msg.document.file_id
            session.setdefault("images", []).append(channel_file_id)
            await update.message.reply_text("✅ Image saved. Send more or press 📄 Create PDF.")
        except Exception as e:
            logger.exception("Failed to save image file")
            await update.message.reply_text(f"❌ Failed to save image: {e}")
        return

    if doc.mime_type == "application/pdf":
        await pdf_handler(update, context)

# ------------------------ App setup ----------------
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(MessageHandler(filters.Document.ALL, document_handler))  
    logger.info("Bot started with PDF & image functionalities + channel storage")
    app.run_polling()

if __name__ == "__main__":
    main()
