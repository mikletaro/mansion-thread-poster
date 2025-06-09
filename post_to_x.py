import os
import datetime
import requests
import gspread
from google.oauth2.service_account import Credentials

SPREADSHEET_ID = os.environ['SPREADSHEET_ID']
X_API_KEY = os.environ['X_API_KEY']
X_API_SECRET = os.environ['X_API_SECRET']
X_ACCESS_TOKEN = os.environ['X_ACCESS_TOKEN']
X_ACCESS_SECRET = os.environ['X_ACCESS_SECRET']

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
CREDS = Credentials.from_service_account_file("service_account.json", scopes=SCOPES)
GC = gspread.authorize(CREDS)

POST_SHEET = "投稿予定"


def post_to_x(text):
    url = "https://api.twitter.com/2/tweets"
    headers = {
        "Authorization": f"Bearer {X_ACCESS_TOKEN}",
        "Content-Type": "application/json",
        "x-api-version": "2025-04"
    }
    payload = {"text": text}
    response = requests.post(url, headers=headers, json=payload)
    return response.status_code == 201


def main():
    sheet = GC.open_by_key(SPREADSHEET_ID).worksheet(POST_SHEET)
    data = sheet.get_all_values()[1:]
    now = datetime.datetime.now()
    today = now.strftime("%Y/%m/%d")
    hour, minute = now.hour, now.minute

    for i, row in enumerate(data):
        date, time_str, text, posted, *_ = row
        if posted.upper() == "TRUE" or date != today:
            continue
        h, m = map(int, time_str.split(":"))
        if hour == h and minute >= m:
            if post_to_x(text):
                sheet.update_cell(i + 2, 4, "TRUE")


if __name__ == "__main__":
    main()
