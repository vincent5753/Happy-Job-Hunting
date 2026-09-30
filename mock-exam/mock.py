#!/usr/bin/env python3
"""Mock_Exam — 本地模擬面試。TTS 出題、麥克風錄音、離線轉錄、機械式量測。

🔴 全程離線（預設 HF_HUB_OFFLINE=1）。答案含公司內部資訊與薪資，不得送雲端 API。
   計分要用 mlx-whisper 模型；第一次加 --allow-download 下載（與 transcribe 共用快取），之後一律斷網。

🔴 這支工具只量測，不評價內容 —— 秒數、錨點命中、必說句、紅線詞、填充詞密度。
   「這個答案好不好」不在它的能力範圍內，也刻意不給它。不做內容判斷的工具
   不可能捏造事實，這是它能被信任的唯一理由。

用法（每一種都會用麥克風錄你的回答；需要 macOS 的 say、afplay 與 ffmpeg）：
    uv run mock.py banks/example.toml --allow-download        # 第一次：順便下載計分用的模型
    uv run mock.py banks/example.toml                         # 整場
    uv run mock.py banks/example.toml --only intro,incident   # 只練指定題
    uv run mock.py banks/example.toml --prerender             # 只算題目音檔就結束（不錄音）
    uv run mock.py banks/example.toml --no-score              # 只練不評（不載模型）
    uv run mock.py banks/example.toml --text                  # 題目改印成文字、不放音（仍然錄音）
    uv run mock.py banks/example.toml --voices                # 試聽同語系的聲音就結束

環境變數：MOCK_AUDIO_DEVICE（錄音裝置名稱，預設 MacBook Pro Microphone）、
MOCK_SESSIONS_DIR（練習紀錄寫到哪，預設 ~/Desktop/career-ops/interview-prep/sessions/，不存在就略過）。
紅線規則讀 banks/_rules.local.toml（有的話，不進版控），否則讀範例 banks/_rules.toml。
"""

from __future__ import annotations

import argparse
import array
import curses
import os
import re
import shutil
import subprocess
import sys
import time
import threading
import tomllib
import wave
from datetime import date, datetime
from pathlib import Path

# HF_HUB_OFFLINE 由 main() 在解析參數後設定：huggingface_hub 在 import 時就固定讀這個變數，
# 而 mlx_whisper 只在 warm_model()／轉錄時才 import，所以在那之前設好就有效。

HERE = Path(__file__).resolve().parent
MODEL = "mlx-community/whisper-large-v3-mlx"
# Whisper 的重複偵測靠溫度回退。只給 0.0 會關掉整個機制，靜音處會鬼打牆。
TEMPERATURE = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)
# 🔴 不要寫死索引。avfoundation 的編號會隨著藍牙耳機接上／拔掉整組位移，
# 錄到 Steam Streaming Microphone 這種虛擬裝置只會得到一整段數位靜音，
# 而 Whisper 對靜音會生出「請不吝點贊 訂閱」之類的幻覺，整場模擬就白跑了。
# 改成每次啟動按名字去問 ffmpeg 要索引；用 MOCK_AUDIO_DEVICE 可覆寫。
AUDIO_DEVICE_NAME = os.environ.get("MOCK_AUDIO_DEVICE", "MacBook Pro Microphone")
MAX_ANSWER_SECONDS = 300
BEEP = "/System/Library/Sounds/Tink.aiff"
# macOS 內建的搞笑音效語音。試聽時濾掉，不然 41 個聲音裡一半在唱歌。
NOVELTY_VOICES = {
    "Albert", "Bad News", "Bahh", "Bells", "Boing", "Bubbles", "Cellos",
    "Deranged", "Good News", "Hysterical", "Jester", "Junior", "Organ",
    "Pipe Organ", "Princess", "Ralph", "Superstar", "Trinoids",
    "Whisper", "Wobble", "Zarvox", "Bahh", "Bad News",
}

# 練習紀錄寫進 career-ops 的 interview-prep/sessions/；路徑不同就設 MOCK_SESSIONS_DIR
SESSIONS_DIR = Path(os.environ.get(
    "MOCK_SESSIONS_DIR", Path.home() / "Desktop/career-ops/interview-prep/sessions")).expanduser()


# --------------------------------------------------------------------------- #
# 讀題庫
# --------------------------------------------------------------------------- #

