import os
import ssl
import threading
import time
import json

from datetime import datetime, date, timezone, timedelta

# Third-party libraries
import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import parser
from flask import Flask, jsonify, request
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.exc import IntegrityError
from sqlalchemy import or_
from sqlalchemy_utils import database_exists, create_database
from waitress import serve
from flask_cors import CORS

from config import config

# --- App & DB Initialization ---
app = Flask(__name__)
CORS(app)

# --- CORRECTED DATABASE CONFIGURATION FOR RENDER ---
# Render provides the database URL as an environment variable.
# This code checks for it, ensuring it works on Render.
# If it's not found, it falls back to a local sqlite file for testing.
db_url = os.environ.get('DATABASE_URL')
if db_url and db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)
app.config['SQLALCHEMY_DATABASE_URI'] = db_url or 'sqlite:///news.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# --- Global variables for background task status ---
FETCH_STATUS = "idle" # Possible values: "idle", "running", "completed", "error"
fetch_lock = threading.Lock()

# --- Database Models ---
class Article(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False, unique=True)
    content = db.Column(db.Text, nullable=False)
    original_url = db.Column(db.String(500), nullable=True)
    category = db.Column(db.String(100), nullable=False)
    published_at = db.Column(db.DateTime, nullable=False)
    source = db.Column(db.String(100), nullable=False)
    related_company = db.Column(db.String(100), nullable=True)

    def to_dict(self):
        return {
            'id': self.id,
            'title': self.title,
            'content': self.content,
            'original_url': self.original_url,
            'category': self.category,
            'published_at': self.published_at.isoformat(),
            'source': self.source,
            'related_company': self.related_company
        }

class FeedSource(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(100), unique=True, nullable=False)
    url = db.Column(db.String(500), unique=True, nullable=False)
    
    def to_dict(self):
        return { 'id': self.id, 'key': self.key, 'url': self.url }

# --- CORRECTED DATABASE INITIALIZATION ---
with app.app_context():
    # This ensures the database exists before creating tables.
    if not database_exists(app.config['SQLALCHEMY_DATABASE_URI']):
        create_database(app.config['SQLALCHEMY_DATABASE_URI'])
    
    db.create_all()
    # Seed initial sources if the table is empty
    if not FeedSource.query.first():
        print("Database is empty. Seeding initial sources...")
        INITIAL_FEEDS = {
            'TC-AI': 'https://techcrunch.com/category/artificial-intelligence/feed/',
            'AIbase': 'https://rsshub.app/aibase/news',
            'Theverge': 'https://www.theverge.com/rss/index.xml',
        }
        for key, url in INITIAL_FEEDS.items():
            db.session.add(FeedSource(key=key, url=url))
        db.session.commit()
        print("Database seeded successfully.")

# --- Pipeline & Setup ---
if hasattr(ssl, '_create_unverified_context'):
    ssl._create_default_https_context = ssl._create_unverified_context

def parse_publication_date_pipeline(entry):
    # ... (function logic remains the same)
    pass

def extract_related_company_pipeline(text):
    # ... (function logic remains the same)
    pass

def analyze_and_rewrite_with_gemini_pipeline(content, title):
    # ... (function logic remains the same)
    pass

def run_website_pipeline(url: str, source_key: str, source_name: str):
    # ... (function logic remains the same)
    pass

def run_pipeline(source_ids=None):
    # ... (function logic remains the same)
    pass

# --- API ROUTES ---
@app.route('/api/articles', methods=['GET'])
def get_articles():
    # ... (function logic remains the same)
    pass

@app.route('/api/sources', methods=['GET'])
def get_sources():
    # ... (function logic remains the same)
    pass

@app.route('/api/sources', methods=['POST'])
def add_source():
    # ... (function logic remains the same)
    pass

@app.route('/api/sources/<int:source_id>', methods=['DELETE'])
def remove_source(source_id):
    # ... (function logic remains the same)
    pass

@app.route('/api/fetch-news', methods=['POST'])
def fetch_news_route():
    # ... (function logic remains the same)
    pass

@app.route('/api/fetch-status', methods=['GET'])
def fetch_status():
    # ... (function logic remains the same)
    pass

if __name__ == '__main__':
    print("Starting API server on http://0.0.0.0:5001")
    serve(app, host='0.0.0.0', port=5001)
