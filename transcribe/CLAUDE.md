# transcribe — 操作說明

用法與設計在 [`README.md`](README.md)；這份只講紅線與常用操作。

## 🔴 紅線

- **只能離線轉錄。** 錄音含公司內部資訊與薪資，不得送任何雲端 API（包括 LLM 轉錄服務）。
  `transcribe.py` 預設 `HF_HUB_OFFLINE=1`，只有使用者明確要下載模型時才加 `--allow-download`。
- **錄音、逐字稿、聲紋都不進版控。** `audio/`、`out/`、`bench/`、`speaker/enroll/`、`speaker/models/`、手寫標記檔 `*.tsv`
  都已擋住；根目錄另外擋 `*.npy`／`*.npz` 與音檔副檔名。新增會輸出這類檔案的路徑，要同時補 `.gitignore`。
- **聲紋是生物特徵。** `me.npy` 不複製到別處、不上傳、不印出向量內容。
- **逐字稿不整段貼進對話。** 要引用就只取需要的那幾句。

## 常用操作

```bash
uv run transcribe.py <音檔>                          # 全離線，走 core.toml 預設（含語者標記）
uv run transcribe.py <音檔> --extra 公司名,面試官姓氏  # 這場的專有名詞，3～5 個就好
uv run transcribe.py <音檔> --no-diarize             # 不做語者標記
uv run transcribe.py <音檔> --language en            # 英文錄音（或 --language "" 自動偵測）
```

- **英文錄音一定要改語言。** `core.toml` 預設 `language = "zh"`，英文錄音會被翻成中文（而且多半是簡體），不是逐字稿。
- 中文輸出預設用 OpenCC `s2tw` 轉繁體（`台`／`了`／`只` 保留原字），`--no-s2tw` 關掉。

- 詞組不要多選：`--groups` 超過約 120 token 會污染標點，而且會讓原本對的詞變錯（`keywords.py` 開頭有實測）。
- 語者標記的判定只分「我／對方」；短於 1.5 秒的段落標 `~`，代表低信度，不能拿來當證據。
- 換了錄音通道（電話／實體／視訊）不需要重新註冊聲紋。要把新錄音併進去用 `speaker.py enroll <標記檔> <音檔> --add`；🔴 不加 `--add` 會用這次的素材整個覆蓋舊聲紋。