def load_toml(path: Path) -> dict:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def load_bank(path: Path) -> tuple[dict, list[dict]]:
    raw = load_toml(path)
    meta = raw.get("bank", {})
    questions = raw.get("question", [])
    if not questions:
        sys.exit(f"❌ {path} 裡沒有任何 [[question]]")
    seen: set[str] = set()
    for q in questions:
        qid = q.get("id")
        if not qid:
            sys.exit(f"❌ {path} 有一題沒有 id")
        if qid in seen:
            sys.exit(f"❌ {path} 的 id 重複：{qid}")
        seen.add(qid)
        if not q.get("text"):
            sys.exit(f"❌ 題目 {qid} 沒有 text")
    return meta, questions


# --------------------------------------------------------------------------- #
# 題目音檔：預先算好，不要在場中即時合成
# --------------------------------------------------------------------------- #

def question_audio(bank_path: Path, qid: str) -> Path:
    return HERE / "audio/questions" / f"{bank_path.stem}-{qid}.aiff"


def prerender(bank_path: Path, meta: dict, questions: list[dict]) -> int:
    """把題目念成 aiff 存起來。已存在且比題庫新的就跳過。"""
    bank_mtime = bank_path.stat().st_mtime
    built = 0
    for q in questions:
        # 每題可以自己指定聲音 —— 同一份題庫要混中英文面試官時用得到
        voice = q.get("voice", meta.get("voice", "Meijia"))
        rate = str(q.get("rate", meta.get("rate", 175)))
        out = question_audio(bank_path, q["id"])
        if out.exists() and out.stat().st_mtime >= bank_mtime:
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["say", "-v", voice, "-r", rate, "-o", str(out), q["text"]],
            check=True,
        )
        built += 1
    return built


def audition(meta: dict, questions: list[dict]) -> None:
    """把第一題用所有同語系的聲音念一遍，讓耳朵選而不是讓我猜。"""
    lang = meta.get("lang", "zh")
    prefix = "zh_TW" if lang.startswith("zh") else "en_"
    listing = subprocess.run(
        ["say", "-v", "?"], capture_output=True, text=True, check=True
    ).stdout
    voices = []
    for line in listing.splitlines():
        # ⚠ 對齊欄寬只對短名字成立："Eddy (Chinese (Taiwan)) zh_TW" 只有一個空格，
        #   用 \s{2,} 切會只抓到 Meijia。靠後面的 "#" 定位才穩。
        m = re.match(r"^(.*?)\s+([a-z]{2}_[A-Z]{2})\s+#", line)
        if m and m.group(2).startswith(prefix):
            name = m.group(1).strip()
            if name not in NOVELTY_VOICES:
                voices.append((name, m.group(2)))
    text = questions[0]["text"]
    rate = str(meta.get("rate", 175))
    print(f"  試聽 {len(voices)} 個聲音，念的是：{text[:30]}…\n")
    for name, loc in voices:
        print(f"  ▶ {name}  ({loc})")
        subprocess.run(["say", "-v", name, "-r", rate, text], check=False)
    print("\n  選好了寫進題庫的 [bank] 段：voice = \"名字\"（單題覆寫就寫在該題底下）")


def play(path: Path | str) -> None:
    subprocess.run(["afplay", str(path)], check=False)


# --------------------------------------------------------------------------- #
# 錄音
# --------------------------------------------------------------------------- #

def list_audio_devices() -> list[tuple[str, str]]:
    """問 ffmpeg 現在有哪些 avfoundation 錄音裝置，回傳 [(index, name)]。"""
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-f", "avfoundation",
         "-list_devices", "true", "-i", ""],
        capture_output=True, text=True,
    )
    devices, in_audio = [], False
    for line in proc.stderr.splitlines():
        if "AVFoundation audio devices" in line:
            in_audio = True
            continue
        if in_audio:
            m = re.search(r"\[(\d+)\] (.+)$", line)
            if not m:
                break
            devices.append((m.group(1), m.group(2).strip()))
    return devices


def resolve_audio_device(name: str) -> str:
    """把裝置名稱換成 avfoundation 的 :index。找不到就把清單印出來讓人自己選。"""
    devices = list_audio_devices()
    for idx, dev in devices:
        if dev == name:
            return f":{idx}"
    for idx, dev in devices:
        if name.lower() in dev.lower():
            return f":{idx}"
    listing = "\n".join(f"    [{i}] {d}" for i, d in devices) or "    （抓不到任何裝置）"
    sys.exit(f"找不到錄音裝置「{name}」。現有的是：\n{listing}\n"
             f"用 --device '<名稱>' 或 MOCK_AUDIO_DEVICE 指定。")


