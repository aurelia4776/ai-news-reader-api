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

# --- STABILIZED DATABASE CONFIGURATION ---
# This is the most critical fix. Render provides the database URL as an environment variable.
# We will now be much stricter about its use to prevent data loss.
db_url = os.environ.get('DATABASE_URL')
if db_url and db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

# If DATABASE_URL is not set, we fall back to a local file, but print a loud warning.
app.config['SQLALCHEMY_DATABASE_URI'] = db_url or 'sqlite:///news.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# --- Global variables for background task status ---
FETCH_STATUS = "idle" # Possible values: "idle", "running", "completed", "error"
FETCH_LOG = [] # Store logs to potentially show on the frontend later
fetch_lock = threading.Lock()

# --- Database Models ---
class Article(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(300), nullable=False, unique=True) # Increased length
    content = db.Column(db.Text, nullable=False)
    original_url = db.Column(db.String(500), nullable=True, unique=True) # Added unique constraint
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

# --- Database Initialization ---
with app.app_context():
    # This ensures the database exists before creating tables.
    if not database_exists(app.config['SQLALCHEMY_DATABASE_URI']):
        print(f"Database not found at {app.config['SQLALCHEMY_DATABASE_URI']}, creating new one.")
        create_database(app.config['SQLALCHEMY_DATABASE_URI'])
    
    # Loudly announce which database we are connected to
    if 'sqlite' in app.config['SQLALCHEMY_DATABASE_URI']:
        print("\n--- WARNING: Using temporary SQLite database. Data will be lost on server restart. ---")
        print("--- For persistent data, set the DATABASE_URL environment variable. ---\n")
    else:
        print(f"\n--- Successfully connected to PostgreSQL database. ---\n")

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

