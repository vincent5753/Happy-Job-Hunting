# secretfill — 操作說明

把「每張表都要填一次、內容永遠一樣、打錯很麻煩」的欄位登記起來，表單裡只留佔位符，
最後一次性取代。值存在 macOS Keychain（service `Happy-Job-Hunting`，account 就是欄位名）。

設計理由與實作細節在 [`README.md`](README.md)；這份只講怎麼用。

## 什麼時候用它

判準是「**可以被固定填入**」，不是「敏感」：

- ✅ 身分證字號、出生日期、地址、手機 —— 敏感且固定
- ✅ 學歷的校名、科系、起訖年月 —— 不敏感但固定，登記一次就不必再翻三年前的舊表
- ❌ 期待薪資、離職原因、應徵職稱 —— 每張表都要重想，不要放進來

## 第一次使用

`fields.toml` 不進版控（欄位組合是個人化的）。repo 裡只有範本：

```bash
cp fields.example.toml fields.toml     # 再改成自己的欄位
uv run secretfill.py set <欄位名稱>     # 存值
```

沒複製就跑任何指令，工具會直接告訴你這一行。

## Keychain service 是寫死的

`secretfill.py` 開頭：

```python
SERVICE = "Happy-Job-Hunting"
```

Keychain 的唯一鍵是 `(service, account)`，`service` 就是這個 repo 的命名空間 —— 所以欄位名
可以維持裸的 `phone_number`，不必加前綴，另一個專案也能有自己的同名欄位而不衝突。

🔴 **改這個字串等於換一個命名空間：現有的值一個都讀不到**（它們還在 Keychain 裡，只是
掛在舊的 service 底下）。要改就得配套遷移，而這支工具**刻意不提供遷移功能** —— 遷移必須
先把值讀出來再用參數寫回去，那正是整個設計在避免的事。正確作法是舊項目刪掉、用新的
service 重新 `set` 一次。

ship 給別人時，這是他們唯一需要改的一行程式碼；其餘一切（欄位、格式、驗證規則）都在
`fields.toml` 裡。

## 兩輪工作流

```
第一輪  把表單非敏感欄位填完，要登記的欄位留 __id_number__ 這種佔位符
第二輪  fill <檔案>          → 只檢查：列出可填／待補／未登記
        set <欄位>...        → 缺的補進 Keychain（互動輸入）
        fill <檔案> --write  → 取代，自動留 .bak.<時間戳>
```

```bash
cd secretfill                      # 在 repo 根目錄底下
uv run secretfill.py fill ~/Downloads/form.docx            # 預演，不寫入
uv run secretfill.py set edu_bs_school edu_bs_from
uv run secretfill.py fill ~/Downloads/form.docx --write     # 寫回原檔
uv run secretfill.py fill ~/Downloads/form.docx -o out/form.docx # 原檔不動
uv run secretfill.py fill ~/Downloads/form.docx --write --no-backup  # 不留 .bak（確定不需要回復時才用）
```

支援 `.docx` 與任何 UTF-8 純文字（`.md`／`.txt`／`.xml`／`.csv`…）。

| 指令 | 作用 |
|---|---|
| `list` | 欄位與「存了沒」，**不顯示值** |
| `list --values` | 連值一起印。唯一一次顯示全部值的指令，印之前會提醒確認沒人在看螢幕 |
| `set <欄位>...` | 存入／更新，由 `security` 自己接 prompt |
| `get <欄位>` / `copy <欄位>` | 印出單一值／複製到剪貼簿（不印） |
| `rm <欄位>` | 從 Keychain 刪除 |

## 登記一個新欄位

先在 `fields.toml` 加一行，再 `set`（格式的寫法與完整註解見 `fields.example.toml`）。**沒登記的佔位符不會被動**，只會在報告裡標 `? 未登記`。

```toml
[fields]
edu_hs_school = "高中職校名"                       # 只有說明

phone_number = { label = "手機號碼",                # 加上格式
                 format = "09xxxxxxxx（不加分隔符號）",
                 pattern = "09\\d{8}" }
```

`format` 是給人看的提示（`set` 在輸入前先印、`list` 與 `fill` 也帶著）；`pattern` 是選填的
正規表示式，**只警告不阻擋**。

⚠ **格式一律宣告在 `fields.toml`，不要寫進 `secretfill.py`。** 什麼算合法的電話或身分證是
發表單那個國家的性質，不是這支工具的性質。

## 出生日期只存一筆

Keychain 裡只有 `birth_date_roc`（民國年），其餘三種寫法由工具換算，不另外存：

| 佔位符 | 結果 |
|---|---|
| `__birth_date_roc__` | 84/10/10 |
| `__birth_date_ce__` | 1995/10/10 |
| `__birth_date_roc_cjk__` | 民國84年10月10日 |
| `__birth_date_ce_cjk__` | 1995年10月10日 |

裸的 `__birth_date__` 會**看文件裡其他日期欄位**決定曆法並印出理由，判不出來才用預設（西元）。
有證據時直接寫明確的那個名稱，不要靠偵測。

## 紅線

- **值不印進對話、不進 argv、不落地成檔案。** `fill` 的報告只有欄位名與次數。
  要看值是使用者自己跑 `get` 或 `list --values`，不是我代跑後貼出來。
- **填好的表單含個資。** 不進版控、不貼進對話。`.gitignore` 已擋 `out/`（只留 `.gitkeep`）、常見文件格式（`*.doc`／`*.docx`／`*.odt`／`*.pdf`／`*.rtf`／`*.pages`／`*.xlsx` 等）、`*.bak.*`；輸出建議寫到 `out/`。
- **不代為送出。** 填好、產好、給使用者看，送出鍵他自己按。

## 踩過的坑

- **Word 會把一個佔位符拆進好幾個 `<w:t>` run**（拼字檢查、修訂記錄），所以比對是以段落為單位
  串接文字節點後進行，不是對 XML 做字串取代。自己寫腳本改 docx 時同一個坑要自己處理。
- **`<w:t[^>]*>` 也會命中 `<w:tcPr>`。** 要寫成 `<w:t(?:\s[^>]*)?>`。
- **核取方框有兩種合法寫法。** Word 存成 `<w:t>q</w:t>`（Wingdings 的 `q`＝☐、`ü`＝✔），
  LibreOffice 存回去會變成 `<w:sym w:font="Wingdings" w:char="0071"/>`（勾是 `00fc`）。
  只數 `<w:t>` 會誤判成「方框被吃掉了」。
- **使用者常在我改檔的同時自己開著 LibreOffice 編輯。** 動手前先看 `docProps/core.xml` 的
  `dcterms:modified`，寫回前再 `cmp` 一次來源檔，不然會互相覆蓋。
