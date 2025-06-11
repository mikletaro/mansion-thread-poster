import os
import base64
import datetime
import random
import re
import requests
import gspread
from google.oauth2.service_account import Credentials
import html
from bs4 import BeautifulSoup
import time

# ----------------------------------------------------------------------
# 環境変数と定数
# ----------------------------------------------------------------------
print(f"▶ TEST_MODE: {os.getenv('TEST_MODE')}")

SPREADSHEET_ID = os.environ['SPREADSHEET_ID']
CLAUDE_API_KEY = os.environ['CLAUDE_API_KEY']
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

MAX_PAGES        = 3     # スレ一覧を巡回するページ数
POST_COUNT       = 14    # 投稿候補の最大数
MAX_RETRY_BASE   = 3     # generate_summary() の内部リトライ回数
MAX_EXTRA_RETRY  = 2     # 禁止語が残った行を追加でリトライする回数

HISTORY_SHEET    = "スレ履歴"
CANDIDATE_SHEET  = "投稿候補"
POST_SHEET       = "投稿予定"

# ----------------------------------------------------------------------
# Google 認証
# ----------------------------------------------------------------------
print("▶ Decoding GCP_SERVICE_ACCOUNT_B64...")
json_bytes = base64.b64decode(os.environ['GCP_SERVICE_ACCOUNT_B64'])
with open("service_account.json", "wb") as f:
    f.write(json_bytes)
print("▶ service_account.json written")

print("▶ Authorizing gspread...")
CREDS = Credentials.from_service_account_file("service_account.json", scopes=SCOPES)
GC = gspread.authorize(CREDS)
print("▶ gspread authorized")

# ----------------------------------------------------------------------
# 禁止語リスト
# ----------------------------------------------------------------------
BANNED_WORDS = [
    "意味不明", "共産主義", "中国人", "血税", "糞尿",
    "悩む", "スケベ", "低俗", "トラブル", "酷い", "劣等感"
]

def contains_banned(words: list[str], text: str) -> bool:
    """テキストに禁止語が含まれていれば True"""
    return any(re.search(re.escape(w), text, re.IGNORECASE) for w in words)

# ----------------------------------------------------------------------
# スレッド一覧取得
# ----------------------------------------------------------------------
def fetch_threads():
    threads = []
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
    }
    for page in range(1, MAX_PAGES + 1):
        url = f"https://www.e-mansion.co.jp/bbs/board/23ku/?page={page}"
        print(f"▶ Fetching: {url}")
        res = requests.get(url, headers=headers, timeout=30)
        if res.status_code != 200:
            print(f"▶ ERROR: Failed to fetch page {page}")
            continue
        blocks = re.findall(
            r'<a href="/bbs/thread/(\d+)/" class="component_thread_list_item link.*?<span class="num_of_item">(\d+)</span>.*?<div class="oneliner title"[^>]*>(.*?)</div>',
            res.text,
            re.DOTALL
        )
        print(f"▶ Found {len(blocks)} thread blocks")
        for tid, count, title in blocks:
            threads.append({
                "url": f"https://www.e-mansion.co.jp/bbs/thread/{tid}/",
                "title": html.unescape(title).strip(),
                "count": int(count),
                "id": tid
            })
    print(f"▶ Fetched {len(threads)} threads")
    return threads

# ----------------------------------------------------------------------
# スレッド本文取得
# ----------------------------------------------------------------------
def fetch_thread_posts(thread_id: str, max_pages: int = 5, delay_sec: float = 1.0) -> list[str]:
    base_url = f"https://www.e-mansion.co.jp/bbs/thread/{thread_id}/?page="
    headers  = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)"
    }
    posts = []
    for page in range(1, max_pages + 1):
        url = base_url + str(page)
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"▶ Error fetching page {page} of thread {thread_id}: {e}")
            continue
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup.select('p[itemprop="commentText"]'):
            txt = tag.get_text(strip=True)
            if txt:
                posts.append(txt)
        time.sleep(delay_sec)
    print(f"▶ Collected {len(posts)} posts from thread {thread_id}")
    return posts

