import sqlite3
import os
import hashlib
import tempfile
import threading
from datetime import datetime

# Add imports for Google Cloud Storage
from cloud_storage import (
    download_file, 
    upload_from_filename, 
    check_if_file_exists,
    get_storage_client
)

# Define constants for GCS
GCS_DB_PATH = 'database/studybuddy.db'

# Local SQLite database path
LOCAL_DB_PATH = 'instance/studybuddy.db'
DB_PATH = LOCAL_DB_PATH

# Lock for database operations
db_lock = threading.Lock()

def sync_db_to_cloud():
    """Upload the local database to GCS"""
    try:
        # Check if GCS is available
        if get_storage_client() is None:
            print("GCS not available, skipping database sync")
            return False
            
        print(f"Syncing database to GCS: {GCS_DB_PATH}")
        result = upload_from_filename(LOCAL_DB_PATH, GCS_DB_PATH, content_type='application/x-sqlite3')
        if result:
            print("Database successfully synced to GCS")
            return True
        else:
            print("Failed to sync database to GCS")
            return False
    except Exception as e:
        print(f"Error syncing database to GCS: {str(e)}")
        import traceback
        traceback.print_exc()
        return False

def ensure_db_exists():
    """Ensure the database exists locally (download from GCS if available)"""
    # Make sure the instance directory exists
    os.makedirs('instance', exist_ok=True)
    
    # If DB already exists locally, keep using it
    if os.path.exists(LOCAL_DB_PATH):
        print(f"Using existing local database: {LOCAL_DB_PATH}")
        return
    
    # Check if GCS is available
    if get_storage_client() is None:
        print("GCS not available, creating new local database")
        init_db_schema()
        return
        
    # Check if a database exists in GCS
    if check_if_file_exists(GCS_DB_PATH):
        print(f"Database found in GCS, downloading to: {LOCAL_DB_PATH}")
        try:
            download_file(GCS_DB_PATH, LOCAL_DB_PATH)
            print("Database downloaded successfully")
        except Exception as e:
            print(f"Error downloading database from GCS: {str(e)}")
            import traceback
            traceback.print_exc()
            init_db_schema()
    else:
        print("No database found in GCS, creating new local database")
        init_db_schema()
        # Upload the new database to GCS
        sync_db_to_cloud()

def get_db_connection():
    """Create a connection to the SQLite database"""
    with db_lock:  # Use lock to prevent concurrent access issues
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn

def init_db_schema():
    """Initialize the database with required tables"""
    print("Initializing database schema")
    conn = sqlite3.connect(LOCAL_DB_PATH)
    cursor = conn.cursor()
    
    # Create users table
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    
    # Create pdfs table (stores unique PDFs)
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS pdfs (
        pdf_id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        file_path TEXT NOT NULL,
        pdf_hash TEXT UNIQUE NOT NULL,
        file_size INTEGER NOT NULL,
        page_count INTEGER NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    
    # Create user_pdfs table (maps users to their PDFs)
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS user_pdfs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        pdf_id INTEGER NOT NULL,
        uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users (user_id),
        FOREIGN KEY (pdf_id) REFERENCES pdfs (pdf_id)
    )
    ''')
    
    # Create sessions table
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS sessions (
        session_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        token TEXT UNIQUE NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        expires_at TIMESTAMP NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users (user_id)
    )
    ''')
    
    conn.commit()
    conn.close()

def calculate_file_hash(file_path):
    """Calculate SHA-256 hash of a file"""
    hasher = hashlib.sha256()
    with open(file_path, 'rb') as f:
        buf = f.read(65536)  # Read in 64k chunks
        while len(buf) > 0:
            hasher.update(buf)
            buf = f.read(65536)
    return hasher.hexdigest()

def add_user(username, email, password_hash):
    """Add a new user to the database"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute(
            'INSERT INTO users (username, email, password_hash) VALUES (?, ?, ?)',
            (username, email, password_hash)
        )
        conn.commit()
        user_id = cursor.lastrowid
        
        # Sync to GCS after write operation
        sync_db_to_cloud()
        
        return user_id
    except sqlite3.IntegrityError:
        # Username or email already exists
        return None
    finally:
        conn.close()

def get_user_by_username(username):
    """Get a user by username"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute('SELECT * FROM users WHERE username = ?', (username,))
    user = cursor.fetchone()
    
    conn.close()
    return dict(user) if user else None

