import os
from dotenv import load_dotenv
import json

# Load environment variables from .env file
load_dotenv()

# Check if credentials file exists
credentials_path = os.getenv('GOOGLE_APPLICATION_CREDENTIALS')
print(f"Credentials path: {credentials_path}")

if not credentials_path:
    print("ERROR: No credentials path set in environment variables")
    exit(1)

if not os.path.exists(credentials_path):
    print(f"ERROR: Credentials file not found at {credentials_path}")
    exit(1)

print(f"Credentials file exists at {credentials_path}")

# Check if the file is a valid JSON file
try:
    with open(credentials_path, 'r') as f:
        credentials_json = json.load(f)
    
    # Check if it has the required fields for GCP service account
    required_fields = ['type', 'project_id', 'private_key_id', 'private_key', 'client_email']
    missing_fields = [field for field in required_fields if field not in credentials_json]
    
    if missing_fields:
        print(f"ERROR: Credentials file is missing required fields: {', '.join(missing_fields)}")
        exit(1)
    
    print("Credentials file is valid!")
    print(f"Project ID: {credentials_json['project_id']}")
    print(f"Client email: {credentials_json['client_email']}")
except json.JSONDecodeError:
    print(f"ERROR: Credentials file is not a valid JSON file")
    exit(1)
except Exception as e:
    print(f"ERROR: Failed to read credentials file: {str(e)}")
    exit(1)

print("\nAll checks passed! The credentials file exists and is valid.") 