#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
マンションコミュニティ自動投稿スクリプト
  - 週 1 回（金曜 23:00JST）の実行を想定
  - 翌週月曜から 8:00 / 15:00 で投稿キューを生成
  - 同じスレ URL の重複を完全排除
"""

import os, base64, datetime, random, re, time, requests, html
from bs4 import BeautifulSoup
import gspread
from google.oauth2.service_account import Credentials

# ──────────────────────────────
# 0. 環境設定
# ──────────────────────────────
print(f"▶ TEST_MODE: {os.getenv('TEST_MODE')}")

SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
CLAUDE_API_KEY = os.environ["CLAUDE_API_KEY"]
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

MAX_PAGES        = 3
POST_COUNT       = 14
MAX_RETRY_BASE   = 3     # Claude ベースリトライ
MAX_EXTRA_RETRY  = 2     # タイトル NG 追加リトライ

HISTORY_SHEET   = "スレ履歴"
CANDIDATE_SHEET = "投稿候補"
POST_SHEET      = "投稿予定"

BANNED_WORDS = [
    "意味不明", "共産主義", "中国人", "血税", "糞尿",
    "悩む", "スケベ", "低俗", "トラブル", "酷い", "劣等感"
]

CTA = " 詳しくはこちら👇"
MAX_TITLE_LEN = 90 - len(CTA)  # → 83 文字

# ──────────────────────────────
# 1. Google 認証
# ──────────────────────────────
json_bytes = base64.b64decode(os.environ["GCP_SERVICE_ACCOUNT_B64"])
with open("service_account.json", "wb") as f:
    f.write(json_bytes)

creds = Credentials.from_service_account_file("service_account.json",
                                              scopes=SCOPES)
gc = gspread.authorize(creds)
print("▶ gspread authorized")

# ──────────────────────────────
# 2. ヘルパ
# ──────────────────────────────
def contains_banned(words: list[str], text: str) -> bool:
    return any(re.search(re.escape(w), text, re.IGNORECASE) for w in words)

# ──────────────────────────────
# 3. 掲示板スクレイプ
# ──────────────────────────────
def fetch_threads():
    threads, seen_ids = [], set()
    headers = {"User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"}
    for page in range(1, MAX_PAGES + 1):
        url = f"https://www.e-mansion.co.jp/bbs/board/23ku/?page={page}"
        res = requests.get(url, headers=headers, timeout=30)
        if res.status_code != 200:
            continue
        blocks = re.findall(
            r'<a href="/bbs/thread/(\d+)/" class="component_thread_list_item link.*?<span class="num_of_item">(\d+)</span>.*?<div class="oneliner title"[^>]*>(.*?)</div>',
            res.text, re.DOTALL)
        for tid, cnt, ttl in blocks:
            if tid in seen_ids:             # ★ 重複IDを除外
                continue
            seen_ids.add(tid)
            threads.append({
                "url": f"https://www.e-mansion.co.jp/bbs/thread/{tid}/",
                "id": tid,
                "title": html.unescape(ttl).strip(),
                "count": int(cnt)
            })
    return threads

def fetch_thread_posts(tid: str, max_pages: int = 3, delay=0.3) -> list[str]:
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    posts = []
    for p in range(1, max_pages + 1):
        url = f"https://www.e-mansion.co.jp/bbs/thread/{tid}/?page={p}"
        try:
            r = requests.get(url, headers=headers, timeout=15)
            r.raise_for_status()
        except requests.RequestException:
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        posts += [t.get_text(strip=True) for t in soup.select('p[itemprop="commentText"]')]
        time.sleep(delay)
    return posts

def fetch_thread_text(url: str) -> str:
    tid = re.search(r'/thread/(\d+)/', url).group(1)
    return "\n".join(fetch_thread_posts(tid))

# ──────────────────────────────
# 4. スプレッドシート I/O
# ──────────────────────────────
def load_history() -> dict[str, int]:
    sheet = gc.open_by_key(SPREADSHEET_ID).worksheet(HISTORY_SHEET)
    return {r[0]: int(r[1]) for r in sheet.get_all_values()[1:]
            if len(r) > 1 and r[1].isdigit()}

def save_history(hist: dict[str, int]):
    ws = gc.open_by_key(SPREADSHEET_ID).worksheet(HISTORY_SHEET)
    ws.clear()
    ws.append_row(["URL", "取得時レス数", "最終取得日"])
    ws.append_rows([[u, c, datetime.date.today().isoformat()] for u, c in hist.items()])

# ──────────────────────────────
# 5. Claude API ラッパ
# ──────────────────────────────
def claude_call(prompt: str, max_tokens: int):
    headers = {"x-api-key": CLAUDE_API_KEY, "anthropic-version": "2023-06-01"}
    body = {
        "model": "claude-3-haiku-20240307",
        "temperature": 0,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}]
    }
    res = requests.post("https://api.anthropic.com/v1/messages", headers=headers,
                        json=body, timeout=45)
    res.raise_for_status()
    return res.json()["content"][0]["text"].strip()

def judge_risk(text: str):
    prompt = f"""SNS 炎上リスクのレビューをしてください。
本文（日本語）について、炎上につながる要素があるか厳格に判定してください。