# 名字裡有這些字的是虛擬／迴路裝置，錄起來八成是一整段靜音。
VIRTUAL_HINTS = ("blackhole", "steam", "teams", "soundflower", "loopback", "聚集裝置")


def pick_audio_device(default_name: str) -> tuple[str, str]:
    """鍵盤上下選錄音裝置。回傳 (":index", 名稱)。

    裝置索引會隨藍牙耳機接上／拔掉整組位移，寫死索引錄到虛擬裝置就是
    一整場靜音。與其猜，不如每次開場花三秒讓人自己確認。"""
    devices = list_audio_devices()
    if not devices:
        sys.exit("❌ 抓不到任何 avfoundation 錄音裝置。")
    if not sys.stdin.isatty():
        return resolve_audio_device(default_name), default_name

    start = 0
    for i, (_, name) in enumerate(devices):
        if name == default_name:
            start = i
            break

    def ui(stdscr) -> int:
        curses.curs_set(0)
        stdscr.keypad(True)
        sel = start
        while True:
            stdscr.erase()
            stdscr.addstr(0, 0, "  選錄音裝置　↑↓ 移動　Enter 確定　q 離開")
            stdscr.addstr(1, 0, "  🔴 選錯會錄到一整段靜音，轉錄只會生出幻覺文字。")
            for i, (idx, name) in enumerate(devices):
                warn = "  ⚠ 虛擬裝置" if any(
                    h in name.lower() for h in VIRTUAL_HINTS) else ""
                line = f"  {'▶' if i == sel else ' '} [{idx}] {name}{warn}"
                stdscr.addstr(3 + i, 0, line,
                              curses.A_REVERSE if i == sel else curses.A_NORMAL)
            stdscr.refresh()
            k = stdscr.getch()
            if k in (curses.KEY_UP, ord("k")):
                sel = (sel - 1) % len(devices)
            elif k in (curses.KEY_DOWN, ord("j")):
                sel = (sel + 1) % len(devices)
            elif k in (curses.KEY_ENTER, 10, 13):
                return sel
            elif k == 27:
                # keypad 沒把 ↑↓ 併成 KEY_UP/KEY_DOWN 時會拆成 ESC [ A/B 三個位元組
                # 進來。直接當 ESC 處理會把游標鍵誤判成「離開」，所以自己解一次。
                stdscr.nodelay(True)
                seq = (stdscr.getch(), stdscr.getch())
                stdscr.nodelay(False)
                if seq == (ord("["), ord("A")):
                    sel = (sel - 1) % len(devices)
                elif seq == (ord("["), ord("B")):
                    sel = (sel + 1) % len(devices)
                else:
                    return -1
            elif k == ord("q"):
                return -1

    chosen = curses.wrapper(ui)
    if chosen < 0:
        sys.exit("取消。")
    idx, name = devices[chosen]
    return f":{idx}", name


def silent(path: Path) -> bool:
    """整段是數位靜音＝根本沒錄到。要當場擋下來，不是等轉錄完才發現。"""
    try:
        with wave.open(str(path)) as w:
            if w.getsampwidth() != 2:
                return False
            buf = array.array("h")
            buf.frombytes(w.readframes(w.getnframes()))
    except Exception:
        return True
    return not buf or max(abs(x) for x in buf) == 0


