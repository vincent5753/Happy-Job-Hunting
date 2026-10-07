# secretfind — 操作說明

credential 存在 macOS Keychain（service `secretfind`，account 就是名稱），指定路徑去掃底下的檔案
有沒有出現這些值。`secretfill` 的反向。

設計理由、比對了哪些寫法、支援的格式與限制在 [`README.md`](README.md)；這份只講怎麼用。

## 什麼時候用它

- commit／push 前確認 repo 裡沒有混進 token（`.env` 被 `git add -A` 帶進去、設定檔寫死金鑰）
- 分享資料夾、打包附件、把子專案拿出去發布之前
- 懷疑某個 credential 外洩，要找出它散在哪些檔案

不適合拿來找「長得像 credential 的東西」—— 它只認 Keychain 裡**已經存了**的值。沒存的 token 掃不到。

## 🔴 紅線

- **值不印進對話、不進 argv、不落地成檔案。** 存值一律請使用者自己跑 `add`（`security` 會開
  prompt）。我不代跑 `security find-generic-password -w`，也不用任何方式讀出值來「確認一下」。
- **只有測試用的假值**可以放在 `security add-generic-password ... -w <值>` 後面，而且必須寫進
  拋棄式 keychain（見下方「測試」），不碰 login keychain。
- **報告貼進對話沒問題**：只有路徑、行號／頁碼、名稱、寫法，沒有值也沒有上下文。
  但**不要為了「看看是什麼」去 `cat`／`sed` 命中的那一行** —— 那一行就是外洩的值本身。
  要確認請使用者自己開檔。
- **找到之後不自己動檔案。** 刪掉或改寫外洩內容、輪替 credential，都先問使用者。
  外洩的 credential 光從檔案拿掉不夠，已經進過 git 歷史或傳出去的，要換新的。

## 常用指令

```bash
cd secretfind                                             # 在 repo 根目錄底下
uv run secretfind.py list                                 # 存了哪些（名稱與長度，不顯示值）
uv run secretfind.py add gh_token openai_key              # 使用者自己跑：互動輸入
uv run secretfind.py rm gh_token

uv run secretfind.py scan <路徑>...                        # 掃描
uv run secretfind.py scan <路徑> -e '*.mp4' -e data/raw    # 排除，可重複
uv run secretfind.py scan -c scan.toml                    # 路徑與排除寫在設定檔
uv run secretfind.py scan -c scan.toml <路徑>             # 同一份排除規則改掃別處（取代 config 的 paths）
uv run secretfind.py scan <路徑> --service Happy-Job-Hunting  # 改掃 secretfill 的個資
```

排除規則語法（fnmatch，**不是**完整 gitignore）：沒有 `/` 比對任何深度的名稱、有 `/` 比對相對掃描根目錄的路徑、
`/` 或 `~` 開頭是絕對路徑。`.git`、`.venv`、`node_modules` 等預設就排除，要掃就加 `--no-default-excludes`。

設定檔從 `secretfind.example.toml` 複製成 `scan.toml`（已 gitignore）。指令列有給路徑就**取代**設定檔的
`paths`（`--max-size` 同理）；`-e`、`--service` 則和設定檔的**合併**。所以 `scan -c scan.toml <別的路徑>`
是「用同一份排除規則掃別的地方」。

## 讀報告

```
cfg.json:1                              api_key   JSON
form.docx!word/document.xml:(文字內容)   api_key   原文
offer.pdf:(第 2 頁)                      gh_token  原文
```

- stdout 是命中清單（tab 分隔），stderr 是進度、摘要、警告 —— 要過濾命中結果只管 stdout。
- `外層!內部路徑` 是 zip 類檔案（docx／xlsx／jar）裡的成員。
- 第三欄是命中的寫法（`原文`、`UTF-16LE`、`URL`、`JSON`、`XML`、`base64+N`…），說明它是以什麼形式外洩的。
- **摘要裡的 ⚠ 要轉告使用者**：有密碼的 PDF、抽不出文字的 PDF、解析失敗的檔案，都是「沒掃到」，
  不是「乾淨」。太短（< 8 bytes）的 credential 也會警告，它的命中可能是誤判。

結束碼：0 沒找到、1 有找到、2 錯誤。在 `set -e` 的腳本裡，**1 會讓腳本中止** —— 那是結果，不是程式壞掉；
要繼續跑就接 `|| rc=$?`。

## 掃不到的東西

回報「沒找到」時要把這些一起講，不要說成「確定沒有外洩」：

- git 歷史（物件是壓縮過的；預設也排除了 `.git`）
- 掃描成圖片的 PDF、圖片裡的文字（需要 OCR）
- `.doc`／`.xls` 舊版 Office、gzip／tar
- 被切成多段、或做過其他轉換（加密、hash、自訂編碼）的值
- 超過 `--max-size`（預設 50 MB）的檔案、symlink

## 測試

**改完程式先跑 `bash test.sh`**（全部通過才算完成；`-v` 看完整輸出）。它自己建拋棄式 keychain、
自己清掉。新功能要在 `test.sh` 補一項 `check`。`add` 的互動輸入要手動測，步驟在檔尾。

不要拿真的 credential 測。要另外做實驗時，一樣用拋棄式 keychain，放在 scratchpad：

```bash
bash <<'EOF'
set -euo pipefail
K=<scratchpad>/t.keychain-db
security create-keychain -p x "$K"
security unlock-keychain -p x "$K"
security add-generic-password -s secretfind -a demo -w 'dummy-value-123' "$K"
SECRETFIND_KEYCHAIN="$K" uv run secretfind.py scan <測試目錄> || echo "rc=$?"
security delete-keychain "$K"
EOF
```

- `SECRETFIND_KEYCHAIN` 讓所有 Keychain 操作改走那個檔，login keychain 完全不會被讀寫。
- `create-keychain` 會把它加進 keychain 搜尋清單，**測完一定要 `delete-keychain`**，再用
  `security list-keychains` 確認只剩 login 與 System。
- 真的 docx／pdf 可以用 `soffice --headless --convert-to docx|pdf` 或 `textutil -convert docx` 從 HTML 產生；
  `pdftotext` 可以先確認文字真的在 PDF 裡，再判斷是不是 secretfind 漏掉。

## 踩過的坑

- **`security -w` 對含換行、引號、反斜線的值會印 hex dump。** 已經用 `-g` 的 `0x` 前綴解回 bytes，
  自己寫腳本讀 Keychain 時同一個坑要自己處理（和 secretfill 一樣）。
- **`dump-keychain` 的 account 有兩種印法**：ASCII 是 `"acct"<blob>="name"`，非 ASCII 是裸的
  `"acct"<blob>=0xE4B8AD...`。只解析帶引號的那種會漏掉中文名稱。
- **PDF 會把長 token 斷成兩行**，抽出的文字中間也可能多插空白，所以 PDF 是兩邊都去掉空白再比對。
- **docx 同一筆 credential 可能被兩種方式命中**（逐 byte 的 `XML` 寫法、去標籤後的原文）。
  去標籤那一輪只補還沒命中的 credential，不會重複回報。
- **互動 shell 是 zsh**，`echo ===` 會報錯；多行測試一律包進 `bash <<'EOF'`。
