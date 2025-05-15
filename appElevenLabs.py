from flask import Flask, request, jsonify, Response, send_file
from flask_cors import CORS
from pdf2image import convert_from_path
import os
from PIL import Image
from google import genai
from elevenlabs.client import ElevenLabs
import base64
from io import BytesIO
import time
import traceback
import json
from dotenv import load_dotenv
import shutil
import re
import zipfile
import io

# # Configure API keys
ELEVENLABS_API_KEY = os.getenv('ELEVENLABS_API_KEY')
GOOGLE_API_KEY = os.getenv('GEMINI_API_KEY')

# # Configure ElevenLabs client
eleven_client = ElevenLabs(api_key=ELEVENLABS_API_KEY)

load_dotenv()

app = Flask(__name__)
CORS(app)

# Configure API keys
GOOGLE_API_KEY = os.getenv('GEMINI_API_KEY')
print(f"Google API Key: {GOOGLE_API_KEY[:10]}...")

# Configure Google Generative AI
genai_client = genai.Client(api_key=GOOGLE_API_KEY)

# Main upload folder
UPLOAD_FOLDER = 'uploads'
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

# Route to get a list of existing PDFs
@app.route('/existing-pdfs', methods=['GET'])
def get_existing_pdfs():
    try:
        pdfs = []
        # Check all folders in uploads directory
        for folder in os.listdir(UPLOAD_FOLDER):
            folder_path = os.path.join(UPLOAD_FOLDER, folder)
            if os.path.isdir(folder_path):
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
                    
                    # Count pages
                    total_pages = 0
                    image_folder = os.path.join(folder_path, 'image_files')
                    if os.path.exists(image_folder):
                        for file in os.listdir(image_folder):
                            if file.endswith('.jpg'):
                                total_pages += 1
                    
                    pdfs.append({
                        'name': folder,
                        'total_pages': total_pages,
                        'date_processed': metadata.get('date_processed', 'Unknown'),
                        'original_filename': metadata.get('original_filename', folder)
                    })
        
        print(f"Found {len(pdfs)} existing PDFs")
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
        
        # Check if this base name exists
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
        pdf_folder = os.path.join(UPLOAD_FOLDER, pdf_name)
        
        if not os.path.exists(pdf_folder) or not os.path.isdir(pdf_folder):
            print(f"Error: PDF folder not found at {pdf_folder}")
            return jsonify({'error': 'PDF not found'}), 404
        
        print(f"Loading PDF from {pdf_folder}")
        
        # Load pages
        image_folder = os.path.join(pdf_folder, 'image_files')
        text_folder = os.path.join(pdf_folder, 'text_files')
        audio_folder = os.path.join(pdf_folder, 'audio_files')
        
        if not os.path.exists(image_folder) or not os.path.exists(text_folder) or not os.path.exists(audio_folder):
            print(f"Error: PDF structure is incomplete. Missing folders in {pdf_folder}")
            return jsonify({'error': 'PDF structure is incomplete'}), 400
        
        # Get page count
        total_pages = 0
        pages = []
        
        page_files = []
        for file in os.listdir(image_folder):
            if file.endswith('.jpg') and '_page_' in file:
                page_files.append(file)
        
        print(f"Found {len(page_files)} page files")
        
        # Sort page files by page number
        page_files.sort(key=lambda f: int(f.split('_page_')[1].split('.')[0]))
        
        for page_file in page_files:
            page_num = int(page_file.split('_page_')[1].split('.')[0])
            total_pages = max(total_pages, page_num)
            
            print(f"Processing page {page_num}")
            
            # Get image
            img_path = os.path.join(image_folder, page_file)
            with open(img_path, 'rb') as img_file:
                img_data = base64.b64encode(img_file.read()).decode()
            
            # Get text
            text_path = os.path.join(text_folder, page_file.replace('.jpg', '.md'))
            explanation = ""
            if os.path.exists(text_path):
                with open(text_path, 'r') as text_file:
                    explanation = text_file.read()
            
            # Get audio
            audio_path = os.path.join(audio_folder, page_file.replace('.jpg', '.mp3'))
            audio_data = b""
            if os.path.exists(audio_path):
                with open(audio_path, 'rb') as audio_file:
                    audio_data = audio_file.read()
            
            audio_str = base64.b64encode(audio_data).decode()
            
            pages.append({
                'page_number': page_num,
                'image': img_data,
                'explanation': explanation,
                'audio': audio_str,
                'audio_url': f"/pdf/{pdf_name}/audio/{page_num}",
                'image_url': f"/pdf/{pdf_name}/image/{page_num}"
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
    audio_path = os.path.join(UPLOAD_FOLDER, pdf_name, 'audio_files', f"{pdf_name}_page_{page_num}.mp3")
    if not os.path.exists(audio_path):
        return jsonify({'error': 'Audio file not found'}), 404
    return send_file(audio_path, mimetype='audio/mpeg')

@app.route('/pdf/<path:pdf_name>/image/<int:page_num>', methods=['GET'])
def get_pdf_image(pdf_name, page_num):
    image_path = os.path.join(UPLOAD_FOLDER, pdf_name, 'image_files', f"{pdf_name}_page_{page_num}.jpg")
    if not os.path.exists(image_path):
        return jsonify({'error': 'Image file not found'}), 404
    return send_file(image_path, mimetype='image/jpeg')

@app.route('/audio/<path:filename>', methods=['GET'])
def get_audio(filename):
    # This route is kept for backward compatibility
    for pdf_folder in os.listdir(UPLOAD_FOLDER):
        audio_path = os.path.join(UPLOAD_FOLDER, pdf_folder, 'audio_files', filename)
        if os.path.exists(audio_path):
            return send_file(audio_path, mimetype='audio/mpeg')
    
    return jsonify({'error': 'Audio file not found'}), 404

@app.route('/image/<path:filename>', methods=['GET'])
def get_image(filename):
    # This route is kept for backward compatibility
    for pdf_folder in os.listdir(UPLOAD_FOLDER):
        image_path = os.path.join(UPLOAD_FOLDER, pdf_folder, 'image_files', filename)
        if os.path.exists(image_path):
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
        
        # Use Gemini to answer the question based on the context
        response = genai_client.models.generate_content(
            model="gemini-1.5-pro",
            contents=[
                f"""
                Context: {context}
                
                Question: {question}
                
                Answer the question based only on the provided context. If the answer is not in the context, 
                say 'I don't have enough information to answer this question based on the provided content.'
                """
            ]
        )
        
        return jsonify({
            'answer': response.text
        })
    
    except Exception as e:
        print(f"Error answering question: {str(e)}")
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/generate-quiz/<path:pdf_name>', methods=['POST'])
def generate_quiz(pdf_name):
    try:
        pdf_folder = os.path.join(UPLOAD_FOLDER, pdf_name)
        if not os.path.exists(pdf_folder):
            return jsonify({'error': 'PDF folder not found'}), 404
            
        # Create quiz folder if it doesn't exist
        quiz_folder = os.path.join(pdf_folder, 'quiz_data')
        if not os.path.exists(quiz_folder):
            os.makedirs(quiz_folder)
            
        # Check if quiz already exists
        quiz_path = os.path.join(quiz_folder, f"{pdf_name}_quiz.json")
        
        if os.path.exists(quiz_path):
            # Return existing quiz
            with open(quiz_path, 'r') as f:
                quiz_data = json.load(f)
            return jsonify(quiz_data)
        
        # Collect all explanations for the PDF
        text_folder = os.path.join(pdf_folder, 'text_files')
        image_folder = os.path.join(pdf_folder, 'image_files')
        all_explanations = []
        
        # Try using text files first
        if os.path.exists(text_folder):
            text_files = sorted([f for f in os.listdir(text_folder) if f.endswith('.md')],
                               key=lambda f: int(f.split('_page_')[1].split('.')[0]))
            
            for text_file in text_files:
                with open(os.path.join(text_folder, text_file), 'r') as f:
                    all_explanations.append(f.read())
        
        # If no text files, use images to regenerate summaries
        if not all_explanations and os.path.exists(image_folder):
            image_files = sorted([f for f in os.listdir(image_folder) if f.endswith('.jpg')],
                                key=lambda f: int(f.split('_page_')[1].split('.')[0]))
            
            for image_file in image_files:
                try:
                    with open(os.path.join(image_folder, image_file), 'rb') as img_file:
                        image_data = img_file.read()
                    
                    image = Image.open(BytesIO(image_data))
                    
                    # Get a brief summary of the page for quiz generation
                    response = genai_client.models.generate_content(
                        model="gemini-1.5-pro",
                        contents=[
                            "Provide a brief summary of the key concepts on this page that would be useful for quiz generation.",
                            image
                        ]
                    )
                    
                    all_explanations.append(response.text)
                except Exception as e:
                    print(f"Error getting summary for page {image_file}: {str(e)}")
        
        if not all_explanations:
            return jsonify({'error': 'No content found to generate quiz'}), 404
        
        # Generate quiz based on all explanations
        combined_text = "\n\n".join(all_explanations)
        
        response = genai_client.models.generate_content(
            model="gemini-1.5-pro",
            contents=[
                f"""
                Based on the following content, generate a quiz with 5 multiple-choice questions to test understanding.
                For each question, provide:
                1. The question text
                2. Four possible answers (A, B, C, D)
                3. The correct answer letter
                4. A brief explanation of why that's the correct answer
                
                Format the output as a JSON array of objects with the following structure:
                [
                  {{
                    "question": "Question text here",
                    "options": ["Option A", "Option B", "Option C", "Option D"],
                    "correctAnswer": "A",
                    "explanation": "Explanation of why A is correct"
                  }},
                  ...
                ]
                
                Content:
                {combined_text}
                """
            ]
        )
        
        # Extract JSON from response
        quiz_text = response.text
        try:
            # Try to extract JSON if it's wrapped in markdown code blocks
            if "```json" in quiz_text:
                quiz_text = quiz_text.split("```json")[1].split("```")[0].strip()
            elif "```" in quiz_text:
                quiz_text = quiz_text.split("```")[1].split("```")[0].strip()
            
            quiz_data = json.loads(quiz_text)
            
            # Save quiz to file
            with open(quiz_path, 'w') as f:
                json.dump(quiz_data, f)
            
            return jsonify(quiz_data)
        
        except json.JSONDecodeError as e:
            print(f"Error parsing quiz JSON: {str(e)}")
            print(f"Raw quiz text: {quiz_text}")
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

@app.route('/process-pdf', methods=['POST'])
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
    
    # Check if this PDF has already been processed
    existing_pdf_path = os.path.join(UPLOAD_FOLDER, pdf_name)
    if os.path.exists(existing_pdf_path):
        # Find a new unique name by appending a number
        counter = 2
        while os.path.exists(os.path.join(UPLOAD_FOLDER, f"{pdf_name}_{counter}")):
            counter += 1
        pdf_name = f"{pdf_name}_{counter}"
    
    # Create the PDF folder and subfolders
    pdf_folder = os.path.join(UPLOAD_FOLDER, pdf_name)
    os.makedirs(pdf_folder, exist_ok=True)
    
    image_folder = os.path.join(pdf_folder, 'image_files')
    text_folder = os.path.join(pdf_folder, 'text_files')
    audio_folder = os.path.join(pdf_folder, 'audio_files')
    
    os.makedirs(image_folder, exist_ok=True)
    os.makedirs(text_folder, exist_ok=True)
    os.makedirs(audio_folder, exist_ok=True)
    
    # Save metadata
    metadata = {
        'date_processed': time.strftime('%Y-%m-%d %H:%M:%S'),
        'original_filename': file.filename
    }
    with open(os.path.join(pdf_folder, 'metadata.json'), 'w') as f:
        json.dump(metadata, f)

    # Save the PDF temporarily
    temp_pdf_path = os.path.join(pdf_folder, 'original.pdf')
    file.save(temp_pdf_path)
    print(f"PDF saved to {temp_pdf_path}")

    def generate():
        try:
            # Convert PDF to images
            print("Converting PDF to images...")
            images = convert_from_path(temp_pdf_path)
            print(f"Converted {len(images)} pages")
            
            # Send total page count to frontend
            yield json.dumps({
                'type': 'info',
                'total_pages': len(images),
                'pdf_name': pdf_name
            }) + '\n'

            for i, image in enumerate(images):
                print(f"Processing page {i+1}...")
                # Save image with proper naming
                img_filename = f"{pdf_name}_page_{i+1}.jpg"
                img_path = os.path.join(image_folder, img_filename)
                image.save(img_path, 'JPEG')
                print(f"Image saved to {img_path}")

                # Convert image to base64 for sending to frontend
                buffered = BytesIO()
                image.save(buffered, format="JPEG")
                img_str = base64.b64encode(buffered.getvalue()).decode()

                # Get AI explanation
                print(f"Getting AI explanation for page {i+1}...")
                try:
                    response = genai_client.models.generate_content(
                        model="gemini-1.5-pro",
                        contents=[
                            f"Please explain this page in {difficulty_level}, including any formulas or mathematical expressions. Make sure to explain them in a way that would be easy to read aloud. Preserve all formatting, including paragraph breaks.",
                            image
                        ]
                    )
                    
                    explanation = response.text
                    print(f"AI explanation received for page {i+1}")
                    
                    # Save the explanation as markdown
                    text_path = os.path.join(text_folder, f"{pdf_name}_page_{i+1}.md")
                    with open(text_path, 'w') as f:
                        f.write(explanation)
                    print(f"Explanation saved to {text_path}")
                    
                except Exception as e:
                    print(f"Error getting AI explanation: {str(e)}")
                    traceback.print_exc()
                    explanation = f"Error analyzing page {i+1}: {str(e)}"

                # Generate audio using Google Text-to-Speech API (REST API with API key)
                print(f"Generating audio for page {i+1}...")
                try:
                    # Use the correct API format for text-to-speech
                    audio_generator = eleven_client.text_to_speech.convert(
                        text=explanation,
                        voice_id="HobRzuqtLputbKAXOdTj",  # Use the voice ID instead of name for "Harsh"
                        model_id="eleven_multilingual_v2",  # Use the correct model ID
                        output_format="mp3_44100_128"  # Specify output format
                    )
                    
                    # Collect all audio data from the generator
                    audio_data = b""
                    for chunk in audio_generator:
                        if chunk:
                            audio_data += chunk
                    audio_filename = f"{pdf_name}_page_{i+1}.mp3"
                    audio_path = os.path.join(audio_folder, audio_filename)
                    with open(audio_path, "wb") as audio_file:
                        audio_file.write(audio_data)
                    
                    print(f"Audio generated and saved to {audio_path}")
                    print(f"Audio generated for page {i+1}")
                except Exception as e:
                    print(f"Error generating audio: {str(e)}")
                    traceback.print_exc()
                    # Return a dummy audio string in case of error
                    audio_data = b""
                    audio_filename = ""
                # Convert audio to base64
                audio_str = base64.b64encode(audio_data).decode()

                # Send this page's result to frontend immediately
                yield json.dumps({
                    'type': 'page',
                    'page_data': {
                        'page_number': i + 1,
                        'image': img_str,
                        'explanation': explanation,
                        'audio': audio_str,
                        'audio_url': f"/pdf/{pdf_name}/audio/{i+1}",
                        'image_url': f"/pdf/{pdf_name}/image/{i+1}"
                    }
                }) + '\n'

            # Send completion message
            yield json.dumps({
                'type': 'complete',
                'message': 'All pages processed successfully',
                'pdf_name': pdf_name
            }) + '\n'

        except Exception as e:
            print(f"Error processing PDF: {str(e)}")
            traceback.print_exc()
            yield json.dumps({
                'type': 'error',
                'error': str(e)
            }) + '\n'

    return Response(generate(), mimetype='text/event-stream')

if __name__ == '__main__':
    app.run(debug=True)