## 判定基準
▼ リスク：高
1. 人種・国籍・宗教・性別・地域などの **属性差別**、誹謗中傷、蔑称、ヘイト表現
2. 公序良俗に反する **違法行為の助長**・推奨・自慢
3. センシティブな事件・災害・政治・宗教に関する **一方的または扇動的表現**
4. 個人や企業への **攻撃的・挑発的な言及**、名誉毀損、プライバシー暴露
5. 虐待・残虐・性的搾取など **不快・暴力的コンテンツ**
▼ リスク：低
上記 1–5 のいずれにも **該当しない** 場合。
## 出力フォーマット（厳守）
- 1 行目：**「リスク：高」** または **「リスク：低」** のみ
- 2 行目：判定理由（条件番号を明記するとベター）
- 3 行目以降は何も書かない
- 条件を満たせない場合は **「ERROR」** とだけ書く
--- 本文 ---
{text}"""
    try:
        msg = claude_call(prompt, 200)
        risk = "高" if "リスク：高" in msg else "低"
        return risk, msg, ("NG" if risk == "高" else "OK")
    except Exception as e:
        return "高", f"[Error] {e}", "NG"

def generate_summary(text: str, max_retry=MAX_RETRY_BASE):
    base_prompt = f"""あなたは X（旧Twitter）向けのコピーライターです。
掲示板スレッド本文を読み、読者が続きをクリックしたくなる **前向きで長め** の日本語タイトルを 1 本だけ生成してください。
### 出力仕様（必ず守る）
1. **タイトル本文のみ** を 1 行で出力
   - 行頭に「-」「–」「・」など記号を付けない
   - 接頭辞「タイトル:」や解説文（例: 「禁止語を含まず～」「90文字以内で～」）を付けない
   - 同一の文を 2 回以上繰り返さない
   - 改行・かぎ括弧・箇条書き・絵文字・記号説明を付けない
   - 90 文字以内
2. 本文冒頭に **地名・駅名・数字** いずれかを入れて目を引く構成にする
3. 末尾に **4から7文字程度の CTA** を付ける（例: 詳しくはこちら👇、続きはこちら👇）
4. 上記を 1 つでも満たせない、または禁止語を 1 語でも含む場合は **NOK** とだけ出力する
   - NOK 以外の文字を一切書かない
### 禁止語
{', '.join(BANNED_WORDS)}
--- 本文 ---
{text}"""
    for _ in range(max_retry + 1):
        try:
            title = claude_call(base_prompt, 80)
        except Exception:
            time.sleep(2); continue

        if "NOK" in title.upper():
            return "NOK"

        title = re.sub(r'\s+', ' ', title.strip())
        title = re.sub(r'^\s*タイトル[:：]\s*', '', title)
        title = re.sub(r'^.*?[「"](.*?)[」"]$', r'\1', title)

        if contains_banned(BANNED_WORDS, title):
            time.sleep(1); continue
        if len(title) > MAX_TITLE_LEN:
            title = title[:MAX_TITLE_LEN].rstrip("、,。. ") + "…"
        return title + CTA
    return "NOK"

# ──────────────────────────────
# 6. メイン
# ──────────────────────────────
def main():
    print("▶ main() start")
    threads  = fetch_threads()
    history  = load_history()
    diffs    = []

    # 上位 20 スレ抽出
    for t in sorted(threads,
                    key=lambda x: x["count"] - history.get(x["url"], 0),
                    reverse=True):
        if t["url"] in history and t["count"] - history[t["url"]] <= 0:
            continue
        if t["url"] not in history and t["count"] < 100:
            continue
        diffs.append(t)
        if len(diffs) == 20:
            break

    # 炎上リスク判定
    candidates, updated = [], {}
    for d in diffs:
        txt = fetch_thread_text(d["url"])
        risk, msg, flag = judge_risk(txt)
        candidates.append({**d, "risk": risk, "comment": msg, "flag": flag})
        updated[d["url"]] = d["count"]

    ok = [c for c in candidates if c["flag"] == "OK"][:POST_COUNT]
    random.shuffle(ok)

    # 投稿予定シート準備
    ws_post = gc.open_by_key(SPREADSHEET_ID).worksheet(POST_SHEET)
    ws_post.clear()
    ws_post.append_row(["日付", "投稿時間", "投稿テキスト", "投稿済み", "URL"])

    today = datetime.date.today()
    base_monday = today + datetime.timedelta(days=((7 - today.weekday()) % 7 or 7))
    scheduled = set()                 # ★ 重複URL防止

    for idx, c in enumerate(ok):
        if c["url"] in scheduled:
            continue
        scheduled.add(c["url"])

        title = "NOK"
        for _ in range(MAX_EXTRA_RETRY + 1):
            title = generate_summary(fetch_thread_text(c["url"]))
            if title.upper() != "NOK":
                break
        if title.upper() == "NOK":
            continue

        post_date = base_monday + datetime.timedelta(days=idx // 2)
        time_str  = "8:00" if idx % 2 == 0 else "15:00"
        tid       = re.search(r'/thread/(\d+)/', c["url"]).group(1)
        utm       = f"?utm_source=x&utm_medium=em-{tid}&utm_campaign={post_date:%Y%m%d}"
        post_txt  = f"{title}\n#マンションコミュニティ\n{c['url']}{utm}"
        ws_post.append_row([post_date.strftime("%Y/%m/%d"), time_str,
                            post_txt, "FALSE", c["url"]])

    # 履歴保存（TEST_MODE=1 のときはスキップ）
    if os.getenv("TEST_MODE") != "1":
        save_history({**history, **{u: updated[u] for u in scheduled}})

    print("▶ Done")

# ──────────────────────────────
if __name__ == "__main__":
    main()
