import os
import ssl

import sys

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
from flask_cors import CORS  # Import CORS

from config import config

# --- App & DB Initialization ---
app = Flask(__name__)
CORS(app)  # Enable CORS for all routes
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


# (The setup_database function remains the same)
def setup_database(app_instance):
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'news.db')
    if not os.path.exists(db_path):
        print("Database not found. Creating and seeding a new one...")
        with app_instance.app_context():
            db.create_all()
            INITIAL_FEEDS = {
                'TC-AI': 'https://techcrunch.com/category/artificial-intelligence/feed/',
                'testingcatalog': 'https://www.testingcatalog.com/rss/',
                '机器之心': 'https://wechat2rss.xlab.app/feed/51e92aad2728acdd1fda7314be32b16639353001.xml',
                '新智元': 'https://wechat2rss.xlab.app/feed/ede30346413ea70dbef5d485ea5cbb95cca446e7.xml',
                'Theverge': 'https://www.theverge.com/rss/index.xml',
            }
            for key, url in INITIAL_FEEDS.items():
                if not FeedSource.query.filter_by(key=key).first():
                    db.session.add(FeedSource(key=key, url=url))
            db.session.commit()
            print("Database created and seeded successfully.")


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


# --- Pipeline for processing general websites ---
def run_website_pipeline(url: str, source_key: str, source_name: str):
    """
    Fetches a website, extracts news using Gemini, and saves them to the database.
    """
    print(f"PIPELINE: Processing website with AI: {url}")
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
        page_response = requests.get(url, headers=headers, timeout=30)
        page_response.raise_for_status()
        soup = BeautifulSoup(page_response.content, 'html.parser')
        page_text = soup.get_text(separator='\n', strip=True)

        if not config.GEMINI_API_KEY:
            print("PIPELINE:   - Skipped (GEMINI_API_KEY not configured).")
            return

        prompt = (
            "You are an expert news-finding AI. Analyze the text content of the provided webpage and identify all distinct news articles. For each article you find, extract the following information:\n"
            "1. `title`: The full title of the article.\n"
            "2. `url`: The absolute URL link to the article. If the link is relative (e.g., '/news/story1'), you must combine it with the base URL of the page.\n"
            "3. `summary`: A concise, one-paragraph summary of the article's main points.\n"
            "4. `published_at`: The publication date and time in ISO 8601 format (YYYY-MM-DDTHH:MM:SSZ). If you can only find a date, use the start of that day. If you cannot determine the date, use null.\n"
            "Respond ONLY with a single JSON object containing a key `articles` which is a list of the article objects you found. Each object in the list must have the keys `title`, `url`, `summary`, and `published_at`. Do not include any articles that are clearly just ads or navigation links. If you find no articles, return an empty list.\n"
            f"Base URL of the page for reference: {url}\n"
            f"Webpage Text Content:\n{page_text[:15000]}"
        )

        gemini_response = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={config.GEMINI_API_KEY}",
            headers={"Content-Type": "application/json"}, json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=180
        )
        gemini_response.raise_for_status()
        data = gemini_response.json()['candidates'][0]['content']['parts'][0]['text']
        try:
            cleaned_data = data.strip().lstrip('```json').rstrip('```')
            found_data = json.loads(cleaned_data)
        except json.JSONDecodeError:
            print(f"PIPELINE:   - ERROR: Gemini API did not return valid JSON.")
            return

        articles_from_ai = found_data.get('articles', [])
        if not articles_from_ai:
            print("PIPELINE:   - Gemini found no articles on this page.")
            return

        print(f"PIPELINE:   - Gemini identified {len(articles_from_ai)} articles. Processing them now.")
        for article_data in articles_from_ai:
            try:
                pub_date = parser.parse(article_data['published_at']) if article_data.get(
                    'published_at') else datetime.now(timezone.utc)
                if Article.query.filter_by(title=article_data['title']).first():
                    print(f"PIPELINE:     - Skipped duplicate title: '{article_data['title']}'")
                    continue
                new_article = Article(
                    title=article_data['title'],
                    content=article_data['summary'],
                    original_url=article_data['url'],
                    category=source_key.split('-', 1)[1] if '-' in source_key else 'Web',
                    published_at=pub_date,
                    source=source_name,
                    related_company=extract_related_company_pipeline(
                        article_data['title'] + " " + article_data['summary'])
                )
                db.session.add(new_article)
                db.session.commit()
                print(f"PIPELINE:     + Saved to database: '{article_data['title']}'")
            except IntegrityError:
                db.session.rollback()
            except Exception as e:
                db.session.rollback()
                print(f"PIPELINE:     - An error occurred while saving article '{article_data.get('title')}': {e}")
    except Exception as e:
        print(f"PIPELINE:   - ERROR: An unexpected error occurred while processing website {url}. Reason: {e}")


def run_pipeline(source_ids=None):
    """
    Runs the news processing pipeline with an intelligent fallback mechanism.
    """
    with app.app_context():
        print("PIPELINE: Starting news processing...")
        source_name_map = {'TC': 'TechCrunch', 'Wired': 'Wired', 'AIbase': 'AIbase'}
        feeds_query = FeedSource.query
        if source_ids:
            feeds_query = feeds_query.filter(FeedSource.id.in_(source_ids))
        feeds = feeds_query.all()

        for feed_source in feeds:
            print(f"\nPIPELINE: --- Processing source: {feed_source.key} ({feed_source.url}) ---")
            source_name = source_name_map.get(feed_source.key.split('-', 1)[0], feed_source.key.split('-', 1)[0])
            feed = feedparser.parse(feed_source.url,
                                    agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36')

            if feed.bozo or not feed.entries:
                if feed.bozo:
                    print(f"PIPELINE: Feed parsing failed (Reason: {feed.bozo_exception}).")
                else:
                    print("PIPELINE: Feed is valid but contains no news entries.")
                print("PIPELINE: Switching to AI-powered website scraper as a fallback.")
                run_website_pipeline(feed_source.url, feed_source.key, source_name)
                continue

            print(f"PIPELINE: Successfully parsed RSS feed. Found {len(feed.entries)} entries.")
            for entry in feed.entries:
                if (datetime.now(timezone.utc) - parse_publication_date_pipeline(entry)).days >= 3:
                    continue
                print(f"PIPELINE: Processing: '{entry.title}'")
                time.sleep(5)
                original_content = BeautifulSoup(entry.summary, 'html.parser').get_text(separator='\n', strip=True)
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
                try:
                    db.session.commit()
                    print("PIPELIPELINE:   + Saved to database.")
                except IntegrityError:
                    db.session.rollback()
                    print(f"PIPELINE:   - Skipped duplicate title: '{entry.title}'")
                except Exception as e:
                    db.session.rollback()
                    print(f"PIPELINE:   - An unexpected database error occurred: {e}")
        print("\nPIPELINE: Finished.")


# --- NEW API ROUTES ---

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


@app.route('/api/fetch-news', methods=['POST'])
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
    setup_database(app)
    print("Starting API server on http://0.0.0.0:5001")
    # Use Waitress, a production-ready server
    serve(app, host='0.0.0.0', port=5001)
