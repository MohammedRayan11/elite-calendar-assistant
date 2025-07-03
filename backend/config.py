import os
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
SERVICE_ACCOUNT_PATH = BASE_DIR / "service-accounts.json"

GOOGLE_SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]