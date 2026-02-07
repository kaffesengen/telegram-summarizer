import os
import json
import asyncio
import logging
from datetime import datetime, timedelta
import pytz

# Biblioteker
import firebase_admin
from firebase_admin import credentials, firestore
import google.generativeai as genai
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from flask import Flask
import threading

# --- KONFIGURASJON ---
# Hent hemmeligheter fra Render Environment Variables
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
FIREBASE_CREDENTIALS_JSON = os.environ.get("FIREBASE_CREDENTIALS")

# Setup Logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# --- 1. FIREBASE SETUP ---
# Vi må laste inn JSON-nøkkelen fra en miljøvariabel fordi vi ikke kan legge filen på GitHub/Render åpent
cred_dict = json.loads(FIREBASE_CREDENTIALS_JSON)
cred = credentials.Certificate(cred_dict)
firebase_admin.initialize_app(cred)
db = firestore.client()

# --- 2. GEMINI AI SETUP ---
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

# --- 3. FLASK SETUP (For å holde Render happy) ---
app = Flask(__name__)

@app.route('/')
def health_check():
    return "Bot er i live!", 200

def run_flask():
    # Render krever at vi lytter på en port (default 10000 eller via PORT env var)
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

# --- 4. TELEGRAM LOGIKK ---

async def log_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lagrer innkommende meldinger i Firestore"""
    if update.message and update.message.text:
        user = update.message.from_user.first_name
        text = update.message.text
        chat_title = update.message.chat.title or "Privat"
        
        doc_data = {
            "timestamp": firestore.SERVER_TIMESTAMP,
            "sender": user,
            "content": text,
            "group": chat_title,
            "processed": False
        }
        
        # Lagre i en samling kalt 'incoming_messages'
        db.collection("incoming_messages").add(doc_data)
        logging.info(f"Lagret melding fra {user}")

async def send_daily_digest(context: ContextTypes.DEFAULT_TYPE):
    """Hovedjobben: Henter data, analyserer med Gemini, sender til brukere"""
    logging.info("Starter daglig oppsummering...")
    
    # 1. Hent meldinger fra siste 24 timer
    now = datetime.now(pytz.utc)
    yesterday = now - timedelta(hours=24)
    
    docs = db.collection("incoming_messages")\
        .where(filter=firestore.FieldFilter("timestamp", ">=", yesterday))\
        .stream()
    
    messages = []
    for doc in docs:
        d = doc.to_dict()
        # Konverter timestamp til string for AI
        time_str = d['timestamp'].strftime("%H:%M") if d.get('timestamp') else "?"
        messages.append(f"[{time_str}] {d.get('sender')}: {d.get('content')}")
    
    full_log = "\n".join(messages)
    
    if not full_log:
        logging.info("Ingen meldinger siste døgn.")
        return

    # 2. Hent brukere fra Firestore som har registrert seg
    # Vi antar at du har en samling 'users' der dokument-ID er UID fra Auth, 
    # men at feltet 'telegram_id' og 'keywords' ligger i dokumentet.
    users_ref = db.collection("users").stream()
    
    for user_doc in users_ref:
        user_data = user_doc.to_dict()
        telegram_id = user_data.get("telegram_id")
        keywords = user_data.get("keywords") # F.eks "Lyd, Lys, Video"
        
        if not telegram_id or not keywords:
            continue
            
        # 3. Send til Gemini
        prompt = f"""
        Du er en nyhetsagent. Her er chatloggen fra siste døgn:
        ---
        {full_log}
        ---
        Brukeren er KUN interessert i temaene: {keywords}.
        
        Oppgave:
        1. Identifiser om det er noe i loggen som matcher temaene.
        2. Hvis JA: Lag en kort, punktvis oppsummering på norsk.
        3. Hvis NEI: Svar kun med teksten "Intet nytt å melde om dine emner i dag."
        4. Ikke finn på ting som ikke står i loggen.
        """
        
        try:
            response = model.generate_content(prompt)
            ai_text = response.text
            
            # 4. Send svar til bruker på Telegram
            await context.bot.send_message(chat_id=telegram_id, text=f"📢 **Dagens Oppsummering**\n({keywords})\n\n{ai_text}")
            logging.info(f"Sendte rapport til {telegram_id}")
            
        except Exception as e:
            logging.error(f"Feil ved sending til {telegram_id}: {e}")

# --- 5. START APP ---

if __name__ == '__main__':
    # Start Flask i en egen tråd for å tilfredsstille Render
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.start()

    # Telegram Bot Setup
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    # Handler for meldinger
    msg_handler = MessageHandler(filters.TEXT & (~filters.COMMAND), log_message)
    application.add_handler(msg_handler)
    
    # Scheduler Setup (Cron job)
    scheduler = AsyncIOScheduler()
    # Kjøres hver dag kl 08:00 (Norsk tid)
    scheduler.add_job(send_daily_digest, 'cron', hour=8, minute=0, timezone='Europe/Oslo', args=[application])
    scheduler.start()
    
    print("Boten kjører...")
    application.run_polling()
  
