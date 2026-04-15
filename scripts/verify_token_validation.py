import asyncio
import os
import sys
from pathlib import Path

# Ensure the project root is in sys.path so we can import from telegram_bot
root_dir = Path(__file__).resolve().parents[1]
sys.path.append(str(root_dir))

try:
    from telegram_bot.backend_client import BackendClient
    from telegram_bot.config import settings
except ImportError as e:
    print(f"Import Error: {e}")
    print("Make sure you are running this script from the project root or have the root in your PYTHONPATH.")
    sys.exit(1)

async def main():
    print("--- Backend Token Validation Test ---")
    
    # Initialize the BackendClient
    # It uses settings.BACKEND_URL by default
    client = BackendClient()
    
    try:
        # 1. Test with an obviously invalid token
        invalid_token = "this_is_an_invalid_token_12345"
        print(f"Testing invalid token: {invalid_token}...")
        is_valid = await client.validate_token(invalid_token)
        if not is_valid:
            print("✅ SUCCESS: Invalid token was correctly rejected.")
        else:
            print("❌ FAILURE: Invalid token was incorrectly accepted!")

        # 2. Test with a potentially valid token
        # We look for a token in the environment variable VALID_BACKEND_TOKEN
        valid_token = os.environ.get("VALID_BACKEND_TOKEN")
        
        if valid_token:
            print(f"Testing valid token from environment: {valid_token}...")
            is_valid = await client.validate_token(valid_token)
            if is_valid:
                print("✅ SUCCESS: Valid token was correctly accepted.")
            else:
                print("❌ FAILURE: Valid token was rejected!")
        else:
            print("⚠️  SKIP: No valid token found in environment variable 'VALID_BACKEND_TOKEN'.")
            print("   To test a valid token, run: set VALID_BACKEND_TOKEN=your_token && python scripts/verify_token_validation.py")

    except Exception as e:
        print(f"❌ UNEXPECTED ERROR: {e}")
    finally:
        await client.close()

    print("-------------------------------------")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
