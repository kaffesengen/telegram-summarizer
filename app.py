import os
import json
import logging
from datetime import datetime, timedelta
import pytz
import threading

# Biblioteker
import firebase_admin
from firebase_admin import credentials, firestore
import google.generativeai as genai
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from flask import Flask

# --- KONFIGURASJON ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
FIREBASE_CREDENTIALS_JSON = os.environ.get("FIREBASE_CREDENTIALS")

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- 1. FIREBASE SETUP ---
cred_dict = json.loads(FIREBASE_CREDENTIALS_JSON)
cred = credentials.Certificate(cred_dict)
firebase_admin.initialize_app(cred)
db = firestore.client()

# --- 2. GEMINI AI SETUP ---
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

# --- 3. FLASK (Keep-alive) ---
app = Flask(__name__)
@app.route('/')
def health_check(): return "Bot er i live og sjekker timeplaner!", 200
def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

# --- 4. HJELPEFUNKSJONER ---

def should_run_now(schedule_config):
    """Sjekker om en gitt tidsplan skal kjøres akkurat nå (denne timen)."""
    if not schedule_config: return False
    
    # Standard: Daglig kl 08:00 hvis ingenting er valgt
    freq = schedule_config.get('frequency', 'daily')
    target_time = schedule_config.get('time', '08:00')
    target_day = schedule_config.get('day', 'monday') # monday, tuesday...
    
    # Norsk tid
    now = datetime.now(pytz.timezone('Europe/Oslo'))
    current_time_str = now.strftime("%H:00") # Vi sjekker kun hele timer
    current_day_name = now.strftime("%A").lower()
    
    # Sjekk klokkeslett (Time)
    # Vi sjekker kun om timen matcher (f.eks "08:00" matcher alt mellom 08:00 og 08:59)
    if target_time.split(':')[0] != current_time_str.split(':')[0]:
        return False
        
    # Sjekk Frekvens
    if freq == 'daily':
        return True
    elif freq == 'weekly':
        return current_day_name == target_day.lower()
    elif freq == 'monthly':
        return now.day == 1 # Kjører 1. i hver måned
        
    return False

# --- 5. BOT LOGIKK ---