def record(out_path: Path, device: str) -> float:
    """開錄，等 Enter 停。回傳實際音檔長度（秒），不是按鍵的時間差。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "avfoundation", "-i", device,
            "-ar", "16000", "-ac", "1",
            "-t", str(MAX_ANSWER_SECONDS),
            str(out_path),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    started = time.monotonic()
    try:
        input()
    except (EOFError, KeyboardInterrupt):
        pass
    try:
        proc.communicate(b"q", timeout=5)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    if not out_path.exists() or out_path.stat().st_size == 0:
        print("   ⚠ 沒有錄到聲音。第一次跑要在 macOS 權限視窗按允許，然後重跑這一題。")
        return 0.0
    try:
        with wave.open(str(out_path)) as wf:
            return wf.getnframes() / float(wf.getframerate())
    except wave.Error:
        return max(0.0, time.monotonic() - started)


# --------------------------------------------------------------------------- #
# 轉錄
# --------------------------------------------------------------------------- #

_CONVERTER = None


def normalize(text: str, lang: str) -> str:
    """large-v3 在中文會漏簡體字（"中华资安"、"专案"、"维护"），實測是穩定現象
    而非偶發。錨點是繁體寫的，不轉換就會把講對的東西算成沒講到 —— 這不是
    美化輸出，是量測正確性的前提。s2twp 連帶處理兩岸詞彙差異。"""
    global _CONVERTER
    if not lang.startswith("zh"):
        return text
    if _CONVERTER is None:
        import opencc
        _CONVERTER = opencc.OpenCC("s2twp")
    return _CONVERTER.convert(text)


def transcribe_all(clips: list[tuple[dict, Path, float]], lang: str) -> dict[str, str]:
    """一個行程內轉完所有片段。mlx_whisper 的 ModelHolder 是類別層快取，
    模型只會載一次 —— 這是不另外開常駐行程的原因。"""
    import mlx_whisper

    texts: dict[str, str] = {}
    for q, clip, dur in clips:
        if dur <= 0:
            texts[q["id"]] = ""
            continue
        result = mlx_whisper.transcribe(
            str(clip),
            path_or_hf_repo=MODEL,
            language=lang or None,
            temperature=TEMPERATURE,
            verbose=None,
        )
        texts[q["id"]] = normalize((result.get("text") or "").strip(), lang)
    return texts


def warm_model() -> None:
    """先把模型載進記憶體，讓等待落在開場而不是收場。"""
    import mlx_whisper
    from mlx_whisper.transcribe import ModelHolder
    import mlx.core as mx

    ModelHolder.get_model(MODEL, mx.float16)
    _ = mlx_whisper  # 明示依賴，避免被當成未使用


# --------------------------------------------------------------------------- #
# 量測
# --------------------------------------------------------------------------- #

def count_fillers(text: str, rules: dict, lang: str) -> list[tuple[str, int]]:
    words = rules.get("filler", {}).get(lang, [])
    low = text.lower()
    hits = [(w, low.count(w.lower())) for w in words]
    return [(w, n) for w, n in hits if n]


def check_redlines(text: str, rules: dict) -> list[tuple[str, str]]:
    out = []
    for rule in rules.get("redline", []):
        pattern = rule.get("pattern", "")
        if pattern and re.search(pattern, text):
            out.append((pattern, rule.get("why", "")))
    return out


def _said(needle: str, text: str) -> bool:
    """錨點含 "|" 時當交替樣式處理。用途是吸收 ASR 的固定失真 ——
    large-v3 把「一年半」聽成「一年辦」是穩定現象，寫成「一年半|一年辦」
    才不會把講對的東西算成沒講到。"""
    low = text.lower()
    return any(part.strip().lower() in low for part in needle.split("|") if part.strip())


def measure(q: dict, text: str, dur: float, rules: dict, lang: str) -> dict:
    target = q.get("target_seconds")
    tol = q.get("tolerance", 15)
    verdict = None
    if target:
        if dur < target - tol:
            verdict = "太短"
        elif dur > target + tol:
            verdict = "太長"
        else:
            verdict = "在範圍內"
    anchors = q.get("anchors", [])
    hit = [a for a in anchors if _said(a, text)]
    miss = [a for a in anchors if not _said(a, text)]
    must = q.get("must_say", [])
    must_miss = [m for m in must if not _said(m, text)]
    return {
        "seconds": round(dur, 1),
        "target": target,
        "tolerance": tol,
        "verdict": verdict,
        "anchor_hit": hit,
        "anchor_miss": miss,
        "must_miss": must_miss,
        "redlines": check_redlines(text, rules),
        "fillers": count_fillers(text, rules, lang),
        "per_minute": round(
            sum(n for _, n in count_fillers(text, rules, lang)) / (dur / 60), 1
        ) if dur > 5 else None,
    }


# --------------------------------------------------------------------------- #
# 輸出
# --------------------------------------------------------------------------- #

def print_report(meta: dict, rows: list[tuple[dict, str, dict]]) -> None:
    print("\n" + "=" * 68)
    print(f"  {meta.get('name', '?')} · 機械量測（工具不評內容）")
    print("=" * 68)
    for q, text, m in rows:
        print(f"\n▶ {q['id']}  {q['text'][:40]}")
        tgt = f" / 目標 {m['target']}±{m['tolerance']}s" if m["target"] else ""
        print(f"   秒數      {m['seconds']}s{tgt}  {m['verdict'] or ''}")
        if q.get("anchors"):
            print(f"   錨點      {len(m['anchor_hit'])}/{len(q['anchors'])} 命中"
                  + (f"　漏：{'、'.join(m['anchor_miss'])}" if m["anchor_miss"] else ""))
        if m["must_miss"]:
            print(f"   🔴 必說   沒講到：{'、'.join(m['must_miss'])}")
        for pattern, why in m["redlines"]:
            print(f"   🔴 紅線   /{pattern}/ — {why}")
        if m["fillers"]:
            detail = "、".join(f"{w}×{n}" for w, n in m["fillers"])
            print(f"   填充詞    {detail}"
                  + (f"　（{m['per_minute']} 次/分）" if m["per_minute"] else ""))
        if q.get("hint"):
            print(f"   提示      {q['hint']}")
    print()


def write_session(meta: dict, rows: list[tuple[dict, str, dict]], out_dir: Path,
                  stamp: str) -> Path | None:
    """寫成 career-ops/interview-prep/sessions/ 吃得懂的格式（source: mock）。
    schema 早就有 mock 這個 enum，所以下游 weekly-digest / debrief / patterns
    零改動就讀得到。"""
    if not SESSIONS_DIR.is_dir():
        return None
    slug = meta.get("slug") or re.sub(
        r"[^a-z0-9]+", "-", meta.get("name", "mock").lower()
    ).strip("-") or "mock"
    # stamp 帶到秒，同一天重跑不會蓋掉前一次；frontmatter 的 date 仍是純日期。
    path = SESSIONS_DIR / f"{stamp}-{slug}-mock.md"
    lines = [
        "---",
        f"company: {meta.get('company', '')}",
        f"role: {meta.get('role', '')}",
        f"round: {meta.get('round', 'hiring-manager')}",
        f"date: {date.today().isoformat()}",
        "interviewer_role: Mock_Exam (TTS)",
        "source: mock",
        "---",
        "",
    ]
    for i, (q, text, m) in enumerate(rows, 1):
        lines += [
            f"## Q{i} · {q['id']}",
            f"**Interviewer:** {q['text']}",
            f"**Candidate:** {text or '（沒有錄到內容）'}",
            "",
            f"<!-- mock: {m['seconds']}s target={m['target']} "
            f"anchors={len(m['anchor_hit'])}/{len(q.get('anchors', []))} "
            f"must_miss={','.join(m['must_miss']) or '-'} "
            f"redlines={len(m['redlines'])} -->",
            "",
        ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #

def main() -> None:
    ap = argparse.ArgumentParser(description="本地模擬面試")
    ap.add_argument("bank", type=Path, help="題庫 toml（banks/*.toml）")
    ap.add_argument("--only", default="", help="只練這幾題，逗號分隔的 id")
    ap.add_argument("--prerender", action="store_true", help="只算題目音檔就結束")
    ap.add_argument("--no-score", action="store_true", help="只練不評，不載模型")
    ap.add_argument("--text", action="store_true", help="題目改印成文字、不放音（仍然會錄你的回答）")
    ap.add_argument("--voices", action="store_true", help="試聽所有同語系的聲音就結束")
    ap.add_argument("--device", default="", help="錄音裝置名稱，給了就跳過選單")
    ap.add_argument("--allow-download", action="store_true",
                    help="允許連線下載計分模型（只有第一次需要）；預設離線 HF_HUB_OFFLINE=1")
    args = ap.parse_args()
    if not args.allow_download:
        os.environ["HF_HUB_OFFLINE"] = "1"

    for tool in ("say", "afplay", "ffmpeg"):
        if not shutil.which(tool):
            sys.exit(f"❌ 找不到 {tool}")

    bank_path = args.bank if args.bank.is_absolute() else HERE / args.bank
    if not bank_path.is_file():
        sys.exit(f"❌ 題庫不存在：{bank_path}")
    meta, questions = load_bank(bank_path)
    # 自己的紅線（含現薪等個人數字）放 _rules.local.toml，不進版控；沒有就用範例
    rules_path = HERE / "banks/_rules.local.toml"
    if not rules_path.is_file():
        rules_path = HERE / "banks/_rules.toml"
    rules = load_toml(rules_path)
    lang = meta.get("lang", "zh")

    if args.only:
        wanted = [s.strip() for s in args.only.split(",") if s.strip()]
        unknown = [w for w in wanted if w not in {q["id"] for q in questions}]
        if unknown:
            sys.exit(f"❌ 題庫裡沒有這些 id：{', '.join(unknown)}")
        questions = [q for q in questions if q["id"] in wanted]

    if args.voices:
        audition(meta, questions)
        return

    if not args.text:
        built = prerender(bank_path, meta, questions)
        if built:
            print(f"  ✓ 算好 {built} 個題目音檔")
    if args.prerender:
        return

    # 模型丟到背景載。選裝置、讀說明、聽題目的時間本來就是空的，
    # 讓載入跟它重疊，開場就不用乾等那一段。
    warming = None
    if not args.no_score:
        warming = threading.Thread(target=warm_model, daemon=True)
        warming.start()

    device_name = args.device or AUDIO_DEVICE_NAME
    if args.device or os.environ.get("MOCK_AUDIO_DEVICE"):
        device = resolve_audio_device(device_name)
    else:
        device, device_name = pick_audio_device(device_name)
    print(f"  錄音裝置 {device} {device_name}")

    print(f"\n  {meta.get('name', '?')} · {len(questions)} 題 · 語言 {lang}")
    print("  每題：聽完題目 → 提示音 → 開始講 → 講完按 Enter")
    print("  🔴 不要看稿。看稿練出來的是朗讀，不是回答。\n")
    input("  按 Enter 開始…")

    # 目錄名帶到秒（YYYY-MM-DD-HHMMSS），同一天重跑各自獨立，
    # 而且字串排序＝時間排序，取最新就是排序後的最後一個。
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    run_dir = HERE / "out" / f"{stamp}-{bank_path.stem}"
    clips: list[tuple[dict, Path, float]] = []
    for i, q in enumerate(questions, 1):
        print(f"\n[{i}/{len(questions)}] {q['id']}")
        if args.text:
            print(f"  ❓ {q['text']}")
        else:
            play(question_audio(bank_path, q["id"]))
        play(BEEP)
        print("  ● 錄音中 —— 講完按 Enter")
        clip = run_dir / "clips" / f"{q['id']}.wav"
        dur = record(clip, device)
        print(f"  ■ {dur:.1f}s")
        if silent(clip):
            print("  🔴 這段是全靜音，麥克風沒吃到聲音。先停下來修，不要繼續錄。")
            sys.exit(f"錄音裝置 {device}（{device_name}）沒有輸入訊號。")
        clips.append((q, clip, dur))

    if args.no_score:
        print(f"\n  錄音在 {run_dir / 'clips'}。要評分就重跑一次不要加 --no-score。")
        return

    if warming and warming.is_alive():
        print("\n  等轉錄模型載完…", end="", flush=True)
        t0 = time.monotonic()
        warming.join()
        print(f" {time.monotonic() - t0:.0f}s")
    print("\n  轉錄中…")
    texts = transcribe_all(clips, lang)
    rows = [(q, texts[q["id"]], measure(q, texts[q["id"]], dur, rules, lang))
            for q, _, dur in clips]

    print_report(meta, rows)

    run_dir.mkdir(parents=True, exist_ok=True)
    transcript = run_dir / "transcript.md"
    transcript.write_text(
        "\n\n".join(f"## {q['id']}\n{text}" for q, text, _ in rows), encoding="utf-8"
    )
    print(f"  逐字稿  {transcript}")
    session = write_session(meta, rows, run_dir, stamp)
    if session:
        print(f"  session {session}")
    else:
        print(f"  session 沒寫：找不到 {SESSIONS_DIR}（career-ops 的 interview-prep/sessions/）。"
              "報告與逐字稿照常在上面；要寫到別處就設 MOCK_SESSIONS_DIR")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        # Ctrl-C 在等 afplay／ffmpeg 的時候一定會踩在 subprocess.wait() 上，
        # 噴出來的 traceback 全是 subprocess 內部，對使用者零資訊。
        print("\n  中斷。已錄到的片段留在 out/。")
        sys.exit(130)
