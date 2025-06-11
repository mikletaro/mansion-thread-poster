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
# 0. 基本設定
# ----------------------------------------------------------------------
print(f"▶ TEST_MODE: {os.getenv('TEST_MODE')}")

SPREADSHEET_ID  = os.environ['SPREADSHEET_ID']
CLAUDE_API_KEY  = os.environ['CLAUDE_API_KEY']
SCOPES          = ["https://www.googleapis.com/auth/spreadsheets"]

MAX_PAGES        = 3     # スレ一覧を巡回する最大ページ
POST_COUNT       = 14    # 投稿候補として採用するスレッド数
MAX_RETRY_BASE   = 3     # generate_summary() 内部リトライ回数
MAX_EXTRA_RETRY  = 2     # "NOK"/禁止語の追加リトライ回数

HISTORY_SHEET    = "スレ履歴"
CANDIDATE_SHEET  = "投稿候補"
POST_SHEET       = "投稿予定"

# ----------------------------------------------------------------------
# 1. Google 認証
# ----------------------------------------------------------------------
print("▶ Decoding GCP_SERVICE_ACCOUNT_B64...")
json_bytes = base64.b64decode(os.environ['GCP_SERVICE_ACCOUNT_B64'])
with open("service_account.json", "wb") as f:
    f.write(json_bytes)
print("▶ service_account.json written")

print("▶ Authorizing gspread...")
CREDS = Credentials.from_service_account_file("service_account.json", scopes=SCOPES)
GC    = gspread.authorize(CREDS)
print("▶ gspread authorized")

# ----------------------------------------------------------------------
# 2. 禁止語リスト
# ----------------------------------------------------------------------
BANNED_WORDS = [
    "意味不明", "共産主義", "中国人", "血税", "糞尿",
    "悩む", "スケベ", "低俗", "トラブル", "酷い", "劣等感", "三流", "タイトル"
]

def contains_banned(words: list[str], text: str) -> bool:
    return any(re.search(re.escape(w), text, re.IGNORECASE) for w in words)

# ----------------------------------------------------------------------
# 3. スレッド取得関連
# ----------------------------------------------------------------------
def fetch_threads():
    threads = []
    ua = {"User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"}
    for p in range(1, MAX_PAGES + 1):
        url = f"https://www.e-mansion.co.jp/bbs/board/23ku/?page={p}"
        print(f"▶ Fetching list page: {url}")
        res = requests.get(url, headers=ua, timeout=30)
        if res.status_code != 200:
            print(f"▶ ERROR: status {res.status_code}")
            continue
        blocks = re.findall(
            r'<a href="/bbs/thread/(\d+)/" class="component_thread_list_item link.*?<span class="num_of_item">(\d+)</span>.*?<div class="oneliner title"[^>]*>(.*?)</div>',
            res.text, re.DOTALL
        )
        for tid, cnt, ttl in blocks:
            threads.append({
                "url": f"https://www.e-mansion.co.jp/bbs/thread/{tid}/",
                "title": html.unescape(ttl).strip(),
                "count": int(cnt),
                "id": tid
            })
        print(f"▶  Page {p}: found {len(blocks)} threads")
    return threads

def fetch_thread_posts(tid: str, max_pages: int = 5, delay: float = 1.0) -> list[str]:
    ua = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    posts = []
    for p in range(1, max_pages + 1):
        url = f"https://www.e-mansion.co.jp/bbs/thread/{tid}/?page={p}"
        try:
            r = requests.get(url, headers=ua, timeout=15)
            r.raise_for_status()
        except requests.RequestException as e:
            print(f"▶ Error: {e}")
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        posts += [t.get_text(strip=True) for t in soup.select('p[itemprop="commentText"]') if t.get_text(strip=True)]
        time.sleep(delay)
    print(f"▶ Thread {tid}: collected {len(posts)} posts")
    return posts

def fetch_thread_text(url: str) -> str:
    tid = re.search(r'/thread/(\d+)/', url).group(1)
    return "\n".join(fetch_thread_posts(tid))

