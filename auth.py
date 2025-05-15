from flask import request, jsonify, session
import hashlib
import uuid
import functools
import sqlite3
from datetime import datetime, timedelta
from database import add_user, get_user_by_username, get_db_connection, sync_db_to_cloud

def hash_password(password):
    """Hash a password using SHA-256"""
    return hashlib.sha256(password.encode()).hexdigest()

def register_user(username, email, password):
    """Register a new user"""
    # Hash the password before storing
    password_hash = hash_password(password)
    
    # Add the user to the database
    user_id = add_user(username, email, password_hash)
    
    if user_id:
        return {'success': True, 'user_id': user_id}
    else:
        return {'success': False, 'error': 'Username or email already exists'}

def create_session(user_id, username):
    """Create a session for the user in the database"""
    # Generate a session token
    session_token = str(uuid.uuid4())
    
    # Set expiration to 7 days from now
    expires_at = datetime.now() + timedelta(days=7)
    
    # Store the session in the database
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute(
            'INSERT INTO sessions (user_id, token, expires_at) VALUES (?, ?, ?)',
            (user_id, session_token, expires_at)
        )
        conn.commit()
        
        # Sync the database to cloud after the write operation
        sync_db_to_cloud()
        
        return {
            'session_token': session_token,
            'user_id': user_id,
            'username': username,
            'expires_at': expires_at.isoformat()
        }
    except Exception as e:
        print(f"Error creating session: {str(e)}")
        return None
    finally:
        conn.close()

def login_user(username, password):
    """Login a user and return a session token"""
    # Hash the provided password
    password_hash = hash_password(password)
    
    # Get the user from the database
    user = get_user_by_username(username)
    
    if not user:
        return {'success': False, 'error': 'Invalid username or password'}
    
    if user['password_hash'] != password_hash:
        return {'success': False, 'error': 'Invalid username or password'}
    
    # Create a new session
    session_data = create_session(user['user_id'], user['username'])
    
    if session_data:
        return {
            'success': True,
            'user_id': session_data['user_id'],
            'username': session_data['username'],
            'session_token': session_data['session_token']
        }
    else:
        return {'success': False, 'error': 'Failed to create session'}

def logout_user(session_token):
    """Logout a user by removing their session"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute('DELETE FROM sessions WHERE token = ?', (session_token,))
        conn.commit()
        
        # Sync the database to cloud after the write operation
        sync_db_to_cloud()
        
        if cursor.rowcount > 0:
            return {'success': True}
        else:
            return {'success': False, 'error': 'Invalid session token'}
    except Exception as e:
        print(f"Error logging out user: {str(e)}")
        return {'success': False, 'error': 'Database error'}
    finally:
        conn.close()

def get_current_user(session_token):
    """Get the current user from a session token"""
    if not session_token:
        return None
        
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        # Get the session and related user data, checking that the session hasn't expired
        cursor.execute('''
            SELECT 
                s.user_id, 
                u.username, 
                s.expires_at
            FROM sessions s
            JOIN users u ON s.user_id = u.user_id
            WHERE s.token = ? AND s.expires_at > datetime('now')
        ''', (session_token,))
        
        session_data = cursor.fetchone()
        
        if not session_data:
            # Clean up expired sessions periodically
            cursor.execute('DELETE FROM sessions WHERE expires_at <= datetime("now")')
            conn.commit()
            sync_db_to_cloud()
            return None
            
        return {
            'user_id': session_data['user_id'],
            'username': session_data['username']
        }
    except Exception as e:
        print(f"Error getting current user: {str(e)}")
        return None
    finally:
        conn.close()

def login_required(f):
    """Decorator to require login for a route"""
    @functools.wraps(f)
    def decorated_function(*args, **kwargs):
        # Get the session token from the request
        auth_header = request.headers.get('Authorization')
        
        if not auth_header or not auth_header.startswith('Bearer '):
            return jsonify({'error': 'Unauthorized - No valid session token'}), 401
        
        session_token = auth_header.split(' ')[1]
        
        # Check if the session is valid
        user = get_current_user(session_token)
        if not user:
            return jsonify({'error': 'Unauthorized - Invalid or expired session token'}), 401
        
        # Add the user to the request context
        request.user = user
        
        return f(*args, **kwargs)
    
    return decorated_function 