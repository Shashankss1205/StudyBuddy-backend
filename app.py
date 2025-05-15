from flask import Flask, request, jsonify, Response, send_file, redirect
from flask_cors import CORS
from pdf2image import convert_from_path
import os
import shutil
from PIL import Image
import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold
import base64
from io import BytesIO
import time
import traceback
import json
import requests
from dotenv import load_dotenv
import re
import zipfile
import io
import os.path
from tempfile import NamedTemporaryFile

# Load environment variables from .env file
load_dotenv()

# Import our modules
from database import (
    calculate_file_hash, 
    add_pdf, 
    associate_pdf_with_user, 
    get_user_pdfs,
    get_pdf_by_hash,
    get_pdf_by_path,
    get_pdf_versions_by_name
)
from auth import (
    register_user, 
    login_user, 
    logout_user, 
    login_required
)
from cloud_storage import (
    generate_signed_url,
    upload_file,
    upload_from_file,
    upload_from_filename,
    check_if_file_exists,
    generate_unique_filepath,
    create_bucket_if_not_exists,
    download_as_string,
    list_files_with_prefix,
    get_storage_client
)

app = Flask(__name__)
CORS(app)

# Configure API keys
GOOGLE_API_KEY = os.getenv('GEMINI_API_KEY')
print(f"Google API Key: {GOOGLE_API_KEY[:10]}...")

# Configure Google Generative AI
genai.configure(api_key=GOOGLE_API_KEY)

# Initialize the Gemini model
generation_config = {
    "temperature": 0.7,
    "top_p": 0.95,
    "top_k": 64,
    "max_output_tokens": 8192,
}

safety_settings = {
    HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
    HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
    HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
    HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
}

model = genai.GenerativeModel(
    model_name="gemini-1.5-pro",
    generation_config=generation_config,
    safety_settings=safety_settings
)

# Initialize Google Cloud Storage
try:
    bucket = create_bucket_if_not_exists()
    if bucket:
        print("Google Cloud Storage initialized successfully")
    else:
        print("Continuing without Google Cloud Storage - will use local storage only")
except Exception as e:
    print(f"Error initializing Google Cloud Storage: {str(e)}")
    traceback.print_exc()
    print("Continuing without Google Cloud Storage - will use local storage only")

# Main upload folder - used for temporary storage before GCS upload
UPLOAD_FOLDER = 'uploads'
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

# GCS folder structure
GCS_PDF_PREFIX = 'pdfs/'
GCS_IMAGE_PREFIX = 'images/'
GCS_AUDIO_PREFIX = 'audio/'
GCS_TEXT_PREFIX = 'text/'
GCS_QUIZ_PREFIX = 'quiz/'

# User authentication routes
@app.route('/auth/register', methods=['POST'])
def register():
    data = request.json
    username = data.get('username')
    email = data.get('email')
    password = data.get('password')
    
    # Validate inputs
    if not username or not email or not password:
        return jsonify({'error': 'Missing required fields'}), 400
    
    # Register the user
    result = register_user(username, email, password)
    
    if result['success']:
        return jsonify({'message': 'User registered successfully', 'user_id': result['user_id']}), 201
    else:
        return jsonify({'error': result['error']}), 400

@app.route('/auth/login', methods=['POST'])
def login():
    data = request.json
    username = data.get('username')
    password = data.get('password')
    
    # Validate inputs
    if not username or not password:
        return jsonify({'error': 'Missing username or password'}), 400
    
    # Login the user
    result = login_user(username, password)
    
    if result['success']:
        return jsonify({
            'message': 'Login successful',
            'user_id': result['user_id'],
            'username': result['username'],
            'session_token': result['session_token']
        }), 200
    else:
        return jsonify({'error': result['error']}), 401

@app.route('/auth/logout', methods=['POST'])
def logout():
    auth_header = request.headers.get('Authorization')
    
    if not auth_header or not auth_header.startswith('Bearer '):
        return jsonify({'error': 'No valid session token provided'}), 400
    
    session_token = auth_header.split(' ')[1]
    
    # Logout the user
    result = logout_user(session_token)
    
    if result['success']:
        return jsonify({'message': 'Logout successful'}), 200
    else:
        return jsonify({'error': result['error']}), 400

# Route to get a list of existing PDFs for the current user
@app.route('/existing-pdfs', methods=['GET'])
@login_required
def get_existing_pdfs():
    try:
        # Get the current user's ID
        user_id = request.user['user_id']
        
        # Get all PDFs for this user
        user_pdfs = get_user_pdfs(user_id)
        
        pdfs = []
        for pdf in user_pdfs:
            # Check if this PDF exists in GCS - handle missing credentials
            gcs_available = get_storage_client() is not None
            gcs_exists = False
            metadata = {}
            
            if gcs_available:
                # First check if the PDF exists in GCS
                gcs_pdf_path = f"{GCS_PDF_PREFIX}{pdf['file_path']}/original.pdf"
                gcs_metadata_path = f"{GCS_PDF_PREFIX}{pdf['file_path']}/metadata.json"
                
                gcs_exists = check_if_file_exists(gcs_pdf_path)
                
                if gcs_exists:
                    # Get metadata if exists in GCS
                    try:
                        metadata_content = download_as_string(gcs_metadata_path)
                        if metadata_content:
                            metadata = json.loads(metadata_content.decode('utf-8'))
                    except Exception as e:
                        print(f"Error loading metadata from GCS: {str(e)}")
                    
                    pdfs.append({
                        'name': pdf['file_path'],
                        'total_pages': pdf['page_count'],
                        'date_processed': str(pdf['uploaded_at']),
                        'original_filename': metadata.get('original_filename', pdf['title'])
                    })
                    continue
            
            # Fallback to checking local storage
            folder_path = os.path.join(UPLOAD_FOLDER, pdf['file_path'])
            
            # Check if it has the required structure
            has_images = os.path.exists(os.path.join(folder_path, 'image_files'))
            has_text = os.path.exists(os.path.join(folder_path, 'text_files'))
            has_audio = os.path.exists(os.path.join(folder_path, 'audio_files'))
            
            if has_images and has_text and has_audio:
                # Get metadata if exists
                metadata_path = os.path.join(folder_path, 'metadata.json')
                metadata = {}
                if os.path.exists(metadata_path):
                    with open(metadata_path, 'r') as f:
                        metadata = json.load(f)
                
                pdfs.append({
                'name': pdf['file_path'],
                'total_pages': pdf['page_count'],
                'date_processed': str(pdf['uploaded_at']),
                'original_filename': metadata.get('original_filename', pdf['title'])
            })
                
                # Try to upload metadata to GCS for future use (if GCS is available)
                if gcs_available:
                    try:
                        gcs_metadata_path = f"{GCS_PDF_PREFIX}{pdf['file_path']}/metadata.json"
                        upload_file(json.dumps(metadata), gcs_metadata_path, content_type='application/json')
                        print(f"Uploaded metadata to GCS: {gcs_metadata_path}")
                    except Exception as e:
                        print(f"Error uploading metadata to GCS: {str(e)}")
        
        print(f"Found {len(pdfs)} existing PDFs for user {user_id}")
        return jsonify({'pdfs': pdfs})
    except Exception as e:
        print(f"Error getting existing PDFs: {str(e)}")
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

