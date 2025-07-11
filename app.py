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
from waitress import serve
from flask_cors import CORS # Import CORS

from config import config

# --- App & DB Initialization ---
app = Flask(__name__)
CORS(app) # Enable CORS for all routes
app.config.from_object(config)
db = SQLAlchemy(app)

# (The database models Article and FeedSource remain the same)
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
        """Serializes the object to a dictionary."""
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
        """Serializes the object to a dictionary."""
        return {
            'id': self.id,
            'key': self.key,
            'url': self.url
        }

# This function will now be called manually via an API route
def setup_database_logic():
    """Creates tables and seeds initial data."""
    try:
        db.create_all()
        if not FeedSource.query.first():
            print("No sources found. Seeding initial data...")
            INITIAL_FEEDS = {
                'TC-AI': 'https://techcrunch.com/category/artificial-intelligence/feed/',
                'testingcatalog': 'https://www.testingcatalog.com/rss/',
                '机器之心': 'https://wechat2rss.xlab.app/feed/51e92aad2728acdd1fda7314be32b16639353001.xml',
                '新智元': 'https://wechat2rss.xlab.app/feed/ede30346413ea70dbef5d485ea5cbb95cca446e7.xml',
                'Theverge': 'https://www.theverge.com/rss/index.xml',
            }
            for key, url in INITIAL_FEEDS.items():
                db.session.add(FeedSource(key=key, url=url))
            db.session.commit()
            return "Database tables created and initial sources seeded."
        else:
            return "Database tables already exist."
    except Exception as e:
        return f"An error occurred during database setup: {e}"


if hasattr(ssl, '_create_unverified_context'):
    ssl._create_default_https_context = ssl._create_unverified_context

def parse_publication_date_pipeline(entry):
    date_fields = ['published_parsed', 'updated_parsed']
    for field in date_fields:
        if hasattr(entry, field) and getattr(entry, field):
            return datetime(*getattr(entry, field)[:6], tzinfo=timezone.utc)
    date_text_fields = ['published', 'updated', 'created']
    for field in date_text_fields:
        if hasattr(entry, field):
            try:
                dt = parser.parse(getattr(entry, field))
                return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except (parser.ParserError, TypeError):
                continue
    return datetime.now(timezone.utc)

def extract_related_company_pipeline(text):
    companies = ['Google', 'OpenAI', 'Meta', 'Anthropic', 'XAI', 'Microsoft', 'Apple', 'Amazon', 'NVIDIA', 'Tesla']
    for company in companies:
        if f' {company.lower()} ' in f' {text.lower()} ': return company
    return None

def analyze_and_rewrite_with_gemini_pipeline(content, title):
    if not config.GEMINI_API_KEY: return True, content
    prompt = (
        "You are a news filter for both English and Chinese content."
        "First, determine if an article is strictly about Artificial Intelligence based on the following rules: "
        "1. The text explicitly contains the keyword 'AI' or '人工智能' or '大模型' in its title or body. "
        "2. The article's title or body mentioned specific AI technologies (like machine learning, LLMs, 大模型), AI products (like ChatGPT, Gemini, Sora), or major AI companies (like OpenAI, Google, Meta, Anthropic, NVIDIA). "
        "Second, if the article IS AI-related, read the entire text and write a thorough, high-quality summary that captures the main content. If it is NOT AI-related, the summary should be an empty string. "
        'Respond ONLY with a JSON object like {"is_ai_related": <true_or_false>, "rewritten_content": "<A professional rewrite of the article if it is AI-related, otherwise an empty string>"}.'
        "If not AI-related, rewritten_content should be an empty string. "
        f'Article Title: "{title}". Article Content: {content}'
    )
    try:
        response = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={config.GEMINI_API_KEY}",
            headers={"Content-Type": "application/json"}, json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=90)
        response.raise_for_status()
        data = response.json()['candidates'][0]['content']['parts'][0]['text']
        parsed_json = json.loads(data.strip().lstrip('```json').rstrip('```'))
        is_relevant = parsed_json.get('is_ai_related', False)
        return is_relevant, parsed_json.get('rewritten_content', '') if is_relevant else ''
    except Exception as e:
        print(f"  - WARNING: Gemini API error: {e}. Falling back to original content.")
        return True, content

def run_website_pipeline(url: str, source_key: str, source_name: str):
    # This function remains the same
    pass

def run_pipeline(source_ids=None):
    # This function remains the same
    pass

# --- API ROUTES ---

# NEW: Manual database setup route
@app.route('/api/setup-database', methods=['GET'])
def setup_database_route():
    """Manually triggers the creation of database tables and seeding of initial data."""
    with app.app_context():
        message = setup_database_logic()
    return jsonify({'status': 'completed', 'message': message})


@app.route('/api/articles', methods=['GET'])
def get_articles():
    """Returns a list of all articles."""
    articles = Article.query.order_by(Article.published_at.desc()).all()
    return jsonify([article.to_dict() for article in articles])

@app.route('/api/articles/<int:article_id>', methods=['DELETE'])
def delete_article(article_id):
    """Deletes an article."""
    article = db.get_or_404(Article, article_id)
    db.session.delete(article)
    db.session.commit()
    return jsonify({'success': True, 'message': 'Article deleted'})

@app.route('/api/sources', methods=['GET'])
def get_sources():
    """Returns a list of all feed sources."""
    sources = FeedSource.query.order_by(FeedSource.key).all()
    return jsonify([source.to_dict() for source in sources])

@app.route('/api/sources', methods=['POST'])
def add_source():
    """Adds a new feed source."""
    data = request.get_json()
    if not data or not data.get('key') or not data.get('url'):
        return jsonify({'success': False, 'message': 'Missing key or url'}), 400
    
    if FeedSource.query.filter((FeedSource.key == data['key']) | (FeedSource.url == data['url'])).first():
        return jsonify({'success': False, 'message': 'Source key or URL already exists'}), 409

    new_source = FeedSource(key=data['key'], url=data['url'])
    db.session.add(new_source)
    db.session.commit()
    return jsonify(new_source.to_dict()), 201

@app.route('/api/sources/<int:source_id>', methods=['DELETE'])
def remove_source(source_id):
    """Removes a feed source."""
    source = db.get_or_404(FeedSource, source_id)
    db.session.delete(source)
    db.session.commit()
    return jsonify({'success': True, 'message': 'Source removed'})

@app.route('/api/fetch-news', methods=['GET', 'POST'])
def fetch_news_route():
    """Triggers the background news fetching pipeline."""
    def run_background_fetch():
        with app.app_context():
             run_pipeline()

    thread = threading.Thread(target=run_background_fetch)
    thread.start()
    return jsonify({'success': True, 'message': 'News fetching process started.'})

# --- Main Execution Block ---
if __name__ == '__main__':
    # We no longer call setup_database() here, it will be triggered manually.
    print("Starting API server on http://0.0.0.0:5001")
    serve(app, host='0.0.0.0', port=5001)