# ----------------------------------------------------------------------
# 4. スプレッドシート履歴
# ----------------------------------------------------------------------
def load_history() -> dict[str, int]:
    sheet = GC.open_by_key(SPREADSHEET_ID).worksheet(HISTORY_SHEET)
    return {r[0]: int(r[1]) for r in sheet.get_all_values()[1:] if len(r) > 1 and r[1].isdigit()}

def save_history(hist: dict[str, int]):
    ws = GC.open_by_key(SPREADSHEET_ID).worksheet(HISTORY_SHEET)
    ws.clear()
    ws.append_row(["URL", "取得時レス数", "最終取得日"])
    for url, cnt in hist.items():
        ws.append_row([url, cnt, datetime.datetime.now().strftime("%Y/%m/%d")])

# ----------------------------------------------------------------------
# 5. 炎上リスク (2 段階)
# ----------------------------------------------------------------------
def judge_risk(text: str):
    prompt = f"""以下の掲示板書き込みの炎上リスクを判定し、最初に「リスク：高」または「リスク：低」を明示して根拠を簡潔に述べてください。
--- 本文 ---
{text}"""
    payload = {
        "model": "claude-3-haiku-20240307",
        "temperature": 0,
        "max_tokens": 300,
        "messages": [{"role": "user", "content": prompt}]
    }
    headers = {"x-api-key": CLAUDE_API_KEY, "anthropic-version": "2023-06-01"}
    try:
        res = requests.post("https://api.anthropic.com/v1/messages", headers=headers,
                            json=payload, timeout=45)
        msg = res.json()["content"][0]["text"].strip()
        risk = "高" if "リスク：高" in msg else "低"
        flag = "NG" if risk == "高" else "OK"
        return risk, msg, flag
    except Exception as e:
        return "高", f"[Error] {e}", "NG"

# ----------------------------------------------------------------------
# 6. タイトル生成 (NOK / API エラー対策)
# ----------------------------------------------------------------------
def generate_summary(text: str, max_retry: int = MAX_RETRY_BASE) -> str:
    base_prompt = f"""
あなたは X（旧Twitter）向けのコピーライターです。  
掲示板スレッド本文を読み、読者が続きをクリックしたくなる **前向きで長め** の日本語タイトルを 1 本だけ生成してください。
### 出力仕様（必ず守る）
1. **タイトル本文のみ** を 1 行で出力
   - 行頭に「-」「–」「・」など記号を付けない
   - 接頭辞「タイトル:」や解説文（例: 「禁止語を含まず～」「120文字以内で～」）を付けない
   - 同一の文を 2 回以上繰り返さない
   - 改行・かぎ括弧・箇条書き・絵文字・記号説明を付けない
   - 120 文字以内
2. 本文冒頭に **地名・駅名・数字** いずれかを入れて目を引く構成にする
3. 末尾に **4～6 文字程度の CTA** を付ける（例: 詳しくはこちら👇、続きはこちら→）
4. 上記を 1 つでも満たせない、または禁止語を 1 語でも含む場合は **NOK** とだけ出力する
   - NOK 以外の文字を一切書かない
### 禁止語
{', '.join(BANNED_WORDS)}
--- 本文 ---
{text}"""
    headers = {"x-api-key": CLAUDE_API_KEY, "anthropic-version": "2023-06-01"}

    for i in range(max_retry + 1):
        body = {
            "model": "claude-3-haiku-20240307",
            "temperature": 0,
            "max_tokens": 100,
            "messages": [{"role": "user", "content": base_prompt}]
        }
        try:
            res = requests.post("https://api.anthropic.com/v1/messages",
                                headers=headers, json=body, timeout=40)
            if res.status_code != 200:
                print(f"▶ Claude HTTP {res.status_code}: retry {i}/{max_retry}")
                time.sleep(3)
                continue
            data = res.json()
            if "content" not in data:
                print(f"▶ Claude JSON without 'content': retry {i}/{max_retry}")
                time.sleep(3)
                continue
            title = data["content"][0]["text"].strip()
        except Exception as e:
            print(f"▶ Claude request error: {e}  retry {i}/{max_retry}")
            time.sleep(3)
            continue

        # NOK 文章を即 NOK 扱い
        if "NOK" in title.upper():
            return "NOK"

        title = re.sub(r'^\s*タイトル[:：]\s*', '', title)
        if title.startswith(("「", "\"", "『")) and title.endswith(("」", "\"", "』")):
            title = title[1:-1]
        title = re.sub(r'^.*?[「"](.*?)[」"]$', r'\1', title)

        if not contains_banned(BANNED_WORDS, title):
            return title
        time.sleep(1)
    return "NOK"