# Route to check if a PDF exists by path
@app.route('/check-pdf/<path:pdf_name>', methods=['GET'])
def check_pdf_exists(pdf_name):
    try:
        # Check if the PDF folder exists
        pdf_folder = os.path.join(UPLOAD_FOLDER, pdf_name)
        if os.path.exists(pdf_folder) and os.path.isdir(pdf_folder):
            # Check if it has the required structure
            has_images = os.path.exists(os.path.join(pdf_folder, 'image_files'))
            has_text = os.path.exists(os.path.join(pdf_folder, 'text_files'))
            has_audio = os.path.exists(os.path.join(pdf_folder, 'audio_files'))
            
            if has_images and has_text and has_audio:
                return jsonify({'exists': True})
        
        return jsonify({'exists': False})
    except Exception as e:
        print(f"Error checking if PDF exists: {str(e)}")
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

# Route to check if a PDF exists by original filename
@app.route('/check-pdf-by-filename/<path:filename>', methods=['GET'])
def check_pdf_exists_by_filename(filename):
    try:
        # Extract the filename without extension
        if '.' in filename:
            filename_no_ext = os.path.splitext(filename)[0]
        else:
            filename_no_ext = filename
            
        # Clean the name (same logic as in process-pdf)
        clean_name = re.sub(r'[^\w\-]', '_', filename_no_ext).lower()
        
        # Check for versions in database
        versions = get_pdf_versions_by_name(clean_name)
        
        if versions and len(versions) > 0:
            return jsonify({
                'exists': True,
                'base_name': clean_name,
                'versions': versions
            })
        
        # Check if GCS is available
        gcs_available = get_storage_client() is not None
        
        # If GCS is available, check there
        if gcs_available:
            gcs_base_path = f"{GCS_PDF_PREFIX}{clean_name}"
            gcs_pdf_path = f"{gcs_base_path}/original.pdf"
            
            if check_if_file_exists(gcs_pdf_path):
                # Check for versions with the same base name
                versions = [clean_name]
                counter = 2
                while check_if_file_exists(f"{GCS_PDF_PREFIX}{clean_name}_{counter}/original.pdf"):
                    versions.append(f"{clean_name}_{counter}")
                    counter += 1
                    
                return jsonify({
                    'exists': True,
                    'base_name': clean_name,
                    'versions': versions
                })
        
        # Fallback to checking local storage
        base_path = os.path.join(UPLOAD_FOLDER, clean_name)
        exists = os.path.exists(base_path) and os.path.isdir(base_path)
        
        if exists:
            # Get all versions (including this one and ones with _2, _3, etc.)
            versions = [clean_name]
            counter = 2
            while os.path.exists(os.path.join(UPLOAD_FOLDER, f"{clean_name}_{counter}")):
                versions.append(f"{clean_name}_{counter}")
                counter += 1
                
            return jsonify({
                'exists': True,
                'base_name': clean_name,
                'versions': versions
            })
        
        return jsonify({'exists': False})
    except Exception as e:
        print(f"Error checking if PDF exists by filename: {str(e)}")
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

# Use existing PDF content
@app.route('/use-existing/<path:pdf_name>', methods=['GET'])
def use_existing_pdf(pdf_name):
    try:
        print(f"Request to use existing PDF: {pdf_name}")
        
        # Check if GCS is available
        gcs_available = get_storage_client() is not None
        
        # Prepare variables
        pages = []
        metadata = {}
        total_pages = 0
        
        # Get metadata if GCS is available
        if gcs_available:
            gcs_metadata_path = f"{GCS_PDF_PREFIX}{pdf_name}/metadata.json"
            
            try:
                if check_if_file_exists(gcs_metadata_path):
                    metadata_content = download_as_string(gcs_metadata_path)
                    if metadata_content:
                        metadata = json.loads(metadata_content)
                        print(f"Loaded metadata from GCS: {metadata}")
                else:
                    print(f"No metadata file found in GCS at {gcs_metadata_path}")
            except Exception as e:
                print(f"Error loading metadata from GCS: {str(e)}")
        
        # Try to find metadata in local folder as fallback
        if not metadata:
            local_metadata_path = os.path.join(UPLOAD_FOLDER, pdf_name, 'metadata.json')
            if os.path.exists(local_metadata_path):
                with open(local_metadata_path, 'r') as f:
                    metadata = json.load(f)
                print(f"Loaded metadata from local file: {metadata}")
                
                # Try to upload to GCS if available
                if gcs_available:
                    try:
                        gcs_metadata_path = f"{GCS_PDF_PREFIX}{pdf_name}/metadata.json"
                        upload_file(json.dumps(metadata), gcs_metadata_path, content_type='application/json')
                        print(f"Uploaded metadata to GCS: {gcs_metadata_path}")
                    except Exception as e:
                        print(f"Error uploading metadata to GCS: {str(e)}")
        
        # Get page count from database if possible
        pdf_info = get_pdf_by_path(pdf_name)
        total_pages = pdf_info.get('page_count', 0) if pdf_info else 0
        
        # If database doesn't have page count and GCS is available, try to determine from GCS
        if total_pages == 0 and gcs_available:
            try:
                # List all image files in GCS with this prefix
                image_prefix = f"{GCS_IMAGE_PREFIX}{pdf_name}/"
                image_files = list_files_with_prefix(image_prefix)
                
                # Extract page numbers from file paths
                page_numbers = []
                for file_path in image_files:
                    # Extract page number from file path like "images/pdf_name/page_1.jpg"
                    match = re.search(r'page_(\d+)\.jpg$', file_path)
                    if match:
                        page_numbers.append(int(match.group(1)))
                
                if page_numbers:
                    total_pages = max(page_numbers)
                    print(f"Determined total pages from GCS: {total_pages}")
                else:
                    print("No page files found in GCS")
            except Exception as e:
                print(f"Error listing files in GCS: {str(e)}")
        
        # If we still don't have total pages, try local files as a last resort
        if total_pages == 0:
            pdf_folder = os.path.join(UPLOAD_FOLDER, pdf_name)
            image_folder = os.path.join(pdf_folder, 'image_files')
            
            if os.path.exists(image_folder):
                page_files = []
                for file in os.listdir(image_folder):
                    if file.endswith('.jpg') and '_page_' in file:
                        page_files.append(file)
                
                # Sort page files by page number
                page_files.sort(key=lambda f: int(f.split('_page_')[1].split('.')[0]))
                
                if page_files:
                    total_pages = int(page_files[-1].split('_page_')[1].split('.')[0])
                    print(f"Determined total pages from local files: {total_pages}")
        
        # If we still don't have pages, return an error
        if total_pages == 0:
            return jsonify({'error': 'Could not determine page count for PDF'}), 404
        
        # Now fetch data for each page
        for page_num in range(1, total_pages + 1):
            print(f"Processing page {page_num}")
            
            # Get image URL
            image_url = f"/pdf/{pdf_name}/image/{page_num}"
            
            # Get audio URL
            audio_url = f"/pdf/{pdf_name}/audio/{page_num}"
            
            # Prepare variables for content
            explanation = ""
            image_data = ""
            audio_data = ""
            
            # Get explanation - first try GCS if available
            if gcs_available:
                gcs_text_path = f"{GCS_TEXT_PREFIX}{pdf_name}/page_{page_num}.md"
                
                try:
                    if check_if_file_exists(gcs_text_path):
                        explanation_bytes = download_as_string(gcs_text_path)
                        if explanation_bytes:
                            explanation = explanation_bytes.decode('utf-8')
                            print(f"Loaded explanation from GCS for page {page_num}")
                except Exception as e:
                    print(f"Error loading explanation from GCS for page {page_num}: {str(e)}")
            
            # If not found in GCS, try local file
            if not explanation:
                local_text_path = os.path.join(UPLOAD_FOLDER, pdf_name, 'text_files', f"{pdf_name}_page_{page_num}.md")
                if os.path.exists(local_text_path):
                    with open(local_text_path, 'r') as f:
                        explanation = f.read()
                    print(f"Loaded explanation from local file for page {page_num}")
                    
                    # Upload to GCS for future use if available
                    if gcs_available:
                        try:
                            gcs_text_path = f"{GCS_TEXT_PREFIX}{pdf_name}/page_{page_num}.md"
                            print(f"Explanation content type: {type(explanation).__name__}, length: {len(explanation)}")
                            upload_file(explanation, gcs_text_path, content_type='text/markdown')
                            print(f"Uploaded explanation to GCS: {gcs_text_path}")
                        except Exception as e:
                            print(f"Error uploading explanation to GCS: {str(e)}")
                            traceback.print_exc()
            
            # For the first page only, include the base64 image and audio data
            if page_num == 1:
                # Get image data - first try GCS if available
                if gcs_available:
                    try:
                        gcs_image_path = f"{GCS_IMAGE_PREFIX}{pdf_name}/page_{page_num}.jpg"
                        if check_if_file_exists(gcs_image_path):
                            image_bytes = download_as_string(gcs_image_path)
                            if image_bytes:
                                image_data = base64.b64encode(image_bytes).decode()
                                print(f"Loaded image data from GCS for page {page_num}")
                    except Exception as e:
                        print(f"Error loading image data from GCS for page {page_num}: {str(e)}")
                
                # If not found in GCS, try local file
                if not image_data:
                    local_image_path = os.path.join(UPLOAD_FOLDER, pdf_name, 'image_files', f"{pdf_name}_page_{page_num}.jpg")
                    if os.path.exists(local_image_path):
                        with open(local_image_path, 'rb') as f:
                            image_bytes = f.read()
                            image_data = base64.b64encode(image_bytes).decode()
                        print(f"Loaded image data from local file for page {page_num}")
                        
                        # Upload to GCS for future use if available
                        if gcs_available:
                            try:
                                gcs_image_path = f"{GCS_IMAGE_PREFIX}{pdf_name}/page_{page_num}.jpg"
                                upload_file(image_bytes, gcs_image_path, content_type='image/jpeg')
                                print(f"Uploaded image to GCS: {gcs_image_path}")
                            except Exception as e:
                                print(f"Error uploading image to GCS: {str(e)}")
                
                # Get audio data - first try GCS if available
                if gcs_available:
                    try:
                        gcs_audio_path = f"{GCS_AUDIO_PREFIX}{pdf_name}/page_{page_num}.mp3"
                        if check_if_file_exists(gcs_audio_path):
                            audio_bytes = download_as_string(gcs_audio_path)
                            if audio_bytes:
                                audio_data = base64.b64encode(audio_bytes).decode()
                                print(f"Loaded audio data from GCS for page {page_num}")
                    except Exception as e:
                        print(f"Error loading audio data from GCS for page {page_num}: {str(e)}")
                
                # If not found in GCS, try local file
                if not audio_data:
                    local_audio_path = os.path.join(UPLOAD_FOLDER, pdf_name, 'audio_files', f"{pdf_name}_page_{page_num}.mp3")
                    if os.path.exists(local_audio_path):
                        with open(local_audio_path, 'rb') as f:
                            audio_bytes = f.read()
                            audio_data = base64.b64encode(audio_bytes).decode()
                        print(f"Loaded audio data from local file for page {page_num}")
                        
                        # Upload to GCS for future use if available
                        if gcs_available:
                            try:
                                gcs_audio_path = f"{GCS_AUDIO_PREFIX}{pdf_name}/page_{page_num}.mp3"
                                upload_file(audio_bytes, gcs_audio_path, content_type='audio/mpeg')
                                print(f"Uploaded audio to GCS: {gcs_audio_path}")
                            except Exception as e:
                                print(f"Error uploading audio to GCS: {str(e)}")
            
            # Add page data to response
            pages.append({
                'page_number': page_num,
                'image': image_data,
                'explanation': explanation,
                'audio': audio_data,
                'audio_url': audio_url,
                'image_url': image_url
            })
        
        print(f"Successfully loaded {len(pages)} pages for PDF {pdf_name}")
        
        return jsonify({
            'total_pages': total_pages,
            'pdf_name': pdf_name,
            'pages': pages
        })
    except Exception as e:
        print(f"Error using existing PDF: {str(e)}")
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

