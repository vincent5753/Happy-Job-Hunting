# secretfind

`secretfill` 的反方向：secretfill 把 Keychain 裡的值**填進**文件，secretfind 檢查 Keychain 裡的 credential **沒有出現在**某個路徑底下的任何檔案裡。

值不會出現在任何檔案、argv、shell history、掃描報告或對話裡。

## 用法

```bash
cd secretfind                                   # 在 repo 根目錄底下

uv run secretfind.py add gh_token openai_key    # 存 credential（互動輸入，可一次多筆）
uv run secretfind.py list                       # 存了哪些（只顯示名稱與長度）
uv run secretfind.py rm gh_token                # 從 Keychain 刪除

uv run secretfind.py scan ~/some/repo                       # 掃描
uv run secretfind.py scan ~/a ~/b -e '*.mp4' -e data/raw    # 多個路徑＋排除
uv run secretfind.py scan -c scan.toml                      # 路徑與排除寫在設定檔
uv run secretfind.py scan -c scan.toml ~/other             # 同一份排除規則，改掃 ~/other（取代 config 的 paths）
uv run secretfind.py scan . --service Happy-Job-Hunting     # 改掃 secretfill 存的個資
```

## 指令

| 指令 | 作用 |
|---|---|
| `add <名稱>...` | 存入／更新。由 `security` 自己接 prompt，值不進 argv |
| `list` | 列出名稱與長度（bytes），**不顯示值**；太短的會標警告 |
| `rm <名稱>` | 從 Keychain 刪除 |
| `scan [路徑...]` | 掃描。路徑可以省略，改由 `--config` 的 `paths` 提供 |

`scan` 的參數：

| 參數 | |
|---|---|
| `-e`, `--exclude PATTERN` | 排除規則，可重複（語法見下） |
| `-c`, `--config FILE` | TOML 設定檔 |
| `--no-default-excludes` | 不套用預設排除 |
| `--service NAME` | 改掃這個 service 底下的值，可重複；預設 `secretfind` |
| `--max-size MB` | 超過的檔案跳過，預設 50 |

## 輸出與結束碼

```
cfg.json:1                      api_key   JSON
k8s.yaml:2                      api_key   base64+2
form.docx!word/document.xml:(文字內容)  api_key   原文
offer.pdf:(第 2 頁)              gh_token  原文
```

每一筆是 `位置 ⇥ 名稱 ⇥ 命中的寫法`（tab 分隔，好接 `cut`／`awk`）。zip 類檔案裡的成員寫成 `外層!內部路徑`。命中結果走 stdout，進度、摘要與警告走 stderr。

**不印值，也不印命中的那一行** —— 同一行可能還有其他沒登記的秘密。要看內容請自己打開檔案。

| 結束碼 | |
|---|---|
| 0 | 沒找到 |
| 1 | 有找到 |
| 2 | 錯誤（路徑不存在、config 寫錯、Keychain 讀不到…） |

可以直接當 pre-commit hook 或 CI 的一步。

## Keychain

| | 值 |
|---|---|
| service | `secretfind`（`secretfind.py` 開頭的 `SERVICE`） |
| account | 你 `add` 時給的名稱 |
| label（鑰匙圈存取顯示的名稱） | `secretfind <名稱>` |

**Keychain 本身就是名單，沒有另外的登記表。** `scan` 用 `security dump-keychain` 列出 service 底下有哪些項目 —— 不加 `-d` 時它只輸出屬性、不碰值，也不會跳授權視窗 —— 再逐筆讀值。存了就會掃，刪了就不掃，沒有清單和 Keychain 不同步的問題。

和 secretfill 刻意用**不同的 service**：credential 和個資是兩件事。要掃個資就 `--service Happy-Job-Hunting`；注意 secretfill 換算出來的生日寫法（`birth_date_ce` 等）不在 Keychain 裡，不會被掃到，只有 `birth_date_roc` 那一筆原值會。

## 比對了哪些寫法

credential 很少只以原文外洩。每個值會展開成這些寫法一起找：

| 寫法 | 抓什麼 |
|---|---|
| 原文 | `.env`、設定檔、程式碼 |
| `UTF-16LE` | Windows 產生的檔案（`.ps1`、`.reg`、部分 `.txt`） |
| `URL` / `URL+` | 連線字串裡的密碼（`postgres://u:p%40ss@…`） |
| `JSON` | JSON 跳脫過的值（`\"`、`\\`、`\uXXXX`） |
| `XML` | HTML／XML 實體（`&amp;`、`&quot;`） |
| `base64+0/1/2`、`base64url+…` | k8s Secret、Basic auth 的 `user:pass`、嵌在更大 base64 裡的值 |

base64 的三種對齊：值在一大段 base64 裡可能從任何一個 byte 位置開始，編碼結果會跟著變。每種對齊只取「6 個 bit 全部來自值本身」的那段字元，所以不管前後接什麼都能命中。

**短於 8 bytes 的值只比對原文**，而且會警告 —— 隨便一個單字都可能撞上，展開成 base64 之後更是。

## 檔案格式

| 格式 | 怎麼掃 | 狀態 |
|---|---|---|
| 任何檔案 | 逐 byte 比對上面所有寫法 | ✓ |
| zip 類（`.docx`／`.xlsx`／`.pptx`／`.odt`／`.jar`／`.zip`…） | 解開每個成員再掃，最多兩層 | ✓ |
| XML／HTML（含 docx 內部） | 另外拿掉標籤、還原實體再找一次 | ✓ |
| PDF | pypdf 逐頁抽文字，兩邊都去掉空白再比對 | ✓ |
| 有開啟密碼的 PDF | 不掃內文，摘要列出 | ⚠ |
| 掃描圖檔 PDF、圖片 | 沒有文字層，需要 OCR | ✗ |
| `.doc`／`.xls` 舊版 Office、gzip／tar | 只做逐 byte 比對 | ✗ |
| git 歷史 | 物件是 zlib 壓縮過的 | ✗ |