def fetch_thread_text(url: str) -> str:
    thread_id = re.search(r'/thread/(\d+)/', url).group(1)
    return "\n".join(fetch_thread_posts(thread_id))

# ----------------------------------------------------------------------
# スプレッドシート履歴
# ----------------------------------------------------------------------
def load_history() -> dict[str, int]:
    sheet = GC.open_by_key(SPREADSHEET_ID).worksheet(HISTORY_SHEET)
    data  = sheet.get_all_values()[1:]  # skip header
    return {row[0]: int(row[1]) for row in data if len(row) > 1 and row[1].isdigit()}

def save_history(history: dict[str, int]):
    sheet = GC.open_by_key(SPREADSHEET_ID).worksheet(HISTORY_SHEET)
    rows  = [[url, cnt, datetime.datetime.now().strftime("%Y/%m/%d")] for url, cnt in history.items()]
    sheet.clear()
    sheet.append_row(["URL", "取得時レス数", "最終取得日"])
    sheet.append_rows(rows)

# ----------------------------------------------------------------------
# リスク判定 (Claude)
# ----------------------------------------------------------------------
def judge_risk(text: str):
    prompt = f"""
以下は掲示板の書き込み内容です。この中に炎上しそうな内容が含まれていないかを判定してください。
最初に「リスク：高」「リスク：低」「リスク：不明」のいずれかを明示してください。そのあとに簡潔な根拠を述べてください。
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
    try:
        res = requests.post("https://api.anthropic.com/v1/messages", headers=headers, json=payload, timeout=45)
        content = res.json()["content"][0]["text"].strip()
        risk    = "高" if "リスク：高" in content else ("低" if "リスク：低" in content else "不明")
        return risk, content, "NG" if risk == "高" else "OK"
    except Exception as e:
        return "不明", f"[Error] {e}", "NG"

# ----------------------------------------------------------------------
# タイトル生成 (Claude + 内部リトライ)
# ----------------------------------------------------------------------
def generate_summary(text: str, max_retry: int = MAX_RETRY_BASE) -> str:
    base_prompt = f"""
