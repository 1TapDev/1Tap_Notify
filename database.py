import psycopg2
import os
from psycopg2.extras import DictCursor
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT")

def connect_db():
    """Establish a connection to the PostgreSQL database."""
    try:
        conn = psycopg2.connect(
            dbname=DB_NAME,
            user=DB_USER,
            password=DB_PASSWORD,
            host=DB_HOST,
            port=DB_PORT
        )
        return conn
    except Exception as e:
        print(f"Database connection error: {e}")
        return None

def initialize_tables():
    """Creates all necessary tables in the database."""
    conn = connect_db()
    if not conn:
        return
    cursor = conn.cursor()
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS servers (
                server_id TEXT PRIMARY KEY,
                server_name TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS categories (
                category_id TEXT PRIMARY KEY,
                server_id TEXT REFERENCES servers(server_id) ON DELETE CASCADE,
                category_name TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS channels (
                channel_id TEXT PRIMARY KEY,
                server_id TEXT REFERENCES servers(server_id) ON DELETE CASCADE,
                category_id TEXT REFERENCES categories(category_id) ON DELETE SET NULL,
                channel_name TEXT NOT NULL,
                webhook_url TEXT
            );
            CREATE TABLE IF NOT EXISTS messages (
                message_id TEXT PRIMARY KEY,
                channel_id TEXT REFERENCES channels(channel_id) ON DELETE CASCADE,
                content TEXT,
                author_id TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_edited BOOLEAN DEFAULT FALSE,
                is_deleted BOOLEAN DEFAULT FALSE
            );
            CREATE TABLE IF NOT EXISTS attachments (
                attachment_id TEXT PRIMARY KEY,
                message_id TEXT REFERENCES messages(message_id) ON DELETE CASCADE,
                url TEXT NOT NULL,
                filename TEXT NOT NULL,
                size INTEGER
            );
            CREATE TABLE IF NOT EXISTS threads (
                thread_id TEXT PRIMARY KEY,
                channel_id TEXT REFERENCES channels(channel_id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                parent_message_id TEXT REFERENCES messages(message_id) ON DELETE SET NULL,
                is_archived BOOLEAN DEFAULT FALSE
            );
        """)
        conn.commit()
        print("Database tables initialized successfully.")
    except Exception as e:
        print(f"Error initializing tables: {e}")
    finally:
        cursor.close()
        conn.close()

if __name__ == "__main__":
    initialize_tables()
