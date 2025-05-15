from google.cloud import storage
import os
import datetime
import uuid

# Default bucket name - should be set in environment variable in production
DEFAULT_BUCKET_NAME = os.getenv('GCS_BUCKET_NAME', 'studybuddy-pdf-storage')

def get_storage_client():
    """Get a Google Cloud Storage client."""
    try:
        # Get credentials file path from environment variable
        credentials_path = os.getenv('GOOGLE_APPLICATION_CREDENTIALS')
        
        if not credentials_path:
            print("No Google Cloud credentials path set in environment variables")
            return None
            
        if not os.path.exists(credentials_path):
            print(f"Credentials file not found at {credentials_path}")
            print("Please make sure the file exists at the specified path.")
            return None
            
        print(f"Using credentials from: {credentials_path}")
        return storage.Client.from_service_account_json(credentials_path)
    except Exception as e:
        print(f"Error creating storage client: {str(e)}")
        print("WARNING: GCS credentials not found - falling back to local storage")
        # Return None to indicate that GCS is not available
        return None

def create_bucket_if_not_exists(bucket_name=DEFAULT_BUCKET_NAME):
    """Create a Google Cloud Storage bucket if it doesn't exist."""
    client = get_storage_client()
    
    if client is None:
        print("GCS not available, skipping bucket creation")
        return None
    
    if not client.lookup_bucket(bucket_name):
        bucket = client.create_bucket(bucket_name)
        
        # Set CORS policy for direct browser access
        bucket.cors = [
            {
                "origin": ["*"],  # Replace with your domain in production
                "responseHeader": ["Content-Type", "x-goog-resumable"],
                "method": ["GET", "HEAD", "PUT", "POST"],
                "maxAgeSeconds": 3600
            }
        ]
        bucket.patch()
        
        print(f"Created bucket {bucket_name}")
    else:
        print(f"Bucket {bucket_name} already exists")
    
    return client.bucket(bucket_name)

def upload_file(file_content, file_path, bucket_name=DEFAULT_BUCKET_NAME, content_type=None):
    """Upload a file to Google Cloud Storage.
    
    Args:
        file_content: The content of the file (bytes or string)
        file_path: The path where the file will be stored in the bucket
        bucket_name: The name of the bucket
        content_type: The content type of the file
        
    Returns:
        The public URL of the uploaded file or None if upload failed
    """
    client = get_storage_client()
    
    if client is None:
        print(f"GCS not available, skipping upload of {file_path}")
        return None
    
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(file_path)
    
    print(f"Uploading file to GCS: path={file_path}, content_type={content_type}, content_is_string={isinstance(file_content, str)}")
    
    if content_type:
        blob.content_type = content_type
        print(f"Set content type for {file_path} to {content_type}")
    
    try:
        if isinstance(file_content, str):
            # For string content, explicitly set the content type
            print(f"Uploading string content of length {len(file_content)} with content_type={content_type}")
            if content_type and content_type.startswith('text/'):
                # For text content types, encode to bytes and set content type
                blob.upload_from_string(file_content, content_type=content_type)
            else:
                # For other content types, default behavior
                blob.upload_from_string(file_content, content_type=content_type)
        else:
            # For binary content
            print(f"Uploading binary content with content_type={content_type}")
            blob.upload_from_string(file_content, content_type=content_type)
        
        print(f"Successfully uploaded {file_path} to GCS")
        return blob.name
    except Exception as e:
        print(f"Error uploading {file_path} to GCS: {str(e)}")
        import traceback
        traceback.print_exc()
        return None

def upload_from_file(file_object, file_path, bucket_name=DEFAULT_BUCKET_NAME, content_type=None):
    """Upload a file object to Google Cloud Storage."""
    client = get_storage_client()
    
    if client is None:
        print(f"GCS not available, skipping upload of {file_path}")
        return None
    
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(file_path)
    
    if content_type:
        blob.content_type = content_type
    
    try:
        blob.upload_from_file(file_object, content_type=content_type)
        print(f"Successfully uploaded {file_path} to GCS")
        return blob.name
    except Exception as e:
        print(f"Error uploading {file_path} to GCS: {str(e)}")
        return None