# Routes to serve files from the PDF structure
@app.route('/pdf/<path:pdf_name>/audio/<int:page_num>', methods=['GET'])
def get_pdf_audio(pdf_name, page_num):
    try:
        print(f"Audio request for PDF: {pdf_name}, Page: {page_num}")
        
        # GCS path for the audio file
        gcs_path = f"{GCS_AUDIO_PREFIX}{pdf_name}/page_{page_num}.mp3"
        
        # Check if GCS is available
        gcs_available = get_storage_client() is not None
        gcs_exists = False
        
        if gcs_available:
            # Check if file exists in GCS
            gcs_exists = check_if_file_exists(gcs_path)
            print(f"File exists in GCS: {gcs_exists}")
            
            if gcs_exists:
                # Generate a signed URL for the audio file
                signed_url = generate_signed_url(gcs_path, expiration_minutes=30)
                if signed_url:
                    print(f"Redirecting to signed URL: {signed_url[:50]}...")
                    return redirect(signed_url)
                else:
                    print("Failed to generate signed URL")
        
        # Fallback to checking local files if not in GCS or signed URL failed
        # Try different potential local paths
        possible_paths = [
            os.path.join(UPLOAD_FOLDER, pdf_name, 'audio_files', f"{pdf_name}_page_{page_num}.mp3"),  # Standard format
            os.path.join(UPLOAD_FOLDER, pdf_name, 'audio_files', f"page_{page_num}.mp3"),  # Alternate format
            os.path.join(UPLOAD_FOLDER, pdf_name, 'audio_files', f"{pdf_name.split('_')[0]}_page_{page_num}.mp3")  # Format with base name
        ]
        
        for audio_path in possible_paths:
            print(f"Checking local path: {audio_path}")
            if os.path.exists(audio_path):
                print(f"Found audio file at: {audio_path}")
                
                # Check if the file is valid
                if os.path.getsize(audio_path) == 0:
                    print(f"Warning: Audio file is empty: {audio_path}")
                    continue
                
                # Try to upload to GCS for future requests if GCS is available
                if gcs_available and not gcs_exists:
                    try:
                        print(f"Uploading to GCS: {gcs_path}")
                        result = upload_from_filename(audio_path, gcs_path, content_type='audio/mpeg')
                        print(f"Upload result: {result}")
                        
                        if result:
                            signed_url = generate_signed_url(gcs_path, expiration_minutes=30)
                            if signed_url:
                                print(f"Redirecting to signed URL after upload: {signed_url[:50]}...")
                                return redirect(signed_url)
                    except Exception as e:
                        print(f"Error uploading audio to GCS: {str(e)}")
                        traceback.print_exc()
                
                # Fallback to serving the local file
                print(f"Serving local file: {audio_path}")
                return send_file(audio_path, mimetype='audio/mpeg')
        
        # If we still haven't found the audio, try to regenerate it
        print(f"Audio file not found for PDF: {pdf_name}, Page: {page_num}")
        
        # Try to regenerate the audio from the text file
        text_path = os.path.join(UPLOAD_FOLDER, pdf_name, 'text_files', f"{pdf_name}_page_{page_num}.md")
        if os.path.exists(text_path):
            try:
                print(f"Attempting to regenerate audio from text: {text_path}")
                with open(text_path, 'r') as f:
                    explanation = f.read()
                
                # Use the Google Cloud Text-to-Speech REST API directly with API key
                tts_url = f"https://texttospeech.googleapis.com/v1/text:synthesize?key={GOOGLE_API_KEY}"
                
                # Remove special markdown characters for speech but keep for display
                speech_text = re.sub(r'\*\*(.*?)\*\*', r'\1', explanation)
                speech_text = speech_text.replace("*", "")
                
                # Ensure the text is not empty
                if not speech_text.strip():
                    speech_text = f"Page {page_num} content could not be processed properly."
                
                # If speech text is too long, truncate it to avoid API limits
                if len(speech_text) > 5000:
                    print(f"Warning: Speech text is very long ({len(speech_text)} chars), truncating...")
                    speech_text = speech_text[:5000] + "... The rest of the content has been truncated for processing."
                
                payload = {
                    "input": {
                        "text": speech_text
                    },
                    "voice": {
                        "languageCode": "en-IN",
                        "name": "en-IN-Chirp3-HD-Achernar", 
                        "ssmlGender": "NEUTRAL"
                    },
                    "audioConfig": {
                        "audioEncoding": "MP3",
                        "effectsProfileId": [
                            "large-automotive-class-device"
                        ],
                        "speakingRate": 1
                    }
                }
                
                print(f"Sending TTS request for page {page_num}...")
                tts_response = requests.post(tts_url, json=payload)
                tts_response.raise_for_status()  # Will raise an exception for 4XX/5XX responses
                
                # Log the response
                print(f"TTS response status: {tts_response.status_code}")
                if tts_response.status_code != 200:
                    print(f"TTS error response: {tts_response.text}")
                
                # Extract audio content from response
                response_json = tts_response.json()
                if "audioContent" not in response_json:
                    print(f"Error: No audioContent in TTS response. Response: {response_json}")
                    raise Exception("No audioContent in TTS response")
                        
                audio_data = base64.b64decode(response_json["audioContent"])
                
                # Check if audio data is valid
                if len(audio_data) == 0:
                    print(f"Error: Empty audio data returned from TTS API")
                    raise Exception("Empty audio data returned from TTS API")
                
                print(f"Received audio data: {len(audio_data)} bytes")
                
                # Make sure the audio directory exists
                audio_dir = os.path.join(UPLOAD_FOLDER, pdf_name, 'audio_files')
                os.makedirs(audio_dir, exist_ok=True)
                
                # Save the audio file locally
                local_audio_path = os.path.join(audio_dir, f"{pdf_name}_page_{page_num}.mp3")
                with open(local_audio_path, 'wb') as f:
                    f.write(audio_data)
                print(f"Audio saved locally to {local_audio_path}, size: {len(audio_data)} bytes")
                
                # Upload audio to GCS
                gcs_audio_path = f"{GCS_AUDIO_PREFIX}{pdf_name}/page_{page_num}.mp3"
                try:
                    upload_file(audio_data, gcs_audio_path, content_type='audio/mpeg')
                    print(f"Audio uploaded to GCS: {gcs_audio_path}")
                except Exception as e:
                    print(f"Error uploading audio to GCS: {str(e)}")
                    traceback.print_exc()
                
            except Exception as e:
                print(f"Error regenerating audio: {str(e)}")
                traceback.print_exc()
        
        return jsonify({'error': 'Audio file not found and could not be generated'}), 404
        
    except Exception as e:
        print(f"Error serving audio: {str(e)}")
        traceback.print_exc()
        return jsonify({'error': f'Error serving audio: {str(e)}'}), 500