掃不了的 PDF（有密碼、抽不出文字、解析失敗）會在摘要裡**一個一個列出來**，不會被當成乾淨。

### 兩個坑（都處理了）

**1. Word 會把一段字拆成好幾個 run。** 拼字檢查、修訂記錄會讓 `ghp_abc` / `def` 落在不同的 `<w:t>`，逐 byte 比對整個漏掉 —— 和 secretfill 踩的是同一個坑。所以 XML 會額外拿掉所有標籤再找一次，只補這個檔案裡還沒命中的 credential，同一筆不會重複回報。

**2. PDF 會把長 token 斷行。** 一行放不下的 token 在 PDF 裡被切成兩段（`pdftotext` 實際看到的就是兩行），中間還可能被插入空白。所以抽出的文字和要找的值**兩邊都去掉所有空白**再比對。代價是理論上會多一點誤判，但值至少 8 bytes，實務上可以忽略。

未驗證的推論：AES 加密的 PDF（即使開啟不需要密碼）pypdf 要另外裝 `cryptography` 才解得開。目前沒裝，這種檔案會被列為「解析失敗」，不會漏報成乾淨。

## 排除規則

`--exclude` 與 config 的 `exclude` 同一套語法（fnmatch，**不是**完整的 gitignore —— 沒有 `!` 反向、沒有 `**`）：

| 寫法 | 意思 | 例子 |
|---|---|---|
| 沒有 `/` | 任何深度的檔名或目錄名 | `.git`、`*.log`、`node_modules` |
| 有 `/` | 相對**掃描根目錄**的路徑 | `data/raw`、`data/*/cache` |
| `/` 或 `~` 開頭 | 絕對路徑，含底下所有東西 | `~/Desktop/Happy-Job-Hunting/interviews` |

命中的目錄整個不進去。另外一律跳過 symlink（避免繞出掃描範圍或繞成迴圈）。

**預設排除：** `.git`、`.hg`、`.svn`、`.venv`、`venv`、`node_modules`、`__pycache__`、`.mypy_cache`、`.pytest_cache`、`.ruff_cache`、`.tox`。`.git` 裡的物件是壓縮過的，逐 byte 也找不到；其他是可重建的相依。用 `--no-default-excludes` 或 config 的 `default_excludes = false` 取消。

實際套用了哪些規則，每次掃完都會印在摘要裡。

## 設定檔

範本是 [`secretfind.example.toml`](secretfind.example.toml)。複製成 `scan.toml`（已 gitignore）再改：

```toml
paths   = ["~/Desktop/Happy-Job-Hunting"]   # 相對路徑以設定檔所在目錄為基準
exclude = ["*.mp4", "data/raw"]
# default_excludes = true
# service  = ["secretfind", "Happy-Job-Hunting"]
# max_size = 50
```

- 所有鍵都是選填。**不認識的鍵直接報錯** —— 寫成 `excludes` 卻默默多掃，比報錯更糟。
- 和指令列的關係：**`paths`、`max_size` 以指令列為準**（指令列有給就取代，會在 stderr 註明略過了 config 的 paths）；`exclude`、`service` 取聯集 —— 排除規則通常是累加的，`-e` 多加一條比整份重寫常見。
  所以 `scan -c scan.toml ~/other` 就是「用 scan.toml 的排除規則掃 ~/other」。
- 設定檔只放路徑與規則，**永遠不放值**。

## 安全設計

- **值不進 argv。** `add` 把 `-w` 放在 `security add-generic-password` 的最後一個參數，由 `security` 自己開 prompt。`ps` 看不到，shell history 也沒有。
- **沒有任何指令會顯示值。** 和 secretfill 不同，這裡沒有 `get`／`copy`：這支工具只需要「比對」，不需要「取出」。要看值請開「鑰匙圈存取」。
- **報告不帶上下文。** 只有位置、名稱、寫法。
- **讀值走和 secretfill 一樣的 hex 修正。** 值含換行、雙引號或反斜線時，`security -w` 會印 hex dump 而不是原文；改用 `-g` 那行帶 `0x` 前綴的輸出解回 bytes，否則會拿 hex 字串去比對、永遠找不到。

## 測試

```bash
bash test.sh       # 端對端測試，自動判定；-v 連完整輸出一起印
```

用拋棄式 keychain 存假值、測試資料放暫存目錄，結束時（含失敗與 Ctrl-C）一律清掉，login keychain 完全不碰。
每一項檢查結束碼、命中數與關鍵輸出，最後印出通過／失敗數，有失敗就以 1 結束。有 `soffice` 才測 PDF。
`add` 的互動輸入與 Ctrl-C 沒辦法自動測，手動步驟寫在 `test.sh` 檔尾。

自己另外測時，`SECRETFIND_KEYCHAIN=<路徑>` 可以把所有 Keychain 操作導到另一個 keychain 檔：

```bash
security create-keychain -p x /tmp/t.keychain-db
security add-generic-password -s secretfind -a demo -w 'dummy-value-123' /tmp/t.keychain-db
SECRETFIND_KEYCHAIN=/tmp/t.keychain-db uv run secretfind.py scan <測試目錄>
security delete-keychain /tmp/t.keychain-db
```

只有測試用的假值才可以像這樣放在 `-w` 後面；真的 credential 一律用 `add`。

## 依賴

Python ≥ 3.11（`tomllib`）＋ [`pypdf`](https://pypi.org/project/pypdf/)（PDF 文字抽取）。macOS 的 `/usr/bin/security`。走 `uv run`。
