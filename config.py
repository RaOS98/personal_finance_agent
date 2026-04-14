import os
from dotenv import load_dotenv

load_dotenv()

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0"))

# Database
DATABASE_URL = os.getenv("DATABASE_URL")

# Ollama
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gemma4:31b")

# Image storage
IMAGE_STORAGE_DIR = os.path.join(os.path.dirname(__file__), "storage", "images")
os.makedirs(IMAGE_STORAGE_DIR, exist_ok=True)

# Reconciliation
RECONCILIATION_DATE_TOLERANCE_DAYS = 5
