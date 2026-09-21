# bot.py
# Установка: pip install python-telegram-bot==21.6 httpx python-docx pypdf
# Запуск:    python bot.py

import os
import asyncio
import logging
import httpx
from telegram import Update
from telegram.constants import ChatAction
from dotenv import load_dotenv
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)

load_dotenv()

logging.basicConfig(level=logging.INFO)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
AI_API_KEY = os.getenv("AI_API_KEY")

# --- Вариант 1: OpenRouter (бесплатные модели) ---
API_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = os.getenv("OPENROUTER_MODEL", "qwen/qwen3.8-27b:free")
MODEL_FALLBACKS = [
    "google/gemma-4-26b-a4b-it:free",
    "liquid/lfm-2.5-2.6b:free",
    "nex-agi/nex-n2.5-mini:free",
]

# --- Вариант 2: Groq (раскомментировать вместо блока выше) ---
# API_URL = "https://api.groq.com/openai/v1/chat/completions"
# MODEL = "llama-3.3-70b-versatile"

MAX_HISTORY = 12          # сообщений (6 пар) в памяти диалога
MAX_DOC_CHARS = 25000     # ограничение на объём документа
AI_TIMEOUT = 90

async def ask_ai(history: list[dict]) -> str:
    headers = {
        "Authorization": f"Bearer {AI_API_KEY}",
        "Content-Type": "application/json",
    }
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
    ] + history
    models = list(dict.fromkeys([MODEL] + MODEL_FALLBACKS))

    async with httpx.AsyncClient(timeout=AI_TIMEOUT) as client:
        errors = []
        for model in models:
            payload = {
                "model": model,
                "messages": messages,
                "temperature": 0.2,
                "max_tokens": 1500,
            }
            try:
                response = await client.post(API_URL, headers=headers, json=payload)
            except httpx.HTTPError as error:
                errors.append(f"{model}: {error}")
                continue

            if response.status_code == 200:
                data = response.json()
                choices = data.get("choices", [])
                if choices:
                    content = choices[0].get("message", {}).get("content", "")
                    if content:
                        return content
                errors.append(f"{model}: пустой ответ")
                continue

            errors.append(f"{model}: HTTP {response.status_code}")

        return "Не удалось получить ответ от бесплатных моделей OpenRouter. " \
               "Попробуйте повторить запрос через минуту или добавьте ключ провайдера.\n" \
               + "\n".join(errors)


def extract_text(path: str) -> str:
    low = path.lower()
    if low.endswith(".docx"):
        import docx
        doc = docx.Document(path)
        parts = [p.text for p in doc.paragraphs]
        for t in doc.tables:
            for row in t.rows:
                parts.append(" | ".join(c.text for c in row.cells))
        return "\n".join(parts)
    if low.endswith(".pdf"):
        from pypdf import PdfReader
        reader = PdfReader(path)
        return "\n".join((p.extract_text() or "") for p in reader.pages)
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return f.read()


def get_history(context: ContextTypes.DEFAULT_TYPE) -> list[dict]:
    return context.user_data.setdefault("history", [])


async def send_long(update: Update, text: str) -> None:
    """Telegram не принимает сообщения длиннее 4096 символов."""
    limit = 4000
    for i in range(0, len(text), limit):
        await update.message.reply_text(text[i:i + limit])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["history"] = []
    await update.message.reply_text(
        "Юридический ассистент (РК, взыскание задолженности).\n\n"
        "Просто напишите свой вопрос или отправьте документ (.docx, .pdf, .txt).\n"
        "• /reset — очистить контекст диалога\n\n"
        "Внимание: ответы носят справочный характер и не заменяют юриста."
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["history"] = []
    await update.message.reply_text("Контекст очищен. Напишите новый вопрос.")


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    history = get_history(context)
    history.append({"role": "user", "content": update.message.text})
    del history[:-MAX_HISTORY]

    await update.message.chat.send_action(ChatAction.TYPING)
    try:
        answer = await ask_ai(history)
    except Exception:
        logging.exception("Ошибка при запросе к AI")
        await update.message.reply_text(
            "Не удалось получить ответ от AI. Попробуйте повторить запрос позже."
        )
        return
    history.append({"role": "assistant", "content": answer})
    await send_long(update, answer)


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc.file_name.lower().endswith((".docx", ".pdf", ".txt")):
        await update.message.reply_text("Поддерживаются форматы: .docx, .pdf, .txt")
        return
    if doc.file_size > 20 * 1024 * 1024:
        await update.message.reply_text("Файл слишком большой (лимит 20 МБ).")
        return

    await update.message.reply_text("Читаю документ...")
    os.makedirs("downloads", exist_ok=True)
    path = os.path.join("downloads", f"{update.effective_user.id}_{doc.file_name}")
    tg_file = await doc.get_file()
    await tg_file.download_to_drive(path)

    try:
        text = await asyncio.to_thread(extract_text, path)
    except Exception as e:
        await update.message.reply_text(f"Не удалось прочитать файл: {e}")
        return
    finally:
        try:
            os.remove(path)
        except OSError:
            pass

    if not text.strip():
        await update.message.reply_text(
            "Текст не извлечён. Возможно, это скан — нужен OCR."
        )
        return

    text = text[:MAX_DOC_CHARS]
    caption = (update.message.caption or "Проверь документ на соответствие "
                                          "законодательству РК.").strip()
    prompt = f'<документ имя="{doc.file_name}">\n{text}\n</документ>\n\n{caption}'

    history = get_history(context)
    history.append({"role": "user", "content": prompt})
    del history[:-MAX_HISTORY]

    await update.message.chat.send_action(ChatAction.TYPING)
    try:
        answer = await ask_ai(history)
    except Exception:
        logging.exception("Ошибка при проверке документа через AI")
        await update.message.reply_text(
            "Не удалось получить ответ от AI. Попробуйте повторить запрос позже."
        )
        return
    history.append({"role": "assistant", "content": answer})
    await send_long(update, answer)


def main():
    if not TELEGRAM_TOKEN or not AI_API_KEY:
        raise SystemExit("Задайте переменные окружения TELEGRAM_TOKEN и AI_API_KEY")

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    print("Бот запущен.")
    app.run_polling()


if __name__ == "__main__":
    main()