# ----------------------------------------------------------------------
# 7. メイン
# ----------------------------------------------------------------------
def main():
    print("▶ main() start")
    threads   = fetch_threads()
    history   = load_history()
    seen_ids  = set()
    diffs     = []

    for t in sorted(threads, key=lambda x: x["count"] - history.get(x["url"], 0), reverse=True):
        if t["id"] in seen_ids:
            continue
        seen_ids.add(t["id"])

        prev = history.get(t["url"], 0)
        diff = t["count"] - prev
        if diff <= 0 and t["url"] in history:
            continue
        if t["url"] not in history and t["count"] < 100:
            continue
        diffs.append({**t, "diff": diff})
        if len(diffs) == 20:
            break

    candidates, updated = [], {}
    for d in diffs:
        text = fetch_thread_text(d["url"])
        if not text:
            continue
        risk, msg, flag = judge_risk(text)
        candidates.append({**d, "risk": risk, "comment": msg, "flag": flag})
        updated[d["url"]] = d["count"]

    ok = [c for c in candidates if c["flag"] == "OK"][:POST_COUNT]
    random.shuffle(ok)

    if os.getenv("TEST_MODE") != "1":
        new_hist = {**history, **{u: updated[u] for u in updated if u in [c["url"] for c in ok]}}
        save_history(new_hist)
    else:
        print("▶ TEST_MODE: history not saved")

    ws_cand = GC.open_by_key(SPREADSHEET_ID).worksheet(CANDIDATE_SHEET)
    ws_cand.clear()
    ws_cand.append_row(["URL", "差分レス数", "タイトル", "炎上リスク", "コメント", "投稿可否"])
    for c in sorted(candidates, key=lambda x: x["diff"], reverse=True):
        ws_cand.append_row([c["url"], c["diff"], c["title"], c["risk"], c["comment"], c["flag"]])

    ws_post = GC.open_by_key(SPREADSHEET_ID).worksheet(POST_SHEET)
    ws_post.clear()
    ws_post.append_row(["日付", "投稿時間", "投稿テキスト", "投稿済み", "URL"])

    today = datetime.date.today()
    for idx, c in enumerate(ok):
        summary = "NOK"
        for n in range(MAX_EXTRA_RETRY + 1):
            summary = generate_summary(fetch_thread_text(c["url"]))
            if summary.upper() != "NOK" and not contains_banned(BANNED_WORDS, summary):
                break
            print(f"▶ Extra retry {n+1}/{MAX_EXTRA_RETRY} → {c['title']}")
        if summary.upper() == "NOK" or contains_banned(BANNED_WORDS, summary):
            print(f"▶ Skip (still NG): {c['title']}")
            continue

        post_date = today + datetime.timedelta(days=1 + idx // 2)
        time_str  = "8:00" if idx % 2 == 0 else "15:00"
        tid       = re.search(r'/thread/(\d+)/', c["url"]).group(1)
        utm       = f"?utm_source=x&utm_medium=em-{tid}&utm_campaign={post_date.strftime('%Y%m%d')}"
        post_txt  = f"{summary}\n#マンションコミュニティ\n{c['url']}{utm}"
        ws_post.append_row([post_date.strftime("%Y/%m/%d"), time_str, post_txt, "FALSE", c["url"]])

    print("▶ Done")

# ----------------------------------------------------------------------
if __name__ == "__main__":
    main()