あなたは掲示板スレッドの要約ライターです。
### 手順
1. {text} を読み、次の「許可カテゴリ」に該当する話題が含まれなければ **NOK** とだけ答える。  
2. 禁止語リストにある語句を含めず、目を引く長い日本語タイトルを1つ作成する。  
3. 出力はタイトルのみ。120文字以内。括弧や前置き語句は禁止。
### 許可カテゴリ
物件概要, 価格・コスト, 交通, 構造・建物, 共用施設, 設備・仕様, 間取り, 買物・食事, 育児・教育, 環境・治安, 周辺施設
### 禁止語リスト
意味不明, 共産主義, 中国人, 血税, 糞尿, 悩む, スケベ, 低俗, トラブル, 酷い, 劣等感
{text}
"""
    for attempt in range(1, max_retry + 1):
        payload = {
            "model": "claude-3-haiku-20240307",
            "temperature": 0,
            "max_tokens": 100,
            "messages": [{"role": "user", "content": base_prompt}]
        }
        headers = {
            "x-api-key": CLAUDE_API_KEY,
            "anthropic-version": "2023-06-01"
        }
        res = requests.post("https://api.anthropic.com/v1/messages", headers=headers, json=payload, timeout=30)
        title = res.json()["content"][0]["text"].strip()

        # 不要な括弧を除去
        if title.startswith("「") and title.endswith("」"):
            title = title[1:-1]
        title = re.sub(r'^.*?[「"](.*?)[」"]$', r'\1', title)

        if not contains_banned(BANNED_WORDS, title):
            return title   # 成功
        print(f"▶ generate_summary retry {attempt}/{max_retry} – banned word detected")
        time.sleep(1)

    return title  # 最終的に禁止語が残っていても返却

# ----------------------------------------------------------------------
# メイン処理
# ----------------------------------------------------------------------
def main():
    print("▶ main() started")
    threads  = fetch_threads()
    history  = load_history()
    seen_ids = set()
    diffs    = []

    # 差分で上位20スレッド抽出
    for t in sorted(threads, key=lambda x: x["count"] - history.get(x["url"], 0), reverse=True):
        if t["id"] in seen_ids:
            continue
        seen_ids.add(t["id"])

        hist_cnt = history.get(t["url"], 0)
        diff     = t["count"] - hist_cnt
        if diff <= 0 and t["url"] in history:
            continue
        if t["url"] not in history and t["count"] < 100:
            continue

        diffs.append({**t, "diff": diff})
        if len(diffs) == 20:
            break

    print(f"▶ Selected {len(diffs)} threads for risk check")
    candidates = []
    updated    = {}
    for t in diffs:
        text = fetch_thread_text(t["url"])
        if not text:
            continue
        risk, desc, flag = judge_risk(text)
        candidates.append({**t, "risk": risk, "comment": desc, "flag": flag})
        updated[t["url"]] = t["count"]

    ok_candidates = [c for c in candidates if c["flag"] == "OK"][:POST_COUNT]
    random.shuffle(ok_candidates)

    # 履歴更新
    if os.getenv("TEST_MODE") != "1":
        save_history({**history, **{u: updated[u] for u in updated if u in [c["url"] for c in ok_candidates]}})
    else:
        print("▶ TEST_MODE – history not saved")

    # 投稿候補シート更新
    cand_ws = GC.open_by_key(SPREADSHEET_ID).worksheet(CANDIDATE_SHEET)
    cand_ws.clear()
    cand_ws.append_row(["URL", "差分レス数", "タイトル", "炎上リスク", "コメント", "投稿可否"])
    for c in sorted(candidates, key=lambda x: x["diff"], reverse=True):
        cand_ws.append_row([c["url"], c["diff"], c["title"], c["risk"], c["comment"], c["flag"]])

    # 投稿予定シート
    post_ws = GC.open_by_key(SPREADSHEET_ID).worksheet(POST_SHEET)
    post_ws.clear()
    post_ws.append_row(["日付", "投稿時間", "投稿テキスト", "投稿済み", "URL"])

    today = datetime.date.today()
    for idx, c in enumerate(ok_candidates):
        # --------------- タイトル生成（追加リトライ付き） ---------------
        attempts = 0
        summary  = ""
        while attempts <= MAX_EXTRA_RETRY:
            summary = generate_summary(fetch_thread_text(c["url"]))
            if not contains_banned(BANNED_WORDS, summary):
                break
            attempts += 1
            print(f"▶ Extra retry {attempts}/{MAX_EXTRA_RETRY} – banned word remains")
            time.sleep(1)

        if contains_banned(BANNED_WORDS, summary):
            print(f"▶ Give up – banned word not cleared: {c['title']}")
            continue  # このスレッドはスキップ

        # --------------- シートへ書き込み ---------------
        post_date = today + datetime.timedelta(days=1 + idx // 2)
        time_str  = "8:00" if idx % 2 == 0 else "15:00"
        thread_id = re.search(r'/thread/(\d+)/', c["url"]).group(1)
        utm       = f"?utm_source=x&utm_medium=em-{thread_id}&utm_campaign={post_date.strftime('%Y%m%d')}"
        post_txt  = f"{summary}\n#マンションコミュニティ\n{c['url']}{utm}"
        post_ws.append_row([post_date.strftime("%Y/%m/%d"), time_str, post_txt, "FALSE", c["url"]])

    print("▶ Done")

# ----------------------------------------------------------------------
if __name__ == "__main__":
    main()
