import os
import time
import requests
from requests_oauthlib import OAuth1Session
from dotenv import load_dotenv

load_dotenv()
# 認証情報（GitHub Secretsや.envから設定）
API_KEY = os.environ['TWITTER_API_KEY']
API_SECRET = os.environ['TWITTER_API_SECRET']
ACCESS_TOKEN = os.environ['TWITTER_ACCESS_TOKEN']
ACCESS_SECRET = os.environ['TWITTER_ACCESS_SECRET']

# 投稿したいテキスト（仮に引数またはファイルで受け取る想定）
def post_to_x(status_text: str):
    url = "https://api.twitter.com/2/tweets"
    oauth = OAuth1Session(API_KEY, API_SECRET, ACCESS_TOKEN, ACCESS_SECRET)

    payload = { "text": status_text }
    response = oauth.post(url, json=payload)

    if response.status_code == 201:
        print("✅ 投稿成功")
        print("📝", response.json())
    else:
        print("❌ 投稿失敗")
        print("Status:", response.status_code)
        print(response.text)
        raise Exception("投稿に失敗しました")

if __name__ == "__main__":
    # 例：固定文を投稿（GitHub Actionsでファイルや引数に置き換え可）
    post_to_x("これは #マンションコミュニティ の自動投稿テストです https://example.com/")
