import os
import datetime
import random
import re
import requests
import gspread
from google.oauth2.service_account import Credentials
import json

SPREADSHEET_ID = os.environ['SPREADSHEET_ID']
CLAUDE_API_KEY = os.environ['CLAUDE_API_KEY']
TEST_MODE = os.environ.get("TEST_MODE") == "1"
print(f"▶ TEST_MODE: {TEST_MODE}")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

CREDS = Credentials.from_service_account_file("service_account.json", scopes=SCOPES)
GC = gspread.authorize(CREDS)

HISTORY_SHEET = "スレ履歴"
CANDIDATE_SHEET = "投稿候補"
POST_SHEET = "投稿予定"
MAX_PAGES = 3
POST_COUNT = 14


def fetch_threads():
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    }
    threads = []
    for page in range(1, MAX_PAGES + 1):
        url = f"https://www.e-mansion.co.jp/bbs/board/23ku/?page={page}"
        res = requests.get(url, headers=HEADERS)
        matches = re.findall(
            r'<a href="/bbs/thread/(\d+)/"[\s\S]*?<div class="oneliner title"[^>]*>([\s\S]*?)</div>[\s\S]*?<span class="num_of_item">(\d+)</span>',
            res.text)
        for tid, title, count in matches:
            threads.append({
                "url": f"https://www.e-mansion.co.jp/bbs/thread/{tid}/",
                "title": title.strip(),
                "count": int(count)
            })
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
    print(f"▶ Updated history with {len(history)} threads")


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
        print(f"▶ Risk Line: {risk_line}")
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
        print(f"▶ {title}｜diff: {diff}｜risk: {risk}｜flag: {flag}")
        candidates.append({"url": url, "diff": diff, "title": title, "risk": risk, "comment": comment, "flag": flag})
        updated[url] = count

    if not TEST_MODE:
        save_history({**history, **updated})

    candidates.sort(key=lambda x: x["diff"], reverse=True)
    ok_candidates = [c for c in candidates if c["flag"] == "OK"][:POST_COUNT]
    print(f"▶ 投稿候補（OK）: {len(ok_candidates)} 件")

    # 投稿候補書き込み
    write_candidates = GC.open_by_key(SPREADSHEET_ID).worksheet(CANDIDATE_SHEET)
    write_candidates.clear()
    write_candidates.append_row(["スレURL", "差分レス数", "タイトル", "炎上リスク判定", "コメント", "投稿可否"])
    for c in candidates[:20]:
        write_candidates.append_row([c["url"], c["diff"], c["title"], c["risk"], c["comment"], c["flag"]])

    # 投稿予定書き込み
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
