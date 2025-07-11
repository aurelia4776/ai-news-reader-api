import os
import ssl
import threading
import time
import json

from datetime import datetime, date, timezone

# Third-party libraries
import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import parser
from flask import Flask, jsonify, request
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.exc import IntegrityError
from sqlalchemy import or_
from waitress import serve
from flask_cors import CORS

from config import config

# --- App & DB Initialization ---
app = Flask(__name__)
CORS(app)
app.config.from_object(config)
db = SQLAlchemy(app)

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

# --- Pipeline & Setup ---
def setup_database_logic():
    try:
        db.create_all()
        if not FeedSource.query.first():
            print("Seeding initial sources...")
            INITIAL_FEEDS = {
                'TC-AI': 'https://techcrunch.com/category/artificial-intelligence/feed/',
                'Theverge': 'https://www.theverge.com/rss/index.xml',
            }
            for key, url in INITIAL_FEEDS.items():
                db.session.add(FeedSource(key=key, url=url))
            db.session.commit()
            return "Database tables created and initial sources seeded."
        return "Database tables already exist."
    except Exception as e:
        return f"An error occurred: {e}"

# (All pipeline functions like run_pipeline, analyze_and_rewrite_with_gemini_pipeline, etc. remain the same as before)
# ... [omitting for brevity, but they should be here] ...

# --- API ROUTES ---
@app.route('/api/setup-database', methods=['GET'])
def setup_database_route():
    with app.app_context():
        message = setup_database_logic()
    return jsonify({'status': 'completed', 'message': message})

@app.route('/api/articles', methods=['GET'])
def get_articles():
    """Returns a list of articles, with optional filtering."""
    query = Article.query

    # Get filters from request arguments
    company_filter = request.args.get('company')
    source_filter = request.args.get('source')
    show_today = request.args.get('today')

    primary_companies = ['Google', 'OpenAI', 'Meta', 'Anthropic', 'XAI', 'Microsoft', 'Apple', 'Amazon', 'NVIDIA', 'Tesla']

    if company_filter:
        if company_filter == 'Others':
            query = query.filter(or_(Article.related_company.is_(None), Article.related_company.notin_(primary_companies)))
        else:
            query = query.filter(Article.related_company == company_filter)
    
    if source_filter:
        query = query.filter(Article.source == source_filter)

    if show_today == 'true':
        today_start = datetime.combine(date.today(), datetime.min.time())
        today_end = datetime.combine(date.today(), datetime.max.time())
        query = query.filter(Article.published_at.between(today_start, today_end))

    articles = query.order_by(Article.published_at.desc()).all()
    return jsonify([article.to_dict() for article in articles])

# (All other routes like /api/sources, /api/fetch-news, etc. remain the same)
# ... [omitting for brevity] ...

if __name__ == '__main__':
    print("Starting API server on http://0.0.0.0:5001")
    serve(app, host='0.0.0.0', port=5001)
