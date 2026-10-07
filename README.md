# Happy-Job-Hunting

個人求職工具集（monorepo）：面試錄音轉譯、信件解碼、表單填寫、憑證外洩掃描、模擬面試、計時器。
評估本身（評分、報告、追蹤表、履歷）交給另一個開源專案 [career-ops](https://github.com/santifer/career-ops)。
**不需要先裝 career-ops**：所有工具都能獨立使用；只有 `mock-exam` 會把練習紀錄另外寫進它的 `interview-prep/sessions/`
（預設 `~/Desktop/career-ops/interview-prep/sessions/`，找不到該目錄就略過並提示；career-ops 放在別處就設 `MOCK_SESSIONS_DIR`）。

> ⚠️ **隱私**：面試錄音／逐字稿、聲紋、招募方信件、含薪資的題庫與表單欄位一律由 `.gitignore` 擋在版控外。

給 agent（Claude Code 等）的入口是 [`CLAUDE.md`](CLAUDE.md)：從零開始的步驟、共通紅線與各工具索引都在那裡。

---

## 子專案

### `transcribe/` — 錄音本地轉譯

以 mlx-whisper（Apple Silicon）做的**全程離線**語音轉文字，可用本人聲紋把每段標成「我／對方」，也附錄音工具。
錄音（面試、會議）常含公司內部資訊與薪資，不送雲端 API。

```bash
cd transcribe
uv run transcribe.py <音檔> --allow-download   # 首次下載模型（唯一需要連線的時候）
uv run transcribe.py <音檔>                    # 之後全離線
```

詳見 [`transcribe/README.md`](transcribe/README.md)。

### `secretfill/` — 表單欄位登記與填寫（macOS Keychain）

每張表都要填、內容永遠一樣、打錯很麻煩的欄位：**表單只留佔位符**，值存在 macOS Keychain，等其他欄位都填完再一次性取代。

```bash
cd secretfill
cp fields.example.toml fields.toml                         # 欄位登記表（只有欄位名與說明，永遠不放值）
uv run secretfill.py list                                  # 有哪些欄位、存了沒
uv run secretfill.py set id_number birth_date_roc          # 存值（互動輸入）
uv run secretfill.py fill ~/Downloads/form.docx            # 預演，不寫入
uv run secretfill.py fill ~/Downloads/form.docx --write     # 取代，自動備份
```

支援 `.docx` 與任何 UTF-8 純文字。操作說明見 [`secretfill/CLAUDE.md`](secretfill/CLAUDE.md)；設計理由見 [`secretfill/README.md`](secretfill/README.md)。

### `secretfind/` — 掃描外洩的 credential（macOS Keychain）

`secretfill` 的反向：credential（API key、token、密碼）存在 macOS Keychain，指定路徑去掃底下的檔案**有沒有出現這些值**。
除了原文，也比對 UTF-16LE、URL／JSON／XML 跳脫與 base64；`.docx`／`.xlsx` 等 zip 類檔案會解開來掃，PDF 逐頁抽文字比對。
報告只有位置、名稱與命中的寫法，**不印值，也不印命中的那一行**。

```bash
cd secretfind
uv run secretfind.py add gh_token                         # 存值（互動輸入）
uv run secretfind.py scan ~/some/repo -e '*.mp4'          # 掃描＋排除；結束碼 0 沒找到、1 有找到
cp secretfind.example.toml scan.toml                      # 路徑與排除規則寫在設定檔（永遠不放值）
uv run secretfind.py scan -c scan.toml
```

操作說明見 [`secretfind/CLAUDE.md`](secretfind/CLAUDE.md)；設計理由、支援的格式與限制見 [`secretfind/README.md`](secretfind/README.md)。

### `eml-extract/` — 信件（.eml）解碼

把 `.eml` 解成看得懂的東西：標頭、`text/plain` 全文、HTML 轉純文字、附件、連結。**預設遮蔽**信裡夾帶的活憑證（JWT、preview key）。

```bash
cd eml-extract
uv run eml_extract.py report.eml
```

### `mock-exam/` — 本地模擬面試

用 macOS `say` 念題目、**用麥克風錄下你的回答**、離線轉錄後做機械量測（秒數、關鍵字命中、紅線詞、填充詞）。只量測，不評內容。
需要 `say`、`afplay`（macOS 內建）與 `ffmpeg`。用法與參數寫在 [`mock.py`](mock-exam/mock.py) 開頭。
真的題庫含薪資，不進版控；repo 裡只有範例 [`banks/example.toml`](mock-exam/banks/example.toml)（檔頭說明格式）與範例紅線 `banks/_rules.toml`
（自己的紅線複製成 `banks/_rules.local.toml`，不進版控）。

```bash
cd mock-exam
uv run mock.py banks/example.toml --allow-download   # 第一次：順便下載計分模型（之後離線）
uv run mock.py banks/example.toml --text             # 題目印成文字、不放音；仍然會錄音
uv run mock.py banks/example.toml --no-score         # 只練不評，不載模型
```

### `timer/` — 終端機計時器（通知 + 音效 + 語音）

純 bash，零依賴。練講稿、番茄鐘、休息倒數用。

```bash
cd timer
./timer.sh 5                    # 5 分鐘（純數字＝分鐘）
./timer.sh 90s                  # 也吃 2m30s、3:30、1:02:30
./timer.sh 25 "番茄鐘" -b        # 背景跑，關掉終端機也還在
./timer.sh --list-sounds        # 系統音效
```

---

## 需求

- macOS / Apple Silicon（`transcribe` 的 mlx-whisper、`secretfill`／`secretfind` 的 Keychain、`timer`／`mock-exam` 的 `say` 都需要）
- [uv](https://docs.astral.sh/uv/)（`brew install uv`）
- `ffmpeg`（`brew install ffmpeg`）
- Python ≥ 3.11（uv 會自動準備）

## 授權

[GNU General Public License v2.0 **only**](LICENSE)（`SPDX-License-Identifier: GPL-2.0-only`）。

Copyright (C) 2026 vincent5753 (Vincent Peng)

---

# Happy-Job-Hunting (English)

A personal job-search toolkit (monorepo): offline transcription of interview recordings, mail decoding, form filling,
credential-leak scanning, mock interviews and a terminal timer. Evaluating postings (scoring, reports, tracker, CVs) lives in [career-ops](https://github.com/santifer/career-ops).
**career-ops is not required**: every tool works on its own; only `mock-exam` additionally writes practice logs into its
`interview-prep/sessions/` (default `~/Desktop/career-ops/interview-prep/sessions/`; skipped with a notice when that directory is missing; override with `MOCK_SESSIONS_DIR`).

Recordings, voiceprints, recruiter mail, salary-bearing question banks and form fields are all gitignored.

Agents start at [`CLAUDE.md`](CLAUDE.md) (Chinese): setup from zero, shared rules, and an index of the tools.

| Tool | What it does |
|---|---|
| `transcribe/` | Fully offline transcription on mlx-whisper, with "me / them" speaker labels from an enrolled voiceprint |
| `secretfill/` | Form placeholders substituted from the macOS Keychain; values never touch a file |
| `secretfind/` | The inverse of secretfill: scan paths for credentials stored in the Keychain (also base64/URL/JSON forms, inside docx and PDF); values are never printed |
| `eml-extract/` | Decode `.eml` files; live credentials redacted by default |
| `mock-exam/` | Local mock interviews: TTS questions, recording, mechanical scoring |
| `timer/` | Terminal timer with notification, sound and speech (pure bash) |

Requirements: macOS on Apple Silicon, [uv](https://docs.astral.sh/uv/), `ffmpeg`.

## License

[GNU General Public License v2.0 **only**](LICENSE) (`SPDX-License-Identifier: GPL-2.0-only`).

Copyright (C) 2026 vincent5753 (Vincent Peng)
