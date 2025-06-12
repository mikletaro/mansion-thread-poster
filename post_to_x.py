#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
投稿予定シートをポーリングして、期日が来た行だけ X（旧 Twitter）へ投稿し
完了したら「投稿済み」列を TRUE に更新するスクリプト
"""

import os, datetime, requests, gspread, pytz
from requests_oauthlib import OAuth1
from google.oauth2.service_account import Credentials

# ───── 0. 環境変数 ─────
SPREADSHEET_ID      = os.environ["SPREADSHEET_ID"]
GCP_SERVICE_ACCOUNT = os.environ["GCP_SERVICE_ACCOUNT_B64"]   # base64
X_API_KEY           = os.environ["X_API_KEY"]
X_API_SECRET        = os.environ["X_API_SECRET"]
X_ACCESS_TOKEN      = os.environ["X_ACCESS_TOKEN"]
X_ACCESS_SECRET     = os.environ["X_ACCESS_SECRET"]

POST_SHEET = "投稿予定"

# ───── 1. Google Sheets 認証 ─────
creds_json = os.environ["GCP_SERVICE_ACCOUNT_B64"]
with open("/tmp/sa.json", "wb") as f:
    f.write(base64.b64decode(creds_json))

gc = gspread.authorize(
    Credentials.from_service_account_file("/tmp/sa.json",
                                          scopes=["https://www.googleapis.com/auth/spreadsheets"])
)

# ───── 2. X 投稿関数 (OAuth1.0a) ─────
def post_to_x(text: str) -> bool:
    url = "https://api.twitter.com/2/tweets"
    auth = OAuth1(X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_SECRET)
    res = requests.post(url, auth=auth, json={"text": text}, timeout=30)
    return res.status_code == 201

# ───── 3. メイン処理 ─────
def main():
    ws = gc.open_by_key(SPREADSHEET_ID).worksheet(POST_SHEET)
    rows = ws.get_all_values()[1:]     # 1 行目ヘッダを除く

    # JST 現在時刻
    jst = pytz.timezone("Asia/Tokyo")
    now = datetime.datetime.now(jst)

    for idx, row in enumerate(rows, start=2):      # シート行番号 = idx
        date_str, time_str, text, posted, *_ = row[:5]

        if posted.upper() == "TRUE":
            continue

        # 日付・時間パース
        try:
            dt_post = jst.localize(
                datetime.datetime.strptime(f"{date_str} {time_str}", "%Y/%m/%d %H:%M")
            )
        except ValueError:
            print(f"行{idx}: 日付/時刻フォーマットエラー")
            continue

        if dt_post <= now:                         # 投稿時刻を過ぎていたら投稿
            success = post_to_x(text)
            status  = "TRUE" if success else "ERROR"
            ws.update_cell(idx, 4, status)         # D列=投稿済み
            print(f"行{idx}: {'投稿完了' if success else '投稿失敗'}")

if __name__ == "__main__":
    main()