@app.route('/pdf/<path:pdf_name>/image/<int:page_num>', methods=['GET'])
def get_pdf_image(pdf_name, page_num):
    try:
        # Log request details
        print(f"Image request for PDF: {pdf_name}, Page: {page_num}")
        
        # GCS path for the image file
        gcs_path = f"{GCS_IMAGE_PREFIX}{pdf_name}/page_{page_num}.jpg"
        print(f"Checking GCS path: {gcs_path}")
        
        # Check if GCS is available
        gcs_available = get_storage_client() is not None
        gcs_exists = False
        
        if gcs_available:
            # Check if file exists in GCS
            gcs_exists = check_if_file_exists(gcs_path)
            print(f"File exists in GCS: {gcs_exists}")
            
            if gcs_exists:
                try:
                    # Generate a signed URL for the image file
                    signed_url = generate_signed_url(gcs_path, expiration_minutes=30)
                    print(f"Generated signed URL: {signed_url[:50]}...") if signed_url else print("Failed to generate signed URL")
                    
                    if signed_url:
                        return redirect(signed_url)
                except Exception as e:
                    print(f"Error generating signed URL: {str(e)}")
                    traceback.print_exc()
        
        # Fallback to checking local files
        # Try different potential local paths
        possible_paths = [
            os.path.join(UPLOAD_FOLDER, pdf_name, 'image_files', f"{pdf_name}_page_{page_num}.jpg"),  # Standard format
            os.path.join(UPLOAD_FOLDER, pdf_name, 'image_files', f"page_{page_num}.jpg")  # Alternate format
        ]
        
        for image_path in possible_paths:
            print(f"Checking local path: {image_path}")
            if os.path.exists(image_path):
                print(f"Found image at: {image_path}")
                
                # Try to upload to GCS for future requests if GCS is available
                if gcs_available and not gcs_exists:
                    try:
                        print(f"Uploading to GCS: {gcs_path}")
                        result = upload_from_filename(image_path, gcs_path, content_type='image/jpeg')
                        print(f"Upload result: {result}")
                        
                        if result:
                            signed_url = generate_signed_url(gcs_path, expiration_minutes=30)
                            if signed_url:
                                return redirect(signed_url)
                    except Exception as e:
                        print(f"Error uploading image to GCS: {str(e)}")
                        traceback.print_exc()
                
                # Fallback to local file
                print(f"Serving local file: {image_path}")
                return send_file(image_path, mimetype='image/jpeg')

        # If we still haven't found the image, check if a generic image exists in GCS
        if gcs_available:
            generic_path = f"{GCS_IMAGE_PREFIX}{pdf_name}/page_{page_num}.jpg"
            if check_if_file_exists(generic_path) and generic_path != gcs_path:
                print(f"Found generic image in GCS: {generic_path}")
                signed_url = generate_signed_url(generic_path, expiration_minutes=30)
                if signed_url:
                    return redirect(signed_url)
        
        print(f"Image not found for PDF: {pdf_name}, Page: {page_num}")
        return jsonify({'error': 'Image file not found'}), 404
    except Exception as e:
        print(f"Error serving image: {str(e)}")
        traceback.print_exc()
        return jsonify({'error': f'Error serving image: {str(e)}'}), 500

@app.route('/get-audio/<path:filename>', methods=['GET'])
def get_audio(filename):
    # GCS path for the audio file
    gcs_path = f"{GCS_AUDIO_PREFIX}{filename}"
    
    # Check if file exists in GCS
    if check_if_file_exists(gcs_path):
        # Generate a signed URL for the audio file
        signed_url = generate_signed_url(gcs_path, expiration_minutes=30)
        return redirect(signed_url)
    
    # Fallback to searching in local files
    for pdf_folder in os.listdir(UPLOAD_FOLDER):
        audio_path = os.path.join(UPLOAD_FOLDER, pdf_folder, 'audio_files', filename)
        if os.path.exists(audio_path):
            # Upload to GCS for future requests
            try:
                upload_from_filename(audio_path, gcs_path, content_type='audio/mpeg')
                signed_url = generate_signed_url(gcs_path, expiration_minutes=30)
                return redirect(signed_url)
            except Exception as e:
                print(f"Error uploading audio to GCS: {str(e)}")
                # Fallback to local file if upload fails
            return send_file(audio_path, mimetype='audio/mpeg')
    
    return jsonify({'error': 'Audio file not found'}), 404

