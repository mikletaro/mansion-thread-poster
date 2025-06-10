import os
import base64
import datetime
import random
import re
import requests
import gspread
from google.oauth2.service_account import Credentials
import html
import json
from bs4 import BeautifulSoup
import time

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
        blocks = re.findall(r'<a href="/bbs/thread/(\d+)/" class="component_thread_list_item link.*?<span class="num_of_item">(\d+)</span>.*?<div class="oneliner title"[^>]*>(.*?)</div>', res.text, re.DOTALL)
        print(f"▶ Found {len(blocks)} thread blocks")
        for tid, count, title in blocks:
            url = f"https://www.e-mansion.co.jp/bbs/thread/{tid}/"
            title = html.unescape(title).strip()
            count = int(count)
            threads.append({"url": url, "title": title, "count": count, "id": tid})
    print(f"▶ Fetched {len(threads)} threads")
    return threads

def fetch_thread_posts(thread_id: str, max_pages: int = 5, delay_sec: float = 1.0) -> list[str]:
    base_url = f"https://www.e-mansion.co.jp/bbs/thread/{thread_id}/?page="
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
    }

    posts = []

    for page in range(1, max_pages + 1):
        url = base_url + str(page)
        print(f"▶ Fetching thread page: {url}")
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"▶ Error fetching page {page} of thread {thread_id}: {e}")
            continue

        soup = BeautifulSoup(resp.text, 'html.parser')
        comment_tags = soup.select('p[itemprop="commentText"]')
        print(f"▶ Found {len(comment_tags)} posts on page {page}")

        for tag in comment_tags:
            text = tag.get_text(strip=True)
            if text:
                posts.append(text)

        time.sleep(delay_sec)

    return posts

def fetch_thread_text(url):
    thread_id = re.search(r'/thread/(\d+)/', url).group(1)
    return "\n".join(fetch_thread_posts(thread_id))

def load_history():
    sheet = GC.open_by_key(SPREADSHEET_ID).worksheet(HISTORY_SHEET)
    data = sheet.get_all_values()[1:]
    return {row[0]: int(row[1]) for row in data if len(row) > 1 and row[1].isdigit()}

def save_history(history):
    sheet = GC.open_by_key(SPREADSHEET_ID).worksheet(HISTORY_SHEET)
    rows = [[url, count, datetime.datetime.now().strftime("%Y/%m/%d")] for url, count in history.items()]
    sheet.clear()
    sheet.append_row(["URL", "取得時レス数", "最終取得日"])
    sheet.append_rows(rows)

def judge_risk(text):
    try:
        prompt = f"""
以下は掲示板の書き込み内容です。この中に炎上しそうな内容が含まれていないかを判定してください。
最初に「リスク：高」「リスク：低」「リスク：不明」のいずれかを明示してください。そのあとに簡単な根拠も説明してください。
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
        risk_lines = [line for line in content.splitlines() if line.startswith("リスク：")]

        if any("高" in line for line in risk_lines):
            risk = "高"
        else:
            risk = "低"

        flag = "NG" if risk == "高" else "OK"
        return risk, content, flag

    except Exception as e:
        return "高", f"[Error] {str(e)}", "NG"

def generate_summary(text):
    prompt = f"""
以下は掲示板の書き込み内容です。内容を読んで、読みたくなる長いタイトル（120文字以内）をひとつ考えて、タイトルのみ出力してください。前置き・要点整理・説明・全体を「」で括るのは禁止です。
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
    summary = res.json()["content"][0]["text"].strip()

    if summary.startswith("「") and summary.endswith("」"):
        summary = summary[1:-1]

    return summary

def main():
    threads = fetch_threads()
    history = load_history()
    diffs = []
    seen_ids = set()

    for t in sorted(threads, key=lambda x: x["count"] - history.get(x["url"], 0), reverse=True):
        tid = t["id"]
        url, title, count = t["url"], t["title"], t["count"]
        if tid in seen_ids:
            continue
        seen_ids.add(tid)

        history_count = history.get(url, 0)
        diff = count - history_count
        print(f"▶ Checking: {title} | count: {count}, history: {history_count}, diff: {diff}")

        if diff <= 0 and url in history:
            continue
        if url not in history and count < 100:
            continue

        diffs.append({"url": url, "title": title, "diff": diff, "count": count})
        if len(diffs) == 20:
            break

    print(f"▶ Selected top {len(diffs)} threads for risk check")

    candidates = []
    updated = {}

    for t in diffs:
        print(f"▶ Fetching thread text and judging risk for: {t['title']}")
        text = fetch_thread_text(t["url"])
        if not text.strip():
            print(f"▶ No posts found for thread {t['title']} — skipping.")
            continue

        risk, comment, flag = judge_risk(text)
        candidates.append({
            "url": t["url"],
            "diff": t["diff"],
            "title": t["title"],
            "risk": risk,
            "comment": comment,
            "flag": flag
        })
        updated[t["url"]] = t["count"]

    print(f"▶ All candidates: {len(candidates)} 件")
    for c in candidates:
        print(f"▶ Candidate: {c['title']} | diff: {c['diff']} | flag: {c['flag']}")

    ok_candidates = [c for c in candidates if c["flag"] == "OK"][:POST_COUNT]
    print(f"▶ OK candidates: {len(ok_candidates)} 件")
    random.shuffle(ok_candidates)

    if os.getenv("TEST_MODE") != "1":
        updated_ok = {c["url"]: updated[c["url"]] for c in ok_candidates}
        save_history({**history, **updated_ok})
    else:
        print("▶ TEST_MODE: スレ履歴は更新しません")

    candidates.sort(key=lambda x: x["diff"], reverse=True)
    write_candidates = GC.open_by_key(SPREADSHEET_ID).worksheet(CANDIDATE_SHEET)
    write_candidates.clear()
    write_candidates.append_row(["URL", "差分レス数", "タイトル", "炎上リスク", "コメント", "投稿可否"])
    for c in candidates:
        write_candidates.append_row([c["url"], c["diff"], c["title"], c["risk"], c["comment"], c["flag"]])

    today = datetime.date.today()
    post_sheet = GC.open_by_key(SPREADSHEET_ID).worksheet(POST_SHEET)
    post_sheet.clear()
    post_sheet.append_row(["日付", "投稿時間", "投稿テキスト", "投稿済み", "URL"])

    for i, c in enumerate(ok_candidates):
        post_date = today + datetime.timedelta(days=1 + i // 2)
        time_str = "8:00" if i % 2 == 0 else "15:00"
        summary = generate_summary(fetch_thread_text(c["url"]))
        thread_id = re.search(r'(\d+)/$', c["url"]).group(1)
        utm = f"?utm_source=x&utm_medium=em-{thread_id}&utm_campaign={post_date.strftime('%Y%m%d')}"
        post_text = f"{summary}\n#マンションコミュニティ\n{c['url']}{utm}"
        post_sheet.append_row([post_date.strftime("%Y/%m/%d"), time_str, post_text, "FALSE", c["url"]])

if __name__ == "__main__":
    main()
