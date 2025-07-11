import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()


class Config:
    """Configuration settings for the application."""
    # General Config
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'a-very-secret-key'

    # Database Config
    SQLALCHEMY_DATABASE_URI = 'sqlite:///news.db'
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # API Keys & Credentials
    GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')

    # RSSHub URL (New)
    RSSHUB_URL = os.getenv('RSSHUB_URL', 'https://rsshub.app')

    # Hashing Log
    HASH_LOG_FILE = 'hash-logs.txt'


# Instantiate config
config = Config()