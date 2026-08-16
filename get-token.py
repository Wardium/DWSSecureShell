import urllib.parse
import requests

# Fill these in from Phase 1
CLIENT_ID = "129761454210-26032tf9jtbc70rt04l4ahjuv85281f9.apps.googleusercontent.com""
CLIENT_SECRET = "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"
PROJECT_ID = "5054b2ba-0390-4474-85be-efeba8a888fd"

# Step 1: Generate Login Link
redirect_uri = "https://www.google.com"
auth_url = (
    f"https://nestservices.google.com/partnerconnections/{PROJECT_ID}/auth?"
    f"redirect_uri={redirect_uri}&access_type=offline&prompt=consent&"
    f"client_id={CLIENT_ID}&response_type=code&scope=https://www.googleapis.com/auth/sdm.service"
)

print(f"1. Click this link and log in: \n{auth_url}\n")
print("2. After approving, you will be redirected to Google.com.")
print("3. Look at the URL in your browser's address bar. Copy the long code right after '?code=' and before '&scope'.\n")

# Step 2: Exchange Code for Refresh Token
code = input("Paste the code here: ")

token_url = "https://oauth2.googleapis.com/token"
payload = {
    "client_id": CLIENT_ID,
    "client_secret": CLIENT_SECRET,
    "code": code,
    "grant_type": "authorization_code",
    "redirect_uri": redirect_uri
}

response = requests.post(token_url, data=payload)
data = response.json()

if "refresh_token" in data:
    print(f"\nSUCCESS! Here is your permanent refresh token. Save this for app.py:\n{data['refresh_token']}")
else:
    print(f"\nError: {data}")
