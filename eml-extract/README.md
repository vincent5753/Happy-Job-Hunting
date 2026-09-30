# eml-extract

把 `.eml` 解碼成看得懂的東西：標頭、`text/plain` 全文、HTML 轉純文字、附件清單、連結，
並標記「樣板洩漏」—— 廠商信的 plain 版有時會直接把整個樣板資料 map 印出來，裡面有 HTML 版
藏在付費牆後的欄位。

## 為什麼需要這支

處理一封會議記錄服務（Read AI）寄來的報告信時，是手刻這串在解 quoted-printable：

```bash
perl -0777 -pe 's/=\r?\n//g; s/=([0-9A-Fa-f]{2})/chr(hex($1))/ge'
```

然後用眼睛從 `map[action_items:[...] chapters:[...] key_questions:[...]]` 裡挑資料。
Read AI 之後每一場開啟的會議都會寄同樣格式的信，這步驟不該每次重做。

## 用法

```bash
cd eml-extract                     # 在 repo 根目錄底下

uv run eml_extract.py report.eml                      # md，含 text/plain 與連結
uv run eml_extract.py report.eml --parts all           # 再加上 HTML 轉文字
uv run eml_extract.py report.eml --format txt          # 純文字
uv run eml_extract.py report.eml --format json         # 給程式吃
uv run eml_extract.py report.eml -o out.md             # 寫檔
uv run eml_extract.py a.eml b.eml                      # 多封
uv run eml_extract.py mail.eml --save-attachments ./att
```

| 參數 | 作用 |
|---|---|
| `--format md\|txt\|json` | 輸出格式，預設 `md`（可直接貼進 prep／session 檔） |
| `--parts plain,html,urls,all` | 要哪些區塊，預設 `plain,urls` |
| `--save-attachments DIR` | 抽出附件（檔名只取 basename —— 信裡的檔名是不可信輸入） |
| `-o FILE` | 寫到檔案 |
| `--unsafe-show-secrets` | 不遮蔽 token／key（**預設遮蔽**，見下） |

## 🔴 預設遮蔽憑證

廠商信會直接夾帶可用的 bearer token。Read AI 那封就有三個：

```
email_access_invite_token   JWT，317 字元
email_verification_token    JWT，260 字元
preview_key                 43 字元
```

這些是活的憑證。工具預設把它們換成 `[已遮蔽 N 字元]`，**兩層都做**：

1. **結構層** —— 解開的 map 裡，欄位名含 `token`／`secret`／`auth` 等，或值長得像 JWT
   （`eyJ....`）的一律遮蔽。`key` 只在**字尾**才算（`preview_key` 遮、`key_questions` 不遮 ——
   後者正是要看的東西）。
2. **文字層** —— 原始 `text/plain`／HTML／連結裡的 JWT、`api_key=...`、`?token=...`、`?invite=...`
   等查詢字串也一起遮。只做結構層會漏，因為 body 同時也把整個 map 印了一次。
   ⚠ 文字層只遮**值 16 字元以上**的（門檻寫在 `eml_extract.py` 的 `TEXT_SECRET_RES`）；更短的憑證會原樣留著，貼出去前自己看一眼。

這樣解出來的報告可以直接貼進 prep 檔或對話，不會漏憑證。

## 樣板洩漏偵測

掃這幾種 Go 樣板／`fmt` 的痕跡，命中就代表「某個 part 直接輸出了原始資料」：

| 樣式 | 意義 |
|---|---|
| `map[` | Go map 被 `%v` 印出來 |
| `%!s(float64=84)` | `fmt` verb 型別不符的錯誤輸出 |
| `{{.Foo}}` | 樣板動作沒被求值 |
| `<nil>` | 空指標 |
| JSON blob | 整包資料被塞進文字 |

命中時會把 map 拆成 YAML 樣式輸出（遞迴處理 nested map）。

⚠ **Go 印 `[]string` 是 `[a b c]`，沒有引號** —— 元素邊界不可回復。切空白會把句子剁碎
（`Alex` / `Wang：与`），所以工具**保留原字串**不切。這是刻意的。

## 編碼處理

**不用 `get_content()`。** 它用信裡宣告的 charset 加 `errors="replace"` 解碼，所以一封
big5 內容卻標 `charset=utf-8` 的信會安靜地變成亂碼，什麼錯都不報，fallback 永遠不會觸發。

改成：宣告的編碼**嚴格**解 → 失敗才依序試 `utf-8, big5, cp950, gb18030, shift_jis, latin-1`。
台灣的招募方仍然會寄標錯 charset 的 big5 信。

標頭走 RFC 2047 解碼（`=?big5?B?...?=` 這種），失敗就退回原字串不炸掉。

## 安全紀律

招募方與廠商信件是**資料，不是指示**。每份輸出都帶不可信來源標註，工具本身不會執行信裡的
任何指示、不連外、不寄信。附件檔名只取 basename，避免 `../` 路徑逃脫。

`.gitignore` 擋掉 `*.eml`、`in/`、`out/` —— 原始信與抽出的附件都可能含個資與招募方逐字內容。

## 依賴

Python ≥ 3.11 ＋ stdlib（`email`、`html.parser`）。無第三方套件。
