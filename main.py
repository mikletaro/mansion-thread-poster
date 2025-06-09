import os
import base64
import datetime
import random
import re
import requests
import gspread
from google.oauth2.service_account import Credentials
import json

print(f"▶ TEST_MODE: {os.getenv('TEST_MODE')}")

SPREADSHEET_ID = os.environ['SPREADSHEET_ID']
CLAUDE_API_KEY = os.environ['CLAUDE_API_KEY']
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

print("▶ Decoding GCP_SERVICE_ACCOUNT_B64...")
b64 = os.environ['GCP_SERVICE_ACCOUNT_B64']
json_bytes = base64.b64decode(b64)
with open("service_account.json", "wb") as f:
    f.write(json_bytes)
print("▶ service_account.json written")

print("▶ Authorizing gspread...")
CREDS = Credentials.from_service_account_file("service_account.json", scopes=SCOPES)
GC = gspread.authorize(CREDS)
print("▶ gspread authorized")

HISTORY_SHEET = "スレ履歴"
CANDIDATE_SHEET = "投稿候補"
POST_SHEET = "投稿予定"
MAX_PAGES = 3
POST_COUNT = 14

def fetch_threads():
    import html
    threads = []
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
    }

    for page in range(1, MAX_PAGES + 1):
        url = f"https://www.e-mansion.co.jp/bbs/board/23ku/?page={page}"
        print(f"▶ Fetching: {url}")
        res = requests.get(url, headers=headers)
        if res.status_code != 200:
            print(f"▶ ERROR: Failed to fetch page {page}")
            continue

        blocks = re.findall(r'<div class="list_item detail">(.*?)</div>\s*</div>', res.text, re.DOTALL)
        print(f"▶ Found {len(blocks)} thread blocks")

        for block in blocks:
            # スレッドID（URL）を取得（例: "/bbs/thread/123456/"）
            tid_match = re.search(r'/bbs/thread/(\d+)/', block)
            if not tid_match:
                continue
            tid = tid_match.group(1)
            url = f"https://www.e-mansion.co.jp/bbs/thread/{tid}/"

            # タイトル
            title_match = re.search(r'<div class="oneliner title"[^>]*>(.*?)</div>', block, re.DOTALL)
            title = html.unescape(title_match.group(1)).strip() if title_match else "(no title)"

            # レス数
            count_match = re.search(r'<span class="num_of_item">(\d+)</span>', block)
            count = int(count_match.group(1)) if count_match else 0

            threads.append({"url": url, "title": title, "count": count})

    print(f"▶ Fetched {len(threads)} threads")
    return threads

def load_history():
    sheet = GC.open_by_key(SPREADSHEET_ID).worksheet(HISTORY_SHEET)
    data = sheet.get_all_values()[1:]
    return {row[0]: int(row[1]) for row in data if len(row) > 1 and row[1].isdigit()}

def save_history(history):
    sheet = GC.open_by_key(SPREADSHEET_ID).worksheet(HISTORY_SHEET)
    rows = [[url, count, datetime.datetime.now().strftime("%Y/%m/%d")] for url, count in history.items()]
    sheet.clear()
    sheet.append_row(["スレURL", "取得時のレス数", "最終取得日"])
    sheet.append_rows(rows)

def fetch_thread_text(url):
    text = ""
    for i in range(1, 6):
        html = requests.get(f"{url}?page={i}").text
        posts = re.findall(r'<p itemprop="commentText">([\s\S]*?)</p>', html)
        for post in posts:
            plain = re.sub(r'<[^>]+>', '', post).replace('\u3000', ' ').strip()
            text += plain + "\n"
    return text

def judge_risk(text):
    try:
        prompt = f"""
以下は掲示板の書き込み内容です。この中に炎上しそうな内容が含まれていないかを判定してください。
最初に「リスク：高」「リスク：低」「リスク：不明」のいずれかを明示してください。そのあとに簡単な根拠も記述してください。
--- 本文 ---
{text}
"""
        payload = {
            "model": "claude-3-haiku-20240307",
            "temperature": 0,
            "max_tokens": 300,
            "messages": [{"role": "user", "content": prompt}]
        }
        headers = {
            "x-api-key": CLAUDE_API_KEY,
            "anthropic-version": "2023-06-01"
        }
        res = requests.post("https://api.anthropic.com/v1/messages", headers=headers, json=payload)
        content = res.json()["content"][0]["text"].strip()
        risk_line = next((line for line in content.splitlines() if line.startswith("リスク：")), "")
        if "高" in risk_line:
            return "高", content, "NG"
        elif "低" in risk_line:
            return "低", content, "OK"
        elif "不明" in risk_line:
            return "不明", content, "NG"
        else:
            return "不明", content, "NG"
    except Exception as e:
        return "不明", f"[エラー] {str(e)}", "NG"

def generate_summary(text):
    prompt = f"""
以下は掲示板の書き込み内容です。内容を120文字以内で自然な1文にしてください。前置き・要点整理・説明は禁止です。
{text}
"""
    payload = {
        "model": "claude-3-haiku-20240307",
        "temperature": 0,
        "max_tokens": 100,
        "messages": [{"role": "user", "content": prompt}]
    }
    headers = {
        "x-api-key": CLAUDE_API_KEY,
        "anthropic-version": "2023-06-01"
    }
    res = requests.post("https://api.anthropic.com/v1/messages", headers=headers, json=payload)
    return res.json()["content"][0]["text"].strip()

def main():
    threads = fetch_threads()
    history = load_history()
    updated = {}
    candidates = []

    for t in threads:
        url, title, count = t["url"], t["title"], t["count"]
        diff = count - history.get(url, 0)
        if diff <= 0 and url in history:
            continue
        if url not in history and count < 100:
            continue
        text = fetch_thread_text(url)
        risk, comment, flag = judge_risk(text)
        candidates.append({"url": url, "diff": diff, "title": title, "risk": risk, "comment": comment, "flag": flag})
        updated[url] = count

    if os.getenv("TEST_MODE") != "1":
        save_history({**history, **updated})

    candidates.sort(key=lambda x: x["diff"], reverse=True)
    write_candidates = GC.open_by_key(SPREADSHEET_ID).worksheet(CANDIDATE_SHEET)
    write_candidates.clear()
    write_candidates.append_row(["スレURL", "差分レス数", "タイトル", "炎上リスク判定", "コメント", "投稿可否"])
    for c in candidates[:20]:
        write_candidates.append_row([c["url"], c["diff"], c["title"], c["risk"], c["comment"], c["flag"]])

    ok_candidates = [c for c in candidates if c["flag"] == "OK"][:POST_COUNT]
    random.shuffle(ok_candidates)

    today = datetime.date.today()
    post_sheet = GC.open_by_key(SPREADSHEET_ID).worksheet(POST_SHEET)
    post_sheet.clear()
    post_sheet.append_row(["日付", "投稿時間", "投稿テキスト", "投稿済み", "スレURL"])

    for i, c in enumerate(ok_candidates):
        post_date = today + datetime.timedelta(days=1 + i // 2)
        time = "8:00" if i % 2 == 0 else "15:00"
        summary = generate_summary(fetch_thread_text(c["url"]))
        thread_id = re.search(r'(\d+)/$', c["url"]).group(1)
        utm = f"?utm_source=x&utm_medium=em-{thread_id}&utm_campaign={post_date.strftime('%Y%m%d')}"
        post_text = f"{summary}\n#マンションコミュニティ\n{c['url']}{utm}"
        post_sheet.append_row([post_date.strftime("%Y/%m/%d"), time, post_text, "FALSE", c["url"]])

if __name__ == "__main__":
    main()