def add_pdf(title, file_path, pdf_hash, file_size, page_count):
    """Add a new PDF to the database if it doesn't exist"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        # First check if the PDF already exists
        cursor.execute('SELECT pdf_id FROM pdfs WHERE pdf_hash = ?', (pdf_hash,))
        existing_pdf = cursor.fetchone()
        
        if existing_pdf:
            # Return the existing PDF's ID
            return existing_pdf['pdf_id']
        
        # Otherwise, add the new PDF
        cursor.execute(
            'INSERT INTO pdfs (title, file_path, pdf_hash, file_size, page_count) VALUES (?, ?, ?, ?, ?)',
            (title, file_path, pdf_hash, file_size, page_count)
        )
        conn.commit()
        pdf_id = cursor.lastrowid
        
        # Sync to GCS after write operation
        sync_db_to_cloud()
        
        return pdf_id
    finally:
        conn.close()

def associate_pdf_with_user(user_id, pdf_id):
    """Associate a PDF with a user"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        # Check if the association already exists
        cursor.execute(
            'SELECT id FROM user_pdfs WHERE user_id = ? AND pdf_id = ?',
            (user_id, pdf_id)
        )
        existing = cursor.fetchone()
        
        if existing:
            return existing['id']
        
        # Create the association
        cursor.execute(
            'INSERT INTO user_pdfs (user_id, pdf_id) VALUES (?, ?)',
            (user_id, pdf_id)
        )
        conn.commit()
        
        # Sync to GCS after write operation
        sync_db_to_cloud()
        
        return cursor.lastrowid
    finally:
        conn.close()

def get_user_pdfs(user_id):
    """Get all PDFs uploaded by a specific user"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT p.pdf_id, p.title, p.file_path, p.pdf_hash, p.file_size, p.page_count, p.created_at, up.uploaded_at
        FROM pdfs p
        JOIN user_pdfs up ON p.pdf_id = up.pdf_id
        WHERE up.user_id = ?
        ORDER BY up.uploaded_at DESC
    ''', (user_id,))
    
    pdfs = [dict(row) for row in cursor.fetchall()]
    conn.close()
    
    return pdfs

def get_pdf_by_hash(pdf_hash):
    """Get a PDF by its hash"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute('SELECT * FROM pdfs WHERE pdf_hash = ?', (pdf_hash,))
    pdf = cursor.fetchone()
    
    conn.close()
    return dict(pdf) if pdf else None

def get_pdf_by_path(file_path):
    """Get PDF information by its file path."""
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute("SELECT * FROM pdfs WHERE file_path = ?", (file_path,))
        pdf_row = cursor.fetchone()

        if pdf_row:
            return {
                'pdf_id': pdf_row[0],
                'title': pdf_row[1],
                'file_path': pdf_row[2],
                'pdf_hash': pdf_row[3],
                'file_size': pdf_row[4],
                'page_count': pdf_row[5],
                'uploaded_at': pdf_row[6]
            }
        else:
            return None
    except Exception as e:
        print(f"Error getting PDF by path: {str(e)}")
        return None
    finally:
        cursor.close()
        conn.close()

def get_pdf_versions_by_name(base_name):
    """Get all versions of a PDF by its base name."""
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # First try exact match
        cursor.execute("SELECT file_path FROM pdfs WHERE file_path = ?", (base_name,))
        versions = [row[0] for row in cursor.fetchall()]
        
        # Then try with '_N' versions
        cursor.execute("SELECT file_path FROM pdfs WHERE file_path LIKE ?", (base_name + '_%',))
        versions.extend([row[0] for row in cursor.fetchall()])
        
        return versions
    except Exception as e:
        print(f"Error getting PDF versions: {str(e)}")
        return []
    finally:
        cursor.close()
        conn.close()

# Initialize the database when this module is imported
ensure_db_exists() 