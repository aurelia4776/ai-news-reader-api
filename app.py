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
    # This function is intentionally left brief as it's a fallback.
    # You can expand its logic as needed.
    print(f"Running website pipeline for {url}")

def run_pipeline(source_ids=None):
    with app.app_context():
        print("PIPELINE: Starting news processing...")
        source_name_map = {'TC': 'TechCrunch', 'Wired': 'Wired', 'AIbase': 'AIbase'}
        feeds_query = FeedSource.query
        if source_ids:
            print(f"PIPELINE: Fetching from selected source IDs: {source_ids}")
            feeds_query = feeds_query.filter(FeedSource.id.in_(source_ids))
        else:
            print("PIPELINE: No specific sources selected, fetching from all.")
        
        feeds = feeds_query.all()
        for feed_source in feeds:
            print(f"\nPIPELINE: --- Processing source: {feed_source.key} ({feed_source.url}) ---")
            source_name = source_name_map.get(feed_source.key.split('-', 1)[0], feed_source.key.split('-', 1)[0])
            feed = feedparser.parse(feed_source.url, agent='Mozilla/5.0')

            if feed.bozo or not feed.entries:
                print("PIPELINE: Feed invalid or empty, attempting website scrape.")
                run_website_pipeline(feed_source.url, feed_source.key, source_name)
                continue

            for entry in feed.entries:
                try:
                    if (datetime.now(timezone.utc) - parse_publication_date_pipeline(entry)).days >= 3:
                        continue
                    
                    if Article.query.filter_by(title=entry.title).first():
                        print(f"PIPELINE:   - Skipped duplicate: '{entry.title}'")
                        continue

                    print(f"PIPELINE: Processing: '{entry.title}'")
                    time.sleep(1) # To avoid being rate-limited
                    original_content = BeautifulSoup(getattr(entry, 'summary', ''), 'html.parser').get_text(separator='\n', strip=True)
                    is_relevant, final_content = analyze_and_rewrite_with_gemini_pipeline(original_content, entry.title)
                    
                    if not is_relevant or not final_content.strip():
                        print("PIPELINE:   - Skipped (not AI-related or no content).")
                        continue

                    new_article = Article(
                        title=entry.title,
                        content=final_content,
                        original_url=entry.link,
                        category=feed_source.key.split('-', 1)[1] if '-' in feed_source.key else 'General',
                        published_at=parse_publication_date_pipeline(entry),
                        source=source_name,
                        related_company=extract_related_company_pipeline(entry.title + " " + original_content)
                    )
                    db.session.add(new_article)
                    db.session.commit()
                    print("PIPELINE:   + Saved to database.")
                except IntegrityError:
                    db.session.rollback()
                    print(f"PIPELINE:   - DB IntegrityError (likely duplicate): '{entry.title}'")
                except Exception as e:
                    db.session.rollback()
                    print(f"PIPELINE:   - An unexpected error occurred for entry '{entry.title}': {e}")
        print("\nPIPELINE: Finished.")


# --- API ROUTES ---
@app.route('/api/setup-database', methods=['GET'])
def setup_database_route():
    with app.app_context():
        message = setup_database_logic()
    return jsonify({'status': 'completed', 'message': message})

@app.route('/api/articles', methods=['GET'])
def get_articles():
    query = Article.query
    company_filter = request.args.get('company')
    start_date_str = request.args.get('start_date')
    end_date_str = request.args.get('end_date')
    show_today = request.args.get('today')

    primary_companies = ['Google', 'OpenAI', 'Meta', 'Anthropic', 'XAI', 'Microsoft', 'Apple', 'Amazon', 'NVIDIA', 'Tesla']

    if company_filter and company_filter != 'all':
        if company_filter == 'Others':
            query = query.filter(Article.related_company.notin_(primary_companies))
        else:
            query = query.filter(Article.related_company == company_filter)
    
    if start_date_str:
        try:
            start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
            query = query.filter(Article.published_at >= start_date)
        except ValueError: pass
    
    if end_date_str:
        try:
            end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
            query = query.filter(Article.published_at < end_date + timedelta(days=1))
        except ValueError: pass

    if show_today == 'true':
        today_start = datetime.combine(date.today(), datetime.min.time())
        today_end = datetime.combine(date.today(), datetime.max.time())
        query = query.filter(Article.published_at.between(today_start, today_end))

    articles = query.order_by(Article.published_at.desc()).all()
    return jsonify([article.to_dict() for article in articles])

@app.route('/api/sources', methods=['GET'])
def get_sources():
    sources = FeedSource.query.order_by(FeedSource.key).all()
    return jsonify([source.to_dict() for source in sources])

@app.route('/api/sources', methods=['POST'])
def add_source():
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
    source = db.get_or_404(FeedSource, source_id)
    db.session.delete(source)
    db.session.commit()
    return jsonify({'success': True, 'message': 'Source removed'})

@app.route('/api/fetch-news', methods=['POST'])
def fetch_news_route():
    data = request.get_json()
    source_ids = data.get('source_ids') if data else None
    thread = threading.Thread(target=run_pipeline, args=(source_ids,))
    thread.start()
    message = f'News fetching started for {len(source_ids)} selected sources.' if source_ids else 'News fetching started for all sources.'
    return jsonify({'success': True, 'message': message})

if __name__ == '__main__':
    with app.app_context():
        db.create_all() # Ensure tables exist on startup
    print("Starting API server on http://0.0.0.0:5001")
    serve(app, host='0.0.0.0', port=5001)
