#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
マンションコミュニティ自動投稿スクリプト
  - 金曜23:00JST実行 → 翌週月曜から投稿
  - URL重複排除・NOKリトライ(5回)・90字CTA固定
"""

import os, base64, datetime, random, re, time, requests, html
from bs4 import BeautifulSoup
import gspread
from google.oauth2.service_account import Credentials

# ------------ 0. 定数 ------------
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
CLAUDE_API_KEY = os.environ["CLAUDE_API_KEY"]
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

MAX_PAGES, POST_COUNT = 3, 14
MAX_RETRY_BASE, MAX_EXTRA_RETRY = 3, 5

HISTORY_SHEET, CANDIDATE_SHEET, POST_SHEET = "スレ履歴", "投稿候補", "投稿予定"
BANNED_WORDS = ["意味不明","共産主義","中国人","血税","糞尿","悩む","スケベ","低俗","トラブル","酷い","劣等感"]
CTA = " 詳しくはこちら👇"; MAX_TITLE_LEN = 90 - len(CTA)

# ------------ 1. Google 認証 ------------
sa = base64.b64decode(os.environ["GCP_SERVICE_ACCOUNT_B64"])
with open("service_account.json","wb") as f: f.write(sa)
gc = gspread.authorize(Credentials.from_service_account_file("service_account.json",scopes=SCOPES))

# ------------ 2. 共通関数 ------------
def contains_banned(words,text): return any(re.search(re.escape(w),text,re.I) for w in words)
def claude_call(prompt,max_tokens):
    res = requests.post("https://api.anthropic.com/v1/messages",
        headers={"x-api-key":CLAUDE_API_KEY,"anthropic-version":"2023-06-01"},
        json={"model":"claude-3-haiku-20240307","temperature":0,"max_tokens":max_tokens,
              "messages":[{"role":"user","content":prompt}]},timeout=45)
    res.raise_for_status(); return res.json()["content"][0]["text"].strip()

# ------------ 3. スクレイパ ------------
def fetch_threads() -> list[dict]:
    """
    23区板を MAX_PAGES 分クロール（重複 ID 除外）。
    - 403/429 が返ったら指数バックオフで 2 回まで再試行
    - ページ間の待機時間を 2 秒に延長
    """
    threads, seen_ids = [], set()
    ua = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    }

    for page in range(1, MAX_PAGES + 1):
        url = f"https://www.e-mansion.co.jp/bbs/board/23ku/?page={page}"

        # ---- 最大 3 回まで指数バックオフでリトライ ----
        success = False
        for retry in range(3):
            try:
                res = requests.get(url, headers=ua, timeout=30)
                if res.status_code == 200:
                    success = True
                    break
                print(f"▶ page{page} status={res.status_code} retry={retry+1}")
            except requests.RequestException as e:
                print(f"▶ page{page} error={e} retry={retry+1}")

            time.sleep(2 ** (retry + 1))   # 2s → 4s → 8s

        if not success:
            print(f"▶ page{page} 取得失敗、スキップ")
            continue

        soup = BeautifulSoup(res.text, "html.parser")
        for a in soup.select("a.component_thread_list_item"):
            tid = re.search(r"/thread/(\d+)/", a["href"]).group(1)
            if tid in seen_ids:
                continue
            seen_ids.add(tid)

            count_tag = a.select_one("span.num_of_item")
            title_tag = a.select_one("div.oneliner.title")
            count = int(count_tag.get_text(strip=True)) if count_tag else 0
            title = html.unescape(title_tag.get_text(strip=True)) if title_tag else "タイトル取得失敗"

            threads.append(
                {"url": f"https://www.e-mansion.co.jp/bbs/thread/{tid}/",
                 "id": tid, "title": title, "count": count})

        time.sleep(2)   # ページ間ディレイを 2 秒に

    return threads

def fetch_thread_text(url,pages=3):
    tid=re.search(r'/thread/(\d+)/',url).group(1)
    ua={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    posts=[]
    for p in range(1,pages+1):
        try:
            r=requests.get(f"https://www.e-mansion.co.jp/bbs/thread/{tid}/?page={p}",headers=ua,timeout=15); r.raise_for_status()
            soup=BeautifulSoup(r.text,"html.parser")
            posts += [t.get_text(strip=True) for t in soup.select('p[itemprop="commentText"]')]
        except requests.RequestException: continue
        time.sleep(0.3)
    return "\n".join(posts)

def fetch_true_title(url: str) -> str:
    """
    スレッド詳細ページから正式タイトルを取得し、
    『【口コミ掲示板】』プレフィックスと
    『｜マンション口コミ・評判（…）』サフィックスを除去
    """
    ua = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        res = requests.get(url, headers=ua, timeout=15)
        res.raise_for_status()
        soup = BeautifulSoup(res.text, "html.parser")

        # <title> タグを取得
        raw = soup.title.get_text(strip=True)

        # ① 先頭の【口コミ掲示板】を削除
        raw = re.sub(r'^【口コミ掲示板】', '', raw)

        # ② 「｜マンション口コミ・評判」以降をカット
        raw = re.sub(r'｜マンション口コミ・評判.*$', '', raw)

        return raw.strip()
    except Exception:
        return ""

# ------------ 4. Sheet util ------------
def load_history():
    rows=gc.open_by_key(SPREADSHEET_ID).worksheet(HISTORY_SHEET).get_all_values()[1:]
    return {r[0]:int(r[1]) for r in rows if len(r)>1 and r[1].isdigit()}
def save_history(hist):
    ws=gc.open_by_key(SPREADSHEET_ID).worksheet(HISTORY_SHEET)
    ws.clear(); ws.append_row(["URL","取得時レス数","最終取得日"])
    ws.append_rows([[u,c,datetime.date.today().isoformat()] for u,c in hist.items()])

# ------------ 5. Claude ラッパ（プロンプトは変更しない） ------------
def judge_risk(text):
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
        ans=claude_call(prompt,200)
        risk="高" if "リスク：高" in ans else "低"
        return risk,ans,("NG" if risk=="高" else "OK")
    except Exception as e:
        return "高",f"[Error] {e}","NG"

def generate_summary(text,max_retry=MAX_RETRY_BASE):
    prompt=f"""あなたは X（旧Twitter）向けのコピーライターです。
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
    for _ in range(max_retry+1):
        try:
            t=claude_call(prompt,80)
        except Exception: time.sleep(2); continue
        if "NOK" in t.upper(): return "NOK"
        t=re.sub(r'\s+',' ',t.strip())
        t=re.sub(r'^\s*タイトル[:：]\s*','',t)
        t=re.sub(r'^.*?[「"](.*?)[」"]$',r'\1',t)
        if contains_banned(BANNED_WORDS,t): time.sleep(1); continue
        if len(t)>MAX_TITLE_LEN: t=t[:MAX_TITLE_LEN].rstrip("、,。. ")+"…"
        return t+CTA
    return "NOK"

