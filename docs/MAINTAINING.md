# 維護者須知

這份寫給要改這個 repo 的程式或文件的人。下面列的規則，每一條背後都有一次實際出過的錯，所以動手前請先讀完。

專案本身在做什麼、目前的結果，看 [README](../README.md) 與 [OVERVIEW.md](OVERVIEW.md)。

## 環境

- Windows，Python 直接執行，不用容器。golden 快照目前是在 Python 3.12.7、pandas 2.3.1、pyarrow 21.0.0、numpy 2.3.1、matplotlib 3.10.3、pandera 0.32.1 下擷取的。
- 套件見 `requirements.txt`。Windows 沒有系統時區資料庫，所以一定要裝 `tzdata`；pandera 至少要 0.24，舊版沒有 `pandera.pandas` 這個 import 路徑。
- 原始資料放在 `data/00_raw_lite/`（正式資料）與 `data/00_raw/`（試驗資料），整個 `data/` 都在 `.gitignore` 裡。

## 改完之後一定要跑

```
python tools/golden.py verify
pytest
```

`golden.py verify` 會重跑兩批資料的完整輸出，和 `tools/golden/manifest.json` 裡的快照比對，包括發佈的 csv、表格區塊、圖、中間表雜湊、分類母體指紋與成本總額。

exit code：

- `0`：一致。
- `1`：可以比較但不一致。管線中途拋例外、分類母體指紋和凍結值對不上，都算這一類，代表被測的程式壞了。
- `2`：前置條件不足，例如 `docs/` 或 `README.md` 有未提交的改動、manifest 不存在、套件版本和 manifest 不同。

改動本來就預期會讓輸出變化時，先 commit，確認 verify 的差異只出現在預期的類別，再擷取新快照：

```
python tools/golden.py capture --accept A,B --reason "為什麼會變"
```

`--accept` 只列真的有變的類別（多列了沒變的類別不會被拒，只會印出提示），而且一定要附 `--reason`。每次 capture 都會在 manifest 的 `history` 追加一筆紀錄，舊紀錄不會被改寫。套件升級要先在舊環境確認快照仍然通過，再用 `--accept-environment` 擷取。

有兩件事要特別注意：

- 文字輸出只要含 CR 位元組就判失敗。寫檔一律用 `lineterminator="\n"` 或 `newline="\n"`，`tests/test_line_endings.py` 會靜態檢查。
- 遮蔽規則如果被改壞，程式不會報錯，只會讓不該公布的數字出現在 csv 裡，或擋掉該公布的數字。這種錯誤只能靠逐位元組比對抓到，所以 verify 不能省。

## 不要做的事

**不要讓 request 的原文落地。** 抽取時只保留 `prompt_text_len` 與 `prompt_text_sha256`，原文不寫進 Parquet、不寫進任何衍生表，也不進 git。內容裡有臨床照護紀錄。`classify_lite/markers.py` 的 `check_columns()` 有硬性斷言擋著，不要繞過。

**會把內容印到螢幕的工具，只在自己的終端機跑。** repo 外的 `review_clusters.py` 與 `rescreen_prefixes.py` 會把完整內容印出來。不要透過任何會保存終端輸出的工具執行它們，包括自動化代理、遠端 shell 與終端錄製，否則原文會跟著輸出被存下來。

**不要從模型的判定結果反推分類法的定義。** 字典一旦照模型的行為修改，之後的人工標註就不再獨立，而算出來的一致率在數字上完全看不出差別。規則見 [annotation_protocol.md](../ref/annotation_protocol.md) 第二節。

**不要把正式資料的檔案放進 `data/00_raw/`。** `extract.py` 會用試驗資料的欄位路徑去解析，產出十幾萬列幾乎全是空值的表，而且不會報錯。正式資料放 `data/00_raw_lite/`。

**不要跑 `git clean -xfd`。** `-x` 會連同 gitignore 的原始資料一起刪掉。要清理請用 `git clean -fd`，或先加 `-n` 看會刪到什麼。

**在 `ref/` 建新檔前，先檢查 `.gitignore`。** `ref/` 採白名單制，新檔預設被擋，而被擋的檔案不會出現在 `git status`。檢查指令：`git check-ignore -v ref/<檔名>`。

**不要放寬 `render_index.run()` 裡 `line == "clean"` 的斷言。** `render_index` 有幾處直接讀全域的 `REGISTRY`，這個斷言是防止正式資料的指標混進試驗資料的索引。

**重算成本一律透過 `extract_lite.load_dataset()` 讀資料。** 直接 glob 讀 Parquet 會少掉 Hive 分區欄 `date_taipei`，價目的生效日會選錯，總額從 $2,749.32 變成 $2,754.80，而且不會報錯。

## 遮蔽規則