def upload_from_filename(local_file_path, file_path, bucket_name=DEFAULT_BUCKET_NAME, content_type=None):
    """Upload a file from local path to Google Cloud Storage."""
    client = get_storage_client()
    
    if client is None:
        print(f"GCS not available, skipping upload of {file_path}")
        return None
    
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(file_path)
    
    if content_type:
        blob.content_type = content_type
    
    try:
        blob.upload_from_filename(local_file_path)
        print(f"Successfully uploaded {file_path} to GCS from {local_file_path}")
        return blob.name
    except Exception as e:
        print(f"Error uploading {file_path} to GCS from {local_file_path}: {str(e)}")
        return None

def generate_signed_url(file_path, bucket_name=DEFAULT_BUCKET_NAME, expiration_minutes=15):
    """Generate a signed URL for temporary access to a file."""
    client = get_storage_client()
    
    if client is None:
        print(f"GCS not available, cannot generate signed URL for {file_path}")
        return None
    
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(file_path)
    
    try:
        # First check if blob exists
        if not blob.exists():
            print(f"File does not exist in GCS: {file_path}")
            return None
            
        # Generate the signed URL
        print(f"Generating signed URL for: {file_path} with expiration: {expiration_minutes} minutes")
        url = blob.generate_signed_url(
            version="v4",
            expiration=datetime.timedelta(minutes=expiration_minutes),
            method="GET"
        )
        
        print(f"Generated signed URL: {url[:50]}..." if url else "Failed to generate URL")
        return url
    except Exception as e:
        print(f"Error generating signed URL for {file_path}: {str(e)}")
        import traceback
        traceback.print_exc()
        return None

def download_file(file_path, local_path, bucket_name=DEFAULT_BUCKET_NAME):
    """Download a file from Google Cloud Storage to a local path."""
    client = get_storage_client()
    
    if client is None:
        print(f"GCS not available, cannot download {file_path}")
        return None
    
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(file_path)
    
    blob.download_to_filename(local_path)
    
    return local_path

def download_as_string(file_path, bucket_name=DEFAULT_BUCKET_NAME):
    """Download a file as a string from Google Cloud Storage."""
    client = get_storage_client()
    
    if client is None:
        print(f"GCS not available, cannot download {file_path}")
        return None
    
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(file_path)
    
    try:
        return blob.download_as_bytes()
    except Exception as e:
        print(f"Error downloading {file_path} from GCS: {str(e)}")
        return None

def check_if_file_exists(file_path, bucket_name=DEFAULT_BUCKET_NAME):
    """Check if a file exists in the bucket."""
    client = get_storage_client()
    
    if client is None:
        print(f"GCS not available, assuming {file_path} does not exist")
        return False
    
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(file_path)
    
    try:
        return blob.exists()
    except Exception as e:
        print(f"Error checking if file exists in GCS: {str(e)}")
        return False

def list_files_with_prefix(prefix, bucket_name=DEFAULT_BUCKET_NAME):
    """List all files in the bucket with a given prefix."""
    client = get_storage_client()
    
    if client is None:
        print(f"GCS not available, cannot list files with prefix {prefix}")
        return []
    
    bucket = client.bucket(bucket_name)
    
    try:
        blobs = bucket.list_blobs(prefix=prefix)
        return [blob.name for blob in blobs]
    except Exception as e:
        print(f"Error listing files in GCS: {str(e)}")
        return []

def generate_unique_filepath(filename, prefix=""):
    """Generate a unique filepath with the given filename and optional prefix.
    
    Args:
        filename: The original filename
        prefix: Optional directory prefix
        
    Returns:
        A unique filepath in the format: prefix/uuid_filename
    """
    unique_id = str(uuid.uuid4())
    base_name, ext = os.path.splitext(filename)
    
    # Clean the filename
    clean_base_name = ''.join(c for c in base_name if c.isalnum() or c in '-_').lower()
    
    unique_filename = f"{clean_base_name}_{unique_id}{ext}"
    if prefix:
        if not prefix.endswith('/'):
            prefix += '/'
        return f"{prefix}{unique_filename}"
    return unique_filename

def delete_file(file_path, bucket_name=DEFAULT_BUCKET_NAME):
    """Delete a file from Google Cloud Storage."""
    client = get_storage_client()
    
    if client is None:
        print(f"GCS not available, cannot delete {file_path}")
        return False
    
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(file_path)
    
    if blob.exists():
        blob.delete()
        return True
    return False 