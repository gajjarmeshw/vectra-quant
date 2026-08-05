"""Helper script to generate Zerodha KiteConnect access_token."""
import sys
from kiteconnect import KiteConnect

def main():
    if len(sys.argv) < 4:
        print("\nUsage: python scripts/get_zerodha_token.py <api_key> <api_secret> <request_token>\n")
        sys.exit(1)

    api_key = sys.argv[1].strip()
    api_secret = sys.argv[2].strip()
    request_token = sys.argv[3].strip()

    try:
        kite = KiteConnect(api_key=api_key)
        data = kite.generate_session(request_token, api_secret=api_secret)
        token = data["access_token"]
        print("\n==========================================")
        print("SUCCESS! Your Zerodha Access Token is:")
        print(f"\nZERODHA_ACCESS_TOKEN={token}\n")
        print("Copy the line above into your .env file.")
        print("==========================================\n")
    except Exception as e:
        print(f"\nERROR generating session: {e}\n")
        sys.exit(1)

if __name__ == "__main__":
    main()