門檻在 `src/config.py`：分組少於 `MIN_GROUP_SIZE`（10）人，或單一使用者佔該組超過 `DOMINANT_THRESHOLD`（30%），比例欄就不公布，計數照樣保留。

每個分組維度都要明確登記在下面的清單裡。試驗資料的清單在 `src/aggregate.py`，正式資料的在 `src/aggregate_lite.py`，名稱多一個 `_LITE` 後綴。

| 清單 | 放什麼 |
| --- | --- |
| `CONCENTRATION_DIMENSIONS` | 會把人分群的維度，例如學位、系所、單位。套用遮蔽 |
| `EXEMPT_DIMENSIONS` | 分的是 request 的維度，例如日期、模型、endpoint。不遮蔽，但輸出必須帶 `n_users` |
| `NON_PERSON_GROUPS` | 不對應個人的分組值，例如 service account。只套用人數門檻，不套用單人佔比門檻 |

新增的維度如果前兩份清單都沒登記，只會發出警告，比例照常輸出：集中度表沒有算這個維度，沒有規則可以套用。所以跑完指標一定要看警告，並把新維度登記進其中一份清單。

## 凍結的座標

下面四個值一改，既有的結果就會作廢，而且多半是悄悄作廢，因為比例照樣算得出來。要改的話，要跟重跑一起做，並另開新的 FREEZE。完整定義在 [annotation_protocol.md](../ref/annotation_protocol.md) 第一節。

| 座標 | 值 | 改了會怎樣 |
| --- | --- | --- |
| 分類母體指紋 | `63af7d40…` | 以 17,365 或 58,100 為分母的比例全部失效 |
| 長度切點 | `[251, 986, 4799]` | 分層的成員換人，抽樣與基準線都會變 |
| 聚類參數 | 門檻 10、前綴上限 4,000 字元 | 群集換掉，`group_id` 不再指同一批內容 |
| 提示詞指紋 | `e13f9738…` | 現行的群集判定結果全部要重跑 |

取得新的人工驗證資料之前，分類法、提示詞與門檻都凍結在 [FREEZE_2026-09.md](../ref/FREEZE_2026-09.md)。

## 指令

正式資料：

```
python -m src.extract_lite                        # 原始 JSON → 依日期分區的 Parquet
python -m src.schema_lite                         # 欄位契約驗證
python -m src.aggregate_lite                      # 使用者層級表與集中度
python -m src.run metrics --line lite --publish   # 指標，並發佈到 docs/
```

試驗資料：

```
python -m src.run all                             # extract → validate → aggregate → metrics
python -m src.run metrics --line clean --publish  # 發佈到 docs/
```

用途分類的前置步驟不在主管線裡，要單獨執行：

```
python -m src.classify_lite.markers               # 掃描結構標記，約 3.5 分鐘
python -m src.classify_lite.prefilter             # 套用四條規則
python -m src.classify_lite.sample_domain         # 以固定種子重建 600 筆樣本
python -m src.classify_lite.judge_domain --dry-run
```

加入新一批正式資料時要檢查的事情（雜湊 salt 有沒有換、有沒有新的系所代碼或模型），寫在 [RUNBOOK_lite.md](RUNBOOK_lite.md)。

## 程式與文件的分工

- `src/` 裡兩批資料的程式並排放，檔名帶 `_lite` 的屬於正式資料。目前刻意不拆成子目錄：搬檔會讓 `git blame` 斷掉，import 與測試也要全部重驗。等共用模組變多、或有人拿錯檔案時再考慮。
- `src/classify_lite/` 的判定器程式參與提示詞指紋。為了整理而改動它，可能讓現行結果作廢，改之前先確認指紋不變。
- 文件裡 `<!-- AUTOGEN:... -->` 標記之間的內容由 `render_*.py` 產生，不要手改。要改內容，請改指標的 `@metric` 參數或 renderer。標記以外的文字是手寫的，重跑時不會被覆寫。
- 文件中的數字一律以 `docs/data_lite/`、`docs/data/` 的 csv 為準。從散文、筆記或另一份文件抄數字，都出過錯。

## 反覆出過的錯

這幾類錯都發生過不只一次，而且都不會報錯：

- **用 heredoc 把含反斜線的程式碼傳給 Python。** shell 會吃掉反斜線，正規式 `r"\n\s*"` 變成 `r"n s*"`，程式照跑，測試照綠。請先寫成 `.py` 檔再執行。
- **分層抽樣的保底名額寫成先給再扣。** 小層的名額最後被扣回 0，而保底本來就是為了小層才存在。
- **把差距小於量測解析度的變化讀成效果。** 例如 23 群的測試臺上差一群就是 4.3 個百分點，這個幅度的變動不能拿來下結論。

更多案例在 [ENGINEERING_NOTES.md](ENGINEERING_NOTES.md)。
