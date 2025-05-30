import os
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters
)
from telegram.constants import ChatAction
import fitz  # PyMuPDF
from PIL import Image

# Load environment
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

# Start bot app
app = ApplicationBuilder().token(BOT_TOKEN).build()

# In-memory user sessions
user_sessions = {}

# === Start Handler ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🖐 Welcome to the PDF-Toolkit Bot!")

app.add_handler(CommandHandler('start', start))

# === Image to PDF: Start session ===
async def image_to_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    user_sessions[user_id] = {"images": []}

    await update.message.reply_text(
        "🖼 Please send images one by one.\nWhen you're done, send /done to generate the PDF."
    )

app.add_handler(CommandHandler("image2pdf", image_to_pdf))

# === Collect Images ===
async def collect_images(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id

    # Ignore if no session
    if user_id not in user_sessions:
        return

    photo = update.message.photo[-1]
    file = await photo.get_file()

    user_folder = f"downloads/{user_id}"
    os.makedirs(user_folder, exist_ok=True)

    file_path = f"{user_folder}/{photo.file_id}.jpg"
    await file.download_to_drive(file_path)

    user_sessions[user_id]["images"].append(file_path)

    await update.message.reply_text("✅ Image saved. Send more or /done to create PDF.")

app.add_handler(MessageHandler(filters.PHOTO, collect_images))

# === Generate PDF from images ===
async def generate_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id

    if user_id not in user_sessions or not user_sessions[user_id]["images"]:
        await update.message.reply_text("❌ No images found. Start with /image2pdf.")
        return

    images = user_sessions[user_id]["images"]
    images.sort()

    image_objs = [Image.open(p).convert("RGB") for p in images]

    output_pdf_path = f"downloads/{user_id}/output.pdf"
    image_objs[0].save(output_pdf_path, save_all=True, append_images=image_objs[1:])

    with open(output_pdf_path, "rb") as pdf_file:
        await context.bot.send_document(chat_id=update.effective_chat.id, document=pdf_file)

    await update.message.reply_text("✅ PDF generated and sent!")

    # Cleanup
    for p in images:
        os.remove(p)
    os.remove(output_pdf_path)
    del user_sessions[user_id]

app.add_handler(CommandHandler("done", generate_pdf))

# === PDF to Image ===
async def pdf_to_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_PHOTO)

    pdf_files = [f for f in os.listdir("downloads") if f.endswith(".pdf")]
    if not pdf_files:
        await update.message.reply_text("❌ No PDF found to convert.")
        return

    pdf_path = f"downloads/{pdf_files[-1]}"
    doc = fitz.open(pdf_path)

    for i, page in enumerate(doc):
        pix = page.get_pixmap(dpi=150)
        img_path = f"downloads/page_{i+1}.jpg"
        pix.save(img_path)

        with open(img_path, "rb") as img_file:
            await update.message.reply_photo(img_file)

    await update.message.reply_text("✅ All pages converted and sent!")

app.add_handler(CommandHandler("convert", pdf_to_image))

# === File Upload Handler ===
async def file_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    file = None
    filename = None

    if update.message.document:
        file_id = update.message.document.file_id
        filename = update.message.document.file_name.lower()
        file = await context.bot.get_file(file_id)

    elif update.message.photo:
        return  # Ignore photo here to let collect_images() handle it

    else:
        await update.message.reply_text("❌ Please send only PDFs or photos.")
        return

    os.makedirs("downloads", exist_ok=True)
    file_path = f"downloads/{filename}"
    await file.download_to_drive(file_path)

    await update.message.reply_text(f"✅ File '{filename}' saved successfully.")

app.add_handler(MessageHandler(filters.Document.ALL, file_handler))

# === Start polling ===
app.run_polling()