async def log_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lagrer melding OG oppdaterer listen over kjente grupper."""
    if update.message and update.message.text:
        user = update.message.from_user.first_name
        text = update.message.text
        chat = update.message.chat
        chat_title = chat.title or "Privat"
        chat_id = str(chat.id)
        
        # 1. Lagre meldingen
        doc_data = {
            "timestamp": firestore.SERVER_TIMESTAMP,
            "sender": user,
            "content": text,
            "group_id": chat_id,
            "group_name": chat_title
        }
        db.collection("incoming_messages").add(doc_data)
        
        # 2. Oppdater listen over kjente grupper (overskriv for å oppdatere navn)
        db.collection("known_groups").document(chat_id).set({
            "name": chat_title,
            "last_active": firestore.SERVER_TIMESTAMP
        }, merge=True)
        
        logger.info(f"Logget: {text[:20]}... i {chat_title}")

async def run_scheduler_job(context: ContextTypes.DEFAULT_TYPE):
    """Kjører hver time og sjekker om noen skal ha post."""
    logger.info("🕒 Sjekker timeplaner...")
    
    users_ref = db.collection("users").stream()
    
    # Hent alle meldinger fra siste 32 dager (for å dekke månedlig), 
    # men vi filtrerer strengere i minnet.
    now_utc = datetime.now(pytz.utc)
    
    for user_doc in users_ref:
        data = user_doc.to_dict()
        user_id = user_doc.id
        telegram_id = data.get("telegram_id")
        keywords = data.get("keywords")
        global_schedule = data.get("schedule", {}) # {freq: 'daily', time: '08:00'}
        group_overrides = data.get("group_schedules", {}) # {group_id: {freq...}}
        
        if not telegram_id or not keywords: continue

        # Hent alle kjente grupper for å iterere gjennom dem
        known_groups = db.collection("known_groups").stream()
        
        messages_to_report = []
        
        for group in known_groups:
            group_id = group.id
            group_name = group.to_dict().get('name', 'Ukjent gruppe')
            
            # Bestem hvilken timeplan som gjelder for denne gruppen
            # Hvis bruker har spesifikt valg for gruppen, bruk det. Ellers bruk global.
            active_schedule = group_overrides.get(group_id, global_schedule)
            
            if should_run_now(active_schedule):
                logger.info(f"MATCH: Skal lage rapport for {user_id} fra gruppe {group_name}")
                
                # Bestem tidsvindu basert på frekvens
                freq = active_schedule.get('frequency', 'daily')
                if freq == 'daily': delta = timedelta(hours=24)
                elif freq == 'weekly': delta = timedelta(days=7)
                elif freq == 'monthly': delta = timedelta(days=32)
                else: delta = timedelta(hours=24)
                
                start_time = now_utc - delta
                
                # Hent meldinger for denne gruppen
                # Merk: I en større app ville vi gjort dette med en smartere query
                msgs = db.collection("incoming_messages")\
                    .where(filter=firestore.FieldFilter("group_id", "==", group_id))\
                    .where(filter=firestore.FieldFilter("timestamp", ">=", start_time))\
                    .stream()
                
                group_msgs = []
                for m in msgs:
                    d = m.to_dict()
                    ts = d['timestamp'].strftime("%d/%m %H:%M") if d.get('timestamp') else ""
                    group_msgs.append(f"[{ts}] {d['sender']}: {d['content']}")
                
                if group_msgs:
                    messages_to_report.append(f"\n--- {group_name} ---\n" + "\n".join(group_msgs))

        # Hvis vi fant meldinger i noen av gruppene som skulle rapporteres NÅ
        if messages_to_report:
            full_log = "\n".join(messages_to_report)
            
            prompt = f"""
            Du er en assistent. Analyser følgende chatlogger fra ulike grupper.
            Brukerens interessefelt: {keywords}.
            
            Oppgave:
            1. Filtrer hardt. Ignorer alt som ikke treffer interessene.
            2. Lag en oppsummering per gruppe.
            3. Hvis en gruppe ikke har noe relevant, ignorer den i svaret.
            4. Hvis INGEN grupper har noe relevant, svar KUN: "Ingen relevante nyheter i dine rapporteringsgrupper."
            
            Logger:
            {full_log}
            """
            
            try:
                response = model.generate_content(prompt)
                await context.bot.send_message(chat_id=telegram_id, text=f"📢 **Dine oppdateringer**\n\n{response.text}")
            except Exception as e:
                logger.error(f"Feil mot Gemini: {e}")

# --- 6. START ---
if __name__ == '__main__':
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.start()

    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), log_message))
    
    scheduler = AsyncIOScheduler()
    # Kjører jobben hvert 60. minutt (på hel timen)
    scheduler.add_job(run_scheduler_job, 'cron', minute=0)
    scheduler.start()
    
    application.run_polling()
@app.route('/')
def health_check(): return "Bot er i live og sjekker timeplaner!", 200
def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

# --- 4. HJELPEFUNKSJONER ---

def should_run_now(schedule_config):
    """Sjekker om en gitt tidsplan skal kjøres akkurat nå (denne timen)."""
    if not schedule_config: return False
    
    # Standard: Daglig kl 08:00 hvis ingenting er valgt
    freq = schedule_config.get('frequency', 'daily')
    target_time = schedule_config.get('time', '08:00')
    target_day = schedule_config.get('day', 'monday') # monday, tuesday...
    
    # Norsk tid
    now = datetime.now(pytz.timezone('Europe/Oslo'))
    current_time_str = now.strftime("%H:00") # Vi sjekker kun hele timer
    current_day_name = now.strftime("%A").lower()
    
    # Sjekk klokkeslett (Time)
    # Vi sjekker kun om timen matcher (f.eks "08:00" matcher alt mellom 08:00 og 08:59)
    if target_time.split(':')[0] != current_time_str.split(':')[0]:
        return False
        
    # Sjekk Frekvens
    if freq == 'daily':
        return True
    elif freq == 'weekly':
        return current_day_name == target_day.lower()
    elif freq == 'monthly':
        return now.day == 1 # Kjører 1. i hver måned
        
    return False

# --- 5. BOT LOGIKK ---

async def log_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lagrer melding OG oppdaterer listen over kjente grupper."""
    if update.message and update.message.text:
        user = update.message.from_user.first_name
        text = update.message.text
        chat = update.message.chat
        chat_title = chat.title or "Privat"
        chat_id = str(chat.id)
        
        # 1. Lagre meldingen
        doc_data = {
            "timestamp": firestore.SERVER_TIMESTAMP,
            "sender": user,
            "content": text,
            "group_id": chat_id,
            "group_name": chat_title
        }
        db.collection("incoming_messages").add(doc_data)
        
        # 2. Oppdater listen over kjente grupper (overskriv for å oppdatere navn)
        db.collection("known_groups").document(chat_id).set({
            "name": chat_title,
            "last_active": firestore.SERVER_TIMESTAMP
        }, merge=True)
        
        logger.info(f"Logget: {text[:20]}... i {chat_title}")