@app.route('/get-image/<path:filename>', methods=['GET'])
def get_image(filename):
    # GCS path for the image file
    gcs_path = f"{GCS_IMAGE_PREFIX}{filename}"
    
    # Check if file exists in GCS
    if check_if_file_exists(gcs_path):
        # Generate a signed URL for the image file
        signed_url = generate_signed_url(gcs_path, expiration_minutes=30)
        return redirect(signed_url)
    
    # Fallback to searching in local files
    for pdf_folder in os.listdir(UPLOAD_FOLDER):
        image_path = os.path.join(UPLOAD_FOLDER, pdf_folder, 'image_files', filename)
        if os.path.exists(image_path):
            # Upload to GCS for future requests
            try:
                upload_from_filename(image_path, gcs_path, content_type='image/jpeg')
                signed_url = generate_signed_url(gcs_path, expiration_minutes=30)
                return redirect(signed_url)
            except Exception as e:
                print(f"Error uploading image to GCS: {str(e)}")
                # Fallback to local file if upload fails
            return send_file(image_path, mimetype='image/jpeg')
    
    return jsonify({'error': 'Image file not found'}), 404

@app.route('/ask-question', methods=['POST'])
def ask_question():
    try:
        data = request.json
        if not data or 'question' not in data or 'context' not in data:
            return jsonify({'error': 'Missing question or context'}), 400
        
        question = data['question']
        context = data['context']
        pdf_name = data.get('pdf_name', '')
        
        print(f"Received question: '{question}' for PDF: {pdf_name}")
        
        # Check if we have additional context from the PDF in GCS
        additional_context = ""
        if pdf_name:
            try:
                gcs_available = get_storage_client() is not None
                if gcs_available:
                    # Get all text files for this PDF
                    text_prefix = f"{GCS_TEXT_PREFIX}{pdf_name}/"
                    text_files = list_files_with_prefix(text_prefix)
                    
                    if text_files:
                        print(f"Found {len(text_files)} additional context files in GCS")
                        # Get content from up to 3 files (to avoid overloading)
                        for text_file in text_files[:3]:
                            content = download_as_string(text_file)
                            if content:
                                additional_context += content.decode('utf-8') + "\n\n"
                
                # If no GCS content, try local files
                if not additional_context:
                    pdf_folder = os.path.join(UPLOAD_FOLDER, pdf_name)
                    text_folder = os.path.join(pdf_folder, 'text_files')
                    
                    if os.path.exists(text_folder):
                        text_files = [f for f in os.listdir(text_folder) if f.endswith('.md')][:3]
                        print(f"Found {len(text_files)} additional context files locally")
                        
                        for text_file in text_files:
                            with open(os.path.join(text_folder, text_file), 'r') as f:
                                additional_context += f.read() + "\n\n"
            
            except Exception as e:
                print(f"Error getting additional context: {str(e)}")
                # Continue with the original context
        
        # Combine the original context with any additional context
        full_context = context
        if additional_context:
            print("Adding additional context from PDF files")
            full_context = full_context + "\n\n" + additional_context
        
        # Use Gemini to answer the question based on the context
        print("Sending question to Gemini API")
        response = model.generate_content(
            contents=[
                f"""
                # Context: {full_context}
                
                # Question: {question}
                
                # Answer the question based on the provided context. Be comprehensive and accurate.
                # If the answer is not in the context, say "I don't have enough information to answer this question accurately."
                # Don't be afraid to give detailed technical explanations if the question asks for them.
                # Avoid starting with phrases like "Think and Response" or similar templates.
                # Always cite page numbers if you know them.
                """
            ]
        )
        
        # Process the response to remove any unwanted prefixes or formatting issues
        answer_text = response.text.strip()
        
        # Remove "Think and Response" prefix and similar phrases
        answer_text = re.sub(r'^(Think and Response\.?|Based on the context,|According to the context,)\s*', '', answer_text, flags=re.IGNORECASE)
        
        print(f"Generated answer: {answer_text[:100]}...")
        
        return jsonify({
            'answer': answer_text
        })
    
    except Exception as e:
        print(f"Error answering question: {str(e)}")
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/generate-quiz/<path:pdf_name>', methods=['POST'])
def generate_quiz(pdf_name):
    try:
        print(f"Generating quiz for PDF: {pdf_name}")
        
        # Check if we need to verify user authentication
        # (Uncomment this if quiz generation should be restricted to authenticated users)
        # if 'user' not in request or not request.user:
        #     return jsonify({'error': 'User not authenticated'}), 401
        
        # First check if PDF exists in GCS
        gcs_available = get_storage_client() is not None
        gcs_pdf_exists = False
        gcs_quiz_path = None
        
        if gcs_available:
            # Check if PDF exists in GCS
            gcs_pdf_path = f"{GCS_PDF_PREFIX}{pdf_name}/original.pdf"
            gcs_pdf_exists = check_if_file_exists(gcs_pdf_path)
            print(f"PDF exists in GCS: {gcs_pdf_exists}")
            
            # Check if quiz already exists in GCS
            gcs_quiz_path = f"{GCS_QUIZ_PREFIX}{pdf_name}/quiz.json"
            gcs_quiz_exists = check_if_file_exists(gcs_quiz_path)
            print(f"Quiz exists in GCS: {gcs_quiz_exists}")
            
            if gcs_quiz_exists:
                # Return existing quiz from GCS
                print(f"Returning existing quiz from GCS")
                quiz_content = download_as_string(gcs_quiz_path)
                if quiz_content:
                    quiz_data = json.loads(quiz_content)
                    return jsonify(quiz_data)
                    
        # Check local storage for PDF and quiz
        pdf_folder = os.path.join(UPLOAD_FOLDER, pdf_name)
        pdf_exists = os.path.exists(pdf_folder)
        print(f"PDF exists in local storage: {pdf_exists}")
        
        if not pdf_exists and not gcs_pdf_exists:
            print(f"PDF not found: {pdf_name}")
            return jsonify({'error': 'PDF folder not found'}), 404
            
        # Create local quiz folder if needed
        quiz_folder = os.path.join(pdf_folder, 'quiz_data')
        if not os.path.exists(quiz_folder) and pdf_exists:
            os.makedirs(quiz_folder)
            
        # Check if quiz already exists locally
        local_quiz_path = os.path.join(quiz_folder, f"{pdf_name}_quiz.json")
        if os.path.exists(local_quiz_path):
            # Return existing quiz
            print(f"Returning existing quiz from local storage")
            with open(local_quiz_path, 'r') as f:
                quiz_data = json.load(f)
                
                # Upload to GCS if available
                if gcs_available and gcs_quiz_path:
                    try:
                        upload_file(json.dumps(quiz_data), gcs_quiz_path, content_type='application/json')
                        print(f"Uploaded existing quiz to GCS: {gcs_quiz_path}")
                    except Exception as e:
                        print(f"Error uploading quiz to GCS: {str(e)}")
                
            return jsonify(quiz_data)
        
        # Collect explanations for the PDF from both GCS and local storage
        all_explanations = []
        
        # Get explanations from GCS if available
        if gcs_available:
            print(f"Checking GCS for explanations")
            # List all text files in GCS
            text_prefix = f"{GCS_TEXT_PREFIX}{pdf_name}/"
            text_files = list_files_with_prefix(text_prefix)
            
            if text_files:
                # Sort by page number
                text_files.sort(key=lambda f: int(re.search(r'page_(\d+)\.md$', f).group(1)))
                
                # Get content for each file
                for text_file in text_files:
                    try:
                        content = download_as_string(text_file)
                        if content:
                            all_explanations.append(content.decode('utf-8'))
                            print(f"Loaded explanation from GCS: {text_file}")
                    except Exception as e:
                        print(f"Error loading explanation from GCS: {str(e)}")
        
        # Get explanations from local storage if needed
        if not all_explanations and pdf_exists:
            print(f"Checking local storage for explanations")
            text_folder = os.path.join(pdf_folder, 'text_files')
        if os.path.exists(text_folder):
            text_files = sorted([f for f in os.listdir(text_folder) if f.endswith('.md')],
                               key=lambda f: int(f.split('_page_')[1].split('.')[0]))
            
            for text_file in text_files:
                with open(os.path.join(text_folder, text_file), 'r') as f:
                    all_explanations.append(f.read())
                    print(f"Loaded explanation from local file: {text_file}")
        
        # If no text files, use images to regenerate summaries
        if not all_explanations:
            print(f"No explanations found, generating from images")
            image_files = []
            
            # Check GCS for images
            if gcs_available:
                image_prefix = f"{GCS_IMAGE_PREFIX}{pdf_name}/"
                image_files_gcs = list_files_with_prefix(image_prefix)
                
                # Extract page numbers from file paths and sort
                if image_files_gcs:
                    # Convert to list of tuples (file_path, page_number)
                    image_files = []
                    for file_path in image_files_gcs:
                        match = re.search(r'page_(\d+)\.jpg$', file_path)
                        if match:
                            page_num = int(match.group(1))
                            image_files.append((file_path, page_num, True))  # True = GCS file
                            
                    # Sort by page number
                    image_files.sort(key=lambda x: x[1])
            
            # Check local storage for images if needed
            if not image_files and pdf_exists:
                image_folder = os.path.join(pdf_folder, 'image_files')
                if os.path.exists(image_folder):
                    local_image_files = sorted([f for f in os.listdir(image_folder) if f.endswith('.jpg')],
                                    key=lambda f: int(re.search(r'_page_(\d+)\.jpg$', f).group(1) 
                                                    if '_page_' in f 
                                                    else re.search(r'page_(\d+)\.jpg$', f).group(1)))
                    
                    image_files = [(os.path.join(image_folder, f), 
                                  int(re.search(r'_page_(\d+)\.jpg$', f).group(1) 
                                      if '_page_' in f 
                                      else re.search(r'page_(\d+)\.jpg$', f).group(1)), 
                                  False)  # False = local file
                                for f in local_image_files]
            
            # Generate summaries from images
            if image_files:
                print(f"Generating summaries from {len(image_files)} images")
                for file_path, page_num, is_gcs in image_files:
                    try:
                        # Get image data
                        if is_gcs:
                            image_data = download_as_string(file_path)
                        else:
                            with open(file_path, 'rb') as img_file:
                                image_data = img_file.read()
                        
                        if not image_data:
                            print(f"No image data for page {page_num}")
                            continue
                        
                        # Load image
                        image = Image.open(BytesIO(image_data))
                        
                        # Get a brief summary of the page for quiz generation
                        print(f"Generating summary for page {page_num}")
                        response = model.generate_content(
                            contents=[
                                "Provide a comprehensive summary of the key concepts on this page that would be useful for quiz generation.",
                                image
                            ]
                        )
                        
                        all_explanations.append(response.text)
                        print(f"Generated summary for page {page_num}")
                    except Exception as e:
                        print(f"Error generating summary for page {page_num}: {str(e)}")
                        traceback.print_exc()
                        continue
        
        if not all_explanations:
            print(f"No content found to generate quiz")
            return jsonify({'error': 'No content found to generate quiz'}), 404
        
        # Generate quiz based on all explanations
        print(f"Generating quiz from {len(all_explanations)} explanations")
        combined_text = "\n\n".join(all_explanations)
        
        response = model.generate_content(
            contents=[
                f"""
                Based on the following content, generate a quiz with 5 multiple-choice questions to test understanding.
                For each question, provide:
                1. The question text
                2. Four possible answers (A, B, C, D)
                3. The correct answer letter
                4. A brief explanation of why that's the correct answer
                
                Format the output exactly as a JSON array of objects with the following structure:
                [
                  {{
                    "question": "Question text here",
                    "options": ["Option A", "Option B", "Option C", "Option D"],
                    "correctAnswer": "A",
                    "explanation": "Explanation of why A is correct"
                  }},
                  ...
                ]
                
                Make sure to provide 5 questions and use the EXACT format above. Return ONLY valid JSON data, nothing else.
                
                Content:
                {combined_text[:8000]}
                """
            ]
        )
        
        # Extract JSON from response
        quiz_text = response.text
        print(f"Raw response: {quiz_text[:100]}...")
        
        try:
            # Try to extract JSON if it's wrapped in markdown code blocks
            if "```json" in quiz_text:
                quiz_text = quiz_text.split("```json")[1].split("```")[0].strip()
                print("Extracted JSON from markdown code block")
            elif "```" in quiz_text:
                quiz_text = quiz_text.split("```")[1].split("```")[0].strip()
                print("Extracted JSON from code block")
            
            # Try to clean up the JSON to handle common formatting issues
            # Remove potential trailing commas
            quiz_text = re.sub(r',\s*}', '}', quiz_text)
            quiz_text = re.sub(r',\s*]', ']', quiz_text)
            
            print(f"Cleaned JSON: {quiz_text[:100]}...")
            quiz_data = json.loads(quiz_text)
            
            # Validate the quiz data has the right structure
            if not isinstance(quiz_data, list):
                raise ValueError("Quiz data is not a list")
                
            for item in quiz_data:
                if not isinstance(item, dict):
                    raise ValueError("Quiz item is not a dictionary")
                if 'question' not in item or 'options' not in item or 'correctAnswer' not in item:
                    raise ValueError("Quiz item is missing required fields")
            
            # Save quiz to local file if PDF exists locally
            if pdf_exists:
                with open(local_quiz_path, 'w') as f:
                    json.dump(quiz_data, f)
                print(f"Saved quiz to local file: {local_quiz_path}")
            
            # Upload quiz to GCS if available
            if gcs_available and gcs_quiz_path:
                try:
                    upload_file(json.dumps(quiz_data), gcs_quiz_path, content_type='application/json')
                    print(f"Uploaded quiz to GCS: {gcs_quiz_path}")
                except Exception as e:
                    print(f"Error uploading quiz to GCS: {str(e)}")
            
            return jsonify(quiz_data)
        
        except json.JSONDecodeError as e:
            print(f"Error parsing quiz JSON: {str(e)}")
            print(f"Raw quiz text: {quiz_text}")
            traceback.print_exc()
            
            # Try one more time with a simpler prompt
            try:
                print("Trying again with simpler prompt")
                simple_response = model.generate_content(
                    contents=[
                        f"""
                        Generate a JSON array of 3 quiz questions about the following content. 
                        Each question should have a 'question' field, an 'options' array with 4 choices, 
                        a 'correctAnswer' field with the letter (A, B, C or D), and an 'explanation' field.
                        Return ONLY valid JSON, nothing else.
                        
                        Content:
                        {combined_text[:4000]}
                        """
                    ]
                )
                
                simple_text = simple_response.text
                if "```json" in simple_text:
                    simple_text = simple_text.split("```json")[1].split("```")[0].strip()
                elif "```" in simple_text:
                    simple_text = simple_text.split("```")[1].split("```")[0].strip()
                
                simple_text = re.sub(r',\s*}', '}', simple_text)
                simple_text = re.sub(r',\s*]', ']', simple_text)
                
                simple_data = json.loads(simple_text)
                
                # Save and upload the quiz data
                if pdf_exists:
                    with open(local_quiz_path, 'w') as f:
                        json.dump(simple_data, f)
                
                if gcs_available and gcs_quiz_path:
                    upload_file(json.dumps(simple_data), gcs_quiz_path, content_type='application/json')
                
                return jsonify(simple_data)
            
            except Exception as backup_error:
                print(f"Second attempt also failed: {str(backup_error)}")
                traceback.print_exc()
            return jsonify({'error': 'Failed to generate valid quiz format'}), 500
        
    except Exception as e:
        print(f"Error generating quiz: {str(e)}")
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/download-materials/<path:pdf_name>', methods=['GET'])
def download_materials(pdf_name):
    try:
        pdf_folder = os.path.join(UPLOAD_FOLDER, pdf_name)
        if not os.path.exists(pdf_folder):
            return jsonify({'error': 'PDF folder not found'}), 404
            
        # Create in-memory zip file
        memory_file = io.BytesIO()
        with zipfile.ZipFile(memory_file, 'w') as zf:
            # Add all related files
            for folder_name in ['audio_files', 'image_files', 'text_files']:
                folder_path = os.path.join(pdf_folder, folder_name)
                if os.path.exists(folder_path):
                    for file in os.listdir(folder_path):
                        file_path = os.path.join(folder_path, file)
                        if os.path.isfile(file_path):
                            zf.write(file_path, f"{folder_name}/{file}")
            
            # Add quiz if it exists
            quiz_folder = os.path.join(pdf_folder, 'quiz_data')
            if os.path.exists(quiz_folder):
                for file in os.listdir(quiz_folder):
                    file_path = os.path.join(quiz_folder, file)
                    if os.path.isfile(file_path):
                        zf.write(file_path, f"quiz_data/{file}")
            
            # Add original PDF file if it exists
            original_pdf_path = os.path.join(pdf_folder, 'original.pdf')
            if os.path.exists(original_pdf_path):
                zf.write(original_pdf_path, f"{pdf_name}.pdf")
            
            # Add metadata file if it exists
            metadata_path = os.path.join(pdf_folder, 'metadata.json')
            if os.path.exists(metadata_path):
                zf.write(metadata_path, "metadata.json")
        
        # Seek to the beginning of the stream
        memory_file.seek(0)
        
        return send_file(
            memory_file,
            mimetype='application/zip',
            as_attachment=True,
            download_name=f"{pdf_name}_study_materials.zip"
        )
    
    except Exception as e:
        print(f"Error creating zip file: {str(e)}")
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

