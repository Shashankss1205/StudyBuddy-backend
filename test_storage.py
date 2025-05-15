import os
from dotenv import load_dotenv
from cloud_storage import get_storage_client, check_if_file_exists, list_files_with_prefix

# Load environment variables from .env file
load_dotenv()

# Print environment variables for debugging
credentials_path = os.getenv('GOOGLE_APPLICATION_CREDENTIALS')
bucket_name = os.getenv('GCS_BUCKET_NAME')

print(f"Credentials path: {credentials_path}")
print(f"Bucket name: {bucket_name}")
print(f"File exists: {os.path.exists(credentials_path) if credentials_path else False}")

# Get storage client
client = get_storage_client()
print(f"Client connected: {client is not None}")

if client:
    # List buckets
    try:
        buckets = list(client.list_buckets())
        print(f"Buckets: {[b.name for b in buckets]}")
    except Exception as e:
        print(f"Error listing buckets: {str(e)}")
    
    # Try to list files
    try:
        bucket = client.bucket(bucket_name)
        if not bucket.exists():
            print(f"Bucket {bucket_name} does not exist")
        else:
            print(f"Bucket {bucket_name} exists")
            files = list(bucket.list_blobs(max_results=10))
            print(f"Files in bucket: {[f.name for f in files][:10] if files else 'No files found'}")
    except Exception as e:
        print(f"Error with bucket operations: {str(e)}") 