async def run_scheduler_job(context: ContextTypes.DEFAULT_TYPE):
    """Kjører hver time og sjekker om noen skal ha post."""
    logger.info("🕒 Sjekker timeplaner...")
    
    users_ref = db.collection("users").stream()
    
    # Hent alle meldinger fra siste 32 dager (for å dekke månedlig), 
    # men vi filtrerer strengere i minnet.
    now_utc = datetime.now(pytz.utc)
    
    for user_doc in users_ref:
        data = user_doc.to_dict()
        user_id = user_doc.id
        telegram_id = data.get("telegram_id")
        keywords = data.get("keywords")
        global_schedule = data.get("schedule", {}) # {freq: 'daily', time: '08:00'}
        group_overrides = data.get("group_schedules", {}) # {group_id: {freq...}}
        
        if not telegram_id or not keywords: continue

        # Hent alle kjente grupper for å iterere gjennom dem
        known_groups = db.collection("known_groups").stream()
        
        messages_to_report = []
        
        for group in known_groups:
            group_id = group.id
            group_name = group.to_dict().get('name', 'Ukjent gruppe')
            
            # Bestem hvilken timeplan som gjelder for denne gruppen
            # Hvis bruker har spesifikt valg for gruppen, bruk det. Ellers bruk global.
            active_schedule = group_overrides.get(group_id, global_schedule)
            
            if should_run_now(active_schedule):
                logger.info(f"MATCH: Skal lage rapport for {user_id} fra gruppe {group_name}")
                
                # Bestem tidsvindu basert på frekvens
                freq = active_schedule.get('frequency', 'daily')
                if freq == 'daily': delta = timedelta(hours=24)
                elif freq == 'weekly': delta = timedelta(days=7)
                elif freq == 'monthly': delta = timedelta(days=32)
                else: delta = timedelta(hours=24)
                
                start_time = now_utc - delta
                
                # Hent meldinger for denne gruppen
                # Merk: I en større app ville vi gjort dette med en smartere query
                msgs = db.collection("incoming_messages")\
                    .where(filter=firestore.FieldFilter("group_id", "==", group_id))\
                    .where(filter=firestore.FieldFilter("timestamp", ">=", start_time))\
                    .stream()
                
                group_msgs = []
                for m in msgs:
                    d = m.to_dict()
                    ts = d['timestamp'].strftime("%d/%m %H:%M") if d.get('timestamp') else ""
                    group_msgs.append(f"[{ts}] {d['sender']}: {d['content']}")
                
                if group_msgs:
                    messages_to_report.append(f"\n--- {group_name} ---\n" + "\n".join(group_msgs))

        # Hvis vi fant meldinger i noen av gruppene som skulle rapporteres NÅ
        if messages_to_report:
            full_log = "\n".join(messages_to_report)
            
            prompt = f"""
            Du er en assistent. Analyser følgende chatlogger fra ulike grupper.
            Brukerens interessefelt: {keywords}.
            
            Oppgave:
            1. Filtrer hardt. Ignorer alt som ikke treffer interessene.
            2. Lag en oppsummering per gruppe.
            3. Hvis en gruppe ikke har noe relevant, ignorer den i svaret.
            4. Hvis INGEN grupper har noe relevant, svar KUN: "Ingen relevante nyheter i dine rapporteringsgrupper."
            
            Logger:
            {full_log}
            """
            
            try:
                response = model.generate_content(prompt)
                await context.bot.send_message(chat_id=telegram_id, text=f"📢 **Dine oppdateringer**\n\n{response.text}")
            except Exception as e:
                logger.error(f"Feil mot Gemini: {e}")

# --- 6. START ---
if __name__ == '__main__':
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.start()

    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), log_message))
    
    scheduler = AsyncIOScheduler()
    # Kjører jobben hvert 60. minutt (på hel timen)
    scheduler.add_job(run_scheduler_job, 'cron', minute=0)
    scheduler.start()
    
    application.run_polling()