# Process PDF with deduplication
@app.route('/process-pdf', methods=['POST'])
@login_required
def process_pdf():
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    
    if not file.filename.endswith('.pdf'):
        return jsonify({'error': 'File must be a PDF'}), 400
    
    # Get the difficulty level from the form data, default to "detailed" if not provided
    difficulty_level = request.form.get('difficulty_level', 'detailed')
    print(f"Processing PDF with difficulty level: {difficulty_level}")

    # Get PDF name without extension for saving files
    original_pdf_name = os.path.splitext(os.path.basename(file.filename))[0]
    
    # Clean the PDF name (replace spaces with underscores, remove special characters)
    pdf_name = re.sub(r'[^\w\-]', '_', original_pdf_name).lower()
    
    # Get the current user's ID
    user_id = request.user['user_id']
    
    # Create a temp file for PDF processing
    with NamedTemporaryFile(delete=False, suffix='.pdf') as temp_file:
        temp_pdf_path = temp_file.name
        file.save(temp_pdf_path)
        print(f"PDF temporarily saved to {temp_pdf_path}")
    
    # Calculate the file's hash to check for duplicates
    pdf_hash = calculate_file_hash(temp_pdf_path)
    print(f"PDF hash: {pdf_hash}")
    
    # Check if this exact PDF has been uploaded before
    existing_pdf = get_pdf_by_hash(pdf_hash)
    
    if existing_pdf:
        print(f"PDF with hash {pdf_hash} already exists in the database")
        
        # Associate this PDF with the current user
        associate_pdf_with_user(user_id, existing_pdf['pdf_id'])
        
        # Clean up the temporary file
        os.remove(temp_pdf_path)
        
        # Use the existing PDF path
        pdf_name = existing_pdf['file_path']
        
        # Return a special response to tell the frontend to use the existing PDF
        return jsonify({
            'type': 'existing',
            'pdf_name': pdf_name,
            'message': 'PDF already exists, associating with your account'
        })
    
    # This is a new PDF that needs to be processed
    
    # Create a unique name for this PDF
    # Create a unique id for GCS paths
    pdf_id = f"{pdf_name}_{int(time.time())}"
    
    # Create local folders for temporary processing
    temp_folder = os.path.join(UPLOAD_FOLDER, pdf_id)
    os.makedirs(temp_folder, exist_ok=True)
    
    # Save metadata
    metadata = {
        'date_processed': time.strftime('%Y-%m-%d %H:%M:%S'),
        'original_filename': file.filename,
        'difficulty_level': difficulty_level,
        'user_id': user_id
    }
    
    # Upload original PDF to GCS
    gcs_pdf_path = f"{GCS_PDF_PREFIX}{pdf_id}/original.pdf"
    try:
        upload_from_filename(temp_pdf_path, gcs_pdf_path, content_type='application/pdf')
        print(f"PDF uploaded to GCS: {gcs_pdf_path}")
    except Exception as e:
        print(f"Error uploading PDF to GCS: {str(e)}")
        traceback.print_exc()
        # Continue with local processing if GCS upload fails
    
    # Upload metadata to GCS
    gcs_metadata_path = f"{GCS_PDF_PREFIX}{pdf_id}/metadata.json"
    try:
        upload_file(json.dumps(metadata), gcs_metadata_path, content_type='application/json')
    except Exception as e:
        print(f"Error uploading metadata to GCS: {str(e)}")
        traceback.print_exc()
    
    # Get file size
    file_size = os.path.getsize(temp_pdf_path)

    def generate():
        try:
            # Convert PDF to images
            print("Converting PDF to images...")
            images = convert_from_path(temp_pdf_path)
            page_count = len(images)
            print(f"Converted {page_count} pages")
            
            # Add the PDF to the database
            pdf_db_id = add_pdf(
                title=original_pdf_name,
                file_path=pdf_id,
                pdf_hash=pdf_hash,
                file_size=file_size,
                page_count=page_count
            )
            
            # Associate the PDF with the user
            associate_pdf_with_user(user_id, pdf_db_id)
            
            # Send total page count to frontend
            yield json.dumps({
                'type': 'info',
                'total_pages': page_count,
                'pdf_name': pdf_id
            }) + '\n'

            for i, image in enumerate(images):
                page_number = i + 1
                print(f"Processing page {page_number}...")
                
                # Calculate progress based on the current page
                # Map progress from 30-95% (upload was 0-30%, final processing will be 95-100%)
                progress_percentage = 30 + int((page_number - 1) * 65 / page_count)
                
                # Send progress update
                yield json.dumps({
                    'type': 'progress',
                    'progress': progress_percentage,
                    'page': page_number,
                    'total_pages': page_count
                }) + '\n'
                
                # Save image temporarily for processing
                img_filename = f"page_{page_number}.jpg"
                temp_img_path = os.path.join(temp_folder, img_filename)
                image.save(temp_img_path, 'JPEG')
                print(f"Image temporarily saved to {temp_img_path}")

                # Upload image to GCS
                gcs_img_path = f"{GCS_IMAGE_PREFIX}{pdf_id}/page_{page_number}.jpg"
                try:
                    upload_from_filename(temp_img_path, gcs_img_path, content_type='image/jpeg')
                    print(f"Image uploaded to GCS: {gcs_img_path}")
                except Exception as e:
                    print(f"Error uploading image to GCS: {str(e)}")
                    traceback.print_exc()

                # Convert image to base64 for sending to frontend
                buffered = BytesIO()
                image.save(buffered, format="JPEG")
                img_str = base64.b64encode(buffered.getvalue()).decode()

                # Get AI explanation
                print(f"Getting AI explanation for page {page_number}...")
                try:
                    response = model.generate_content(
                        contents=[
                            f"Please explain this page in {difficulty_level}, including any formulas or mathematical expressions. Make sure to explain them in a way that would be easy to read aloud. Give a '.' after a long pause and a ';' after a medium pause based on the importance of the words. Preserve all formatting, including paragraph breaks. Also dont use any sub scripting symbols or special characters, instead read it aloud. Dont repeat content from the previous page and useless information in the header and footer.",
                            image
                        ]
                    )
                    
                    explanation = response.text
                    print(f"AI explanation received for page {page_number}")
                    
                    # Save explanation as Markdown file
                    text_path = os.path.join(temp_folder, f"{pdf_id}_page_{page_number}.md")
                    with open(text_path, 'w') as f:
                        f.write(explanation)
                    
                    # Upload explanation to GCS
                    gcs_text_path = f"{GCS_TEXT_PREFIX}{pdf_id}/page_{page_number}.md"
                    print(f"Explanation content type: {type(explanation).__name__}, length: {len(explanation)}")
                    upload_file(explanation, gcs_text_path, content_type='text/markdown')
                    print(f"Explanation uploaded to GCS: {gcs_text_path}")
                    
                except Exception as e:
                    print(f"Error generating explanation for page {page_number}: {str(e)}")
                    explanation = f"Failed to generate explanation for page {page_number}: {str(e)}"
                    
                    # Save error message
                    text_path = os.path.join(temp_folder, f"{pdf_id}_page_{page_number}.md")
                    with open(text_path, 'w') as f:
                        f.write(explanation)

                # Generate audio using Google Text-to-Speech API (REST API with API key)
                print(f"Generating audio for page {page_number}...")
                try:
                    # Use the Google Cloud Text-to-Speech REST API directly with API key
                    tts_url = f"https://texttospeech.googleapis.com/v1/text:synthesize?key={GOOGLE_API_KEY}"
                    
                    # Remove special markdown characters for speech but keep for display
                    speech_text = re.sub(r'\*\*(.*?)\*\*', r'\1', explanation)
                    speech_text = speech_text.replace("*", "")
                    
                    # Ensure the text is not empty
                    if not speech_text.strip():
                        speech_text = f"Page {page_number} content could not be processed properly."
                    
                    # If speech text is too long, truncate it to avoid API limits
                    if len(speech_text) > 5000:
                        print(f"Warning: Speech text is very long ({len(speech_text)} chars), truncating...")
                        speech_text = speech_text[:5000] + "... The rest of the content has been truncated for processing."
                    
                    payload = {
                        "input": {
                            "text": speech_text
                        },
                        "voice": {
                            "languageCode": "en-IN",
                            "name": "en-IN-Chirp3-HD-Achernar", 
                            "ssmlGender": "NEUTRAL"
                        },
                        "audioConfig": {
                            "audioEncoding": "MP3",
                            "effectsProfileId": [
                                "large-automotive-class-device"
                            ],
                            "speakingRate": 1
                        }
                    }
                    
                    print(f"Sending TTS request for page {page_number}...")
                    tts_response = requests.post(tts_url, json=payload)
                    tts_response.raise_for_status()  # Will raise an exception for 4XX/5XX responses
                    
                    # Log the response
                    print(f"TTS response status: {tts_response.status_code}")
                    if tts_response.status_code != 200:
                        print(f"TTS error response: {tts_response.text}")
                    
                    # Extract audio content from response
                    response_json = tts_response.json()
                    if "audioContent" not in response_json:
                        print(f"Error: No audioContent in TTS response. Response: {response_json}")
                        raise Exception("No audioContent in TTS response")
                        
                    audio_data = base64.b64decode(response_json["audioContent"])
                    
                    # Check if audio data is valid
                    if len(audio_data) == 0:
                        print(f"Error: Empty audio data returned from TTS API")
                        raise Exception("Empty audio data returned from TTS API")
                    
                    print(f"Received audio data: {len(audio_data)} bytes")
                    
                    # Make sure the audio directory exists
                    audio_dir = os.path.join(temp_folder, 'audio_files')
                    os.makedirs(audio_dir, exist_ok=True)
                    
                    # Save the audio file locally
                    local_audio_path = os.path.join(audio_dir, f"{pdf_id}_page_{page_number}.mp3")
                    with open(local_audio_path, 'wb') as f:
                        f.write(audio_data)
                    print(f"Audio saved locally to {local_audio_path}, size: {len(audio_data)} bytes")
                    
                    # Upload audio to GCS
                    gcs_audio_path = f"{GCS_AUDIO_PREFIX}{pdf_id}/page_{page_number}.mp3"
                    try:
                        upload_file(audio_data, gcs_audio_path, content_type='audio/mpeg')
                        print(f"Audio uploaded to GCS: {gcs_audio_path}")
                    except Exception as e:
                        print(f"Error uploading audio to GCS: {str(e)}")
                        traceback.print_exc()
                    
                except Exception as e:
                    print(f"Error generating audio: {str(e)}")
                    traceback.print_exc()
                    # Return a dummy audio string in case of error
                    audio_data = b""

                # Convert audio to base64 for sending to frontend
                audio_str = base64.b64encode(audio_data).decode()

                # Send this page's result to frontend immediately
                yield json.dumps({
                    'type': 'page',
                    'page_data': {
                        'page_number': page_number,
                        'image': img_str,
                        'explanation': explanation,
                        'audio': audio_str,
                        'audio_url': f"/pdf/{pdf_id}/audio/{page_number}",
                        'image_url': f"/pdf/{pdf_id}/image/{page_number}"
                    }
                }) + '\n'

            # Clean up temporary files
            try:
                shutil.rmtree(temp_folder)
                os.remove(temp_pdf_path)
                print(f"Temporary files cleaned up")
            except Exception as e:
                print(f"Error cleaning up temporary files: {str(e)}")
                traceback.print_exc()

            # Send completion message
            yield json.dumps({
                'type': 'complete',
                'message': 'All pages processed successfully',
                'pdf_name': pdf_id
            }) + '\n'

        except Exception as e:
            print(f"Error processing PDF: {str(e)}")
            traceback.print_exc()
            # Send error message
            yield json.dumps({
                'type': 'error',
                'error': str(e)
            }) + '\n'

    # Return a streaming response
    return Response(generate(), mimetype='text/plain')

# Test route for GCS connection
@app.route('/test-gcs', methods=['GET'])
def test_gcs():
    try:
        client = get_storage_client()
        if client is None:
            return jsonify({
                'success': False,
                'message': 'Failed to create storage client. Check GOOGLE_APPLICATION_CREDENTIALS environment variable.'
            }), 500
            
        bucket_name = os.getenv('GCS_BUCKET_NAME', 'studybuddy-pdf-storage')
        bucket = client.bucket(bucket_name)
        
        # Test if we can list blobs in the bucket
        blobs = list(bucket.list_blobs(max_results=5))
        
        return jsonify({
            'success': True,
            'message': 'Successfully connected to GCS bucket',
            'bucket_name': bucket_name,
            'files_sample': [blob.name for blob in blobs][:5]
        })
        
    except Exception as e:
        return jsonify({
            'success': False,
            'message': f'Error connecting to GCS: {str(e)}'
        }), 500

if __name__ == '__main__':
    # app.run(port=5000, debug=True)
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)