# ------------ 6. メイン ------------
def main():
    print("▶ main() start")

    # 1. スレ抽出 & 差分判定
    threads = fetch_threads()
    print(f"▶ 取得スレ数 = {len(threads)}")
    history = load_history()

    diffs = []
    for t in sorted(
        threads,
        key=lambda x: x["count"] - history.get(x["url"], 0),
        reverse=True,
    ):
        if t["url"] not in history:                         # 新規スレは除外
            continue
        if t["count"] - history[t["url"]] <= 0:             # 差分なしは除外
            continue
        diffs.append(t)
        if len(diffs) == 25:
            break
    print(f"▶ 差分候補   = {len(diffs)}")

    # 2. 炎上リスク判定
    candidates, updated = [], {}
    for d in diffs:
        risk, msg, flag = judge_risk(fetch_thread_text(d["url"]))
        candidates.append({**d, "risk": risk, "comment": msg, "flag": flag})
        updated[d["url"]] = d["count"]

    ok = [c for c in candidates if c["flag"] == "OK"][:POST_COUNT]
    random.shuffle(ok)
    print(f"▶ OK候補     = {len(ok)}")

    # 3. 投稿候補シートを更新
    ws_cand = gc.open_by_key(SPREADSHEET_ID).worksheet(CANDIDATE_SHEET)
    ws_cand.clear()
    ws_cand.append_row(
        ["URL", "差分レス数", "スレッドタイトル", "炎上リスク", "コメント", "投稿可否"]
    )
    for c in sorted(
        candidates,
        key=lambda x: x["count"] - history.get(x["url"], 0),
        reverse=True,
    ):
        ws_cand.append_row(
            [
                c["url"],
                c["count"] - history.get(c["url"], 0),
                c["title"],
                c["risk"],
                c["comment"],
                c["flag"],
            ]
        )
    print(f"▶ 投稿候補シート更新 = {len(candidates)} 行")

    # 4. 投稿予定シート（最大 14 行・7 日均等配置）
    ws_post = gc.open_by_key(SPREADSHEET_ID).worksheet(POST_SHEET)
    ws_post.clear()
    ws_post.append_row(["日付", "投稿時間", "投稿テキスト", "投稿済み", "URL"])

    today = datetime.date.today()
    base_monday = today + datetime.timedelta(days=((7 - today.weekday()) % 7 or 7))

    scheduled, row_count = set(), 0
    for c in ok:
        if c["url"] in scheduled:
            continue

        # タイトル生成（NOK リトライ）
        title = "NOK"
        for _ in range(MAX_EXTRA_RETRY + 1):
            title = generate_summary(fetch_thread_text(c["url"]))
            if title.upper() != "NOK":
                break
        if title.upper() == "NOK":
            continue

        # 7 日 × 2 枠に均等配置
        day_offset = row_count % 7
        post_date = base_monday + datetime.timedelta(days=day_offset)
        time_str = "8:00" if row_count // 7 == 0 else "15:00"

        # 投稿テキスト生成
        tid = re.search(r"/thread/(\d+)/", c["url"]).group(1)
        utm = f"?utm_source=x&utm_medium=em-{tid}&utm_campaign={post_date:%Y%m%d}"
        post_txt = f"{title}\n#マンションコミュニティ\n{c['url']}{utm}"

        ws_post.append_row(
            [
                post_date.strftime("%Y/%m/%d"),
                time_str,
                post_txt,
                "FALSE",
                c["url"],
            ]
        )
        scheduled.add(c["url"])
        row_count += 1
        if row_count == POST_COUNT:          # 14 行で終了
            break
    print(f"▶ 投稿行数   = {row_count}")

    # 5. 履歴更新
    if os.getenv("TEST_MODE") != "1":
        save_history({**history, **{u: updated[u] for u in scheduled}})

    print("▶ Done")

# ------------ 7. 実行 ------------
if __name__=="__main__":
    main()
