#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
投稿予定シートをチェックし、指定日時を過ぎた行だけ Twitter へ投稿。
成功したら「投稿済み」列を TRUE、失敗したら ERROR に更新するスクリプト
"""

import os, base64, datetime, requests, gspread, pytz
from requests_oauthlib import OAuth1
from google.oauth2.service_account import Credentials

# ───── 0. 環境変数 ─────
SPREADSHEET_ID          = os.environ["SPREADSHEET_ID"]
GCP_SERVICE_ACCOUNT_B64 = os.environ["GCP_SERVICE_ACCOUNT_B64"]       # base64
TWITTER_API_KEY         = os.environ["TWITTER_API_KEY"]
TWITTER_API_SECRET      = os.environ["TWITTER_API_SECRET"]
TWITTER_ACCESS_TOKEN    = os.environ["TWITTER_ACCESS_TOKEN"]
TWITTER_ACCESS_SECRET   = os.environ["TWITTER_ACCESS_SECRET"]

POST_SHEET = "投稿予定"

# ───── 1. Google Sheets 認証 ─────
with open("/tmp/sa.json", "wb") as f:
    f.write(base64.b64decode(GCP_SERVICE_ACCOUNT_B64))

gc = gspread.authorize(
    Credentials.from_service_account_file(
        "/tmp/sa.json",
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
)

# ───── 2. Twitter 投稿関数 (OAuth1.0a) ─────
def post_to_twitter(text: str) -> bool:
    url = "https://api.twitter.com/2/tweets"
    auth = OAuth1(
        TWITTER_API_KEY,
        TWITTER_API_SECRET,
        TWITTER_ACCESS_TOKEN,
        TWITTER_ACCESS_SECRET
    )
    res = requests.post(url, auth=auth, json={"text": text}, timeout=30)
    return res.status_code == 201


# ───── 3. メイン処理 ─────
def main():
    ws   = gc.open_by_key(SPREADSHEET_ID).worksheet(POST_SHEET)
    rows = ws.get_all_values()[1:]       # ヘッダを除外

    # JST 現在時刻
    jst = pytz.timezone("Asia/Tokyo")
    now = datetime.datetime.now(jst)

    for idx, row in enumerate(rows, start=2):      # シート行番号は 2 行目から
        date_str, time_str, text, posted, *_ = row[:5]

        if posted.upper() == "TRUE":
            continue

        # 日付＋時刻をパース
        try:
            dt_post = jst.localize(
                datetime.datetime.strptime(f"{date_str} {time_str}", "%Y/%m/%d %H:%M")
            )
        except ValueError:
            print(f"行{idx}: 日付/時刻フォーマットエラー")
            continue

        # 期日を過ぎていれば投稿
        if dt_post <= now:
            success = post_to_twitter(text)
            status  = "TRUE" if success else "ERROR"
            ws.update_cell(idx, 4, status)     # D 列 = 投稿済み
            print(f"行{idx}: {'投稿完了' if success else '投稿失敗'}")


if __name__ == "__main__":
    main()