def log_message(message):
    """Helper function to print and log messages."""
    print(message)
    FETCH_LOG.append(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")

def parse_publication_date_pipeline(entry):
    """Robustly parses publication date from a feed entry."""
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
    return datetime.now(timezone.utc) # Fallback to now

def extract_related_company_pipeline(text):
    """Identifies a primary company mentioned in the text."""
    companies = ['Google', 'OpenAI', 'Meta', 'Anthropic', 'XAI', 'Microsoft', 'Apple', 'Amazon', 'NVIDIA', 'Tesla']
    # Check for whole words to avoid matching substrings (e.g., 'Snapple' for 'Apple')
    for company in companies:
        if f' {company.lower()} ' in f' {text.lower()} ': return company
    return None

def analyze_and_rewrite_with_gemini_pipeline(content, title):
    """Uses Gemini to validate AI relevance and summarize content."""
    if not config.GEMINI_API_KEY: 
        log_message("  - WARNING: GEMINI_API_KEY not set. Skipping analysis.")
        return True, content
    
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
        
        # Robust JSON parsing
        raw_text = response.json()['candidates'][0]['content']['parts'][0]['text']
        try:
            # Clean potential markdown formatting
            clean_text = raw_text.strip().lstrip('```json').rstrip('```').strip()
            parsed_json = json.loads(clean_text)
        except json.JSONDecodeError:
            log_message(f"  - WARNING: Gemini returned non-JSON response: {raw_text}")
            return True, content # Fallback: assume it's relevant

        is_relevant = parsed_json.get('is_ai_related', False)
        rewritten_content = parsed_json.get('rewritten_content', '') if is_relevant else ''
        return is_relevant, rewritten_content

    except requests.RequestException as e:
        log_message(f"  - WARNING: Gemini API request failed: {e}. Falling back to original content.")
    except Exception as e:
        log_message(f"  - WARNING: An unexpected error occurred during Gemini analysis: {e}. Falling back to original content.")
    
    return True, content # Fallback for any error

def run_pipeline(source_ids=None):
    """The main news fetching and processing pipeline."""
    global FETCH_STATUS
    with app.app_context():
        log_message("PIPELINE: Starting news processing...")
        source_name_map = {'TC': 'TechCrunch', 'Wired': 'Wired', 'AIbase': 'AIbase'}
        
        feeds_query = FeedSource.query
        if source_ids:
            log_message(f"PIPELINE: Fetching from selected source IDs: {source_ids}")
            feeds_query = feeds_query.filter(FeedSource.id.in_(source_ids))
        else:
            log_message("PIPELINE: No specific sources selected, fetching from all.")
        
        feeds = feeds_query.all()
        if not feeds:
            log_message("PIPELINE: No sources found to process.")

        for feed_source in feeds:
            log_message(f"\nPIPELINE: --- Processing source: {feed_source.key} ({feed_source.url}) ---")
            
            try:
                feed = feedparser.parse(feed_source.url, agent='Mozilla/5.0 (compatible; AINewsReader/1.0)')
                if feed.bozo:
                    log_message(f"PIPELINE: WARNING - Feed may be malformed for {feed_source.key}. Bozo reason: {feed.bozo_exception}")
                if not feed.entries:
                    log_message(f"PIPELINE: Feed is empty for {feed_source.key}. Skipping.")
                    continue
            except Exception as e:
                log_message(f"PIPELINE: CRITICAL - Failed to parse feed for {feed_source.key}. Error: {e}")
                continue # Skip to the next source

            for entry in feed.entries:
                try:
                    pub_date = parse_publication_date_pipeline(entry)
                    if (datetime.now(timezone.utc) - pub_date).days >= 3:
                        continue # Skip old articles silently

                    # Check for duplicates by URL (more reliable than title)
                    if entry.link and Article.query.filter_by(original_url=entry.link).first():
                        continue

                    log_message(f"PIPELINE: Processing: '{entry.title}'")
                    time.sleep(1) # Be polite to servers

                    original_content = BeautifulSoup(getattr(entry, 'summary', ''), 'html.parser').get_text(separator='\n', strip=True)
                    is_relevant, final_content = analyze_and_rewrite_with_gemini_pipeline(original_content, entry.title)
                    
                    if not is_relevant or not final_content.strip():
                        log_message("PIPELINE:   - Skipped (not AI-related or empty summary).")
                        continue

                    new_article = Article(
                        title=entry.title[:300], # Truncate to fit model
                        content=final_content,
                        original_url=entry.link,
                        category=feed_source.key.split('-', 1)[1] if '-' in feed_source.key else 'General',
                        published_at=pub_date,
                        source=source_name_map.get(feed_source.key.split('-', 1)[0], feed_source.key.split('-', 1)[0]),
                        related_company=extract_related_company_pipeline(entry.title + " " + original_content)
                    )
                    db.session.add(new_article)
                    db.session.commit()
                    log_message("PIPELINE:   + Saved to database.")

                except IntegrityError:
                    db.session.rollback()
                    # This is now an expected way to catch duplicates, so no scary log message needed.
                except Exception as e:
                    db.session.rollback()
                    log_message(f"PIPELINE:   - An unexpected error occurred for entry '{entry.title}': {e}")
        
        log_message("\nPIPELINE: Finished.")
        FETCH_STATUS = "completed"

# --- API ROUTES ---
@app.route('/api/articles', methods=['GET'])
def get_articles():
    query = Article.query
    source_filter = request.args.get('source')
    company_filter = request.args.get('company')
    start_date_str = request.args.get('start_date')
    end_date_str = request.args.get('end_date')
    show_today = request.args.get('today')

    primary_companies = ['Google', 'OpenAI', 'Meta', 'Anthropic', 'XAI', 'Microsoft', 'Apple', 'Amazon', 'NVIDIA', 'Tesla']

    if source_filter and source_filter != 'All Sources':
        query = query.filter(Article.source == source_filter)
    
    if company_filter and company_filter.lower() != 'all':
        if company_filter == 'Others':
            query = query.filter(Article.related_company.notin_(primary_companies))
        else:
            query = query.filter(Article.related_company.ilike(f'%{company_filter}%'))
    
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
        query = query.filter(Article.published_at >= today_start)

    articles = query.order_by(Article.published_at.desc()).all()
    return jsonify([article.to_dict() for article in articles])

# --- NEW: DELETE Article Endpoint ---
@app.route('/api/articles/<int:article_id>', methods=['DELETE'])
def delete_article(article_id):
    article = db.get_or_404(Article, article_id)
    db.session.delete(article)
    db.session.commit()
    return jsonify({'success': True, 'message': 'Article deleted'})

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
    global FETCH_STATUS
    if not fetch_lock.acquire(blocking=False):
        return jsonify({'success': False, 'message': 'A fetch process is already running.'}), 429

    try:
        data = request.get_json()
        source_ids = data.get('source_ids') if data else None
        
        def run_background_fetch(ids):
            global FETCH_STATUS, FETCH_LOG
            FETCH_STATUS = "running"
            FETCH_LOG = [] # Clear previous logs
            try:
                run_pipeline(source_ids=ids)
            except Exception as e:
                FETCH_STATUS = "error"
                log_message(f"PIPELINE CRITICAL ERROR: {e}")
            finally:
                # This 'finally' block ensures the lock is ALWAYS released
                fetch_lock.release()

        thread = threading.Thread(target=run_background_fetch, args=(source_ids,))
        thread.start()
        
        message = f'News fetching started for {len(source_ids)} selected sources.' if source_ids else 'News fetching started for all sources.'
        return jsonify({'success': True, 'message': message})
    except Exception as e:
        fetch_lock.release() # Also release lock on initial error
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/fetch-status', methods=['GET'])
def fetch_status():
    global FETCH_STATUS
    status_to_return = FETCH_STATUS
    # Reset status to idle only after it has been 'completed' once
    if FETCH_STATUS in ["completed", "error"]:
        FETCH_STATUS = "idle"
    return jsonify({'status': status_to_return, 'log': FETCH_LOG})

if __name__ == '__main__':
    print("Starting API server on http://0.0.0.0:5001")
    serve(app, host='0.0.0.0', port=5001)
