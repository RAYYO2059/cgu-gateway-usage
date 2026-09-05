# lite 資料流：擴充與維護

這份文件寫給「下次拿到一批新的 lite 匯出」的人——很可能是三個月後的你自己。
它記的不是指令清單，而是**哪些地方會出錯、為什麼**。指令本身在 `--help` 裡都查得到。

lite 與 clean 是兩條並行的資料流，共用 `src/config.py` 的路徑常數，其餘完全分開：

| | clean | lite |
|---|---|---|
| 原始檔 | `data/00_raw/`（不進版控） | `data/00_raw_lite/`（不進版控，可用 `$CGU_LITE_RAW` 覆寫） |
| 抽取 | `src/extract.py` | `src/extract_lite.py` |
| 輸出 | `data/01_request/` | `data/01_request_lite/` |
| 契約 | `src/schema.py` | `src/schema_lite.py` |
| 人層級鍵 | `username` | `anonymous_user_id` |

兩邊的 `request_id` 都是頂層同名欄位，所以**混在一起不會立刻報錯**，只會安靜地錯。
底下第一條坑講的就是這件事。

---

## 情境一：新增日期範圍

最常見的情況——上游又匯出了一批新日期。

### 解壓

把新的日期資料夾解壓進 `data/00_raw_lite/`（或 `$CGU_LITE_RAW` 指到的位置），維持既有的
`<根目錄>/YYYY-MM-DD/YYYY-MM-DD/*.standard.json` 結構。

> **只解壓新的日期資料夾，不要重新解壓既有的。**
>
> 增量機制認的是 `(相對路徑, mtime, 檔案大小)` 這組三元組。重新解壓會把既有檔案的
> mtime 全部改成當下時間，於是 `select_pending()` 認定它們「內容變過」，
> 12 萬檔會整批重跑一次——而且不只是慢：那些檔案會走「重新處理」路徑，
> `delete_source_paths()` 會先把既有輸出的對應列刪掉再重寫。結果最後是對的，
> 但你會白等二十分鐘，中途被中斷還會留下刪一半的分區。

macOS 打包的 zip 有時會夾帶 `__MACOSX/` 與 `._*`。目前這批沒有，但下次不保證：

```bash
find data/00_raw_lite -name "__MACOSX" -type d -prune -exec rm -rf {} +
find data/00_raw_lite -name "._*" -type f -delete
```

### 放到別的磁碟（選用）

預設位置是 `data/00_raw_lite/`，不必設定任何東西。這批資料約 **680 MB**，
硬碟空間不夠時可以用環境變數指到別處：

```powershell
$env:CGU_LITE_RAW = 'D:\lite_raw'
```

```bash
export CGU_LITE_RAW=/mnt/external/lite_raw
```

無論用預設還是覆寫，`config.lite_raw_dir()` 都會擋掉指向 `data/00_raw/` 底下的路徑
（見坑 1），並在目錄不存在時告訴你該放哪裡。

### 執行

```bash
python -m src.extract_lite
```

12 萬檔大約要跑二十分鐘（實測約 100 檔/秒）。如果你的執行環境有逾時限制
（背景任務、CI、SSH 連線斷掉），用 `--max-files` 把它切成幾段：

```bash
python -m src.extract_lite --max-files 40000 --chunk-size 5000
```

重複執行到 `尚未處理 0` 為止即可，**每一段都會接續上一段**。這是刻意設計的：
每寫完一塊就存一次 manifest，所以中斷最多損失一塊（預設 10,000 檔）。
本專案第一次匯入 12 萬筆就是分五段跑完的。

`--chunk-size` 只影響「多久存一次檔」，不影響結果；調小會多出一些 parquet 檔，
讀取端不受影響。

### 接壤日會被補齊，那是對的

新舊兩批的交界日會出現「同一個分區多了幾個 part 檔」。因為分區鍵是台北日期，
而每個台北日橫跨兩個 UTC 資料夾（見下面的坑 4），所以舊批最後那個殘缺的台北日
會被新批補滿。

不要因為看到 `date_taipei=2026-08-24/` 底下有 `part-<舊 run>-c0003-0.parquet` 和
`part-<新 run>-c0000-0.parquet` 兩個檔就以為重複了——`request_id` 去重會擋掉
真正的重複，多個 part 檔只是分次寫入的痕跡。要確認的話跑一次
`python -m src.schema_lite`，`request_id` 唯一性是硬性斷言。

---

## 情境二：schema 變更

上游改了匯出格式。這時候 `schema_lite.py` 會開始尖叫，而**尖叫是它的用途**。

> **斷言失敗是資訊，不是障礙。不要為了讓管線跑過去而放寬斷言。**
>
> `schema_lite.py` 裡每一條斷言都是在 120,520 筆全量資料上實測為零違反之後才寫進去的。
> 它失敗代表兩件事之一：上游真的改了，或者你的解析寫錯了。兩種都需要人去看。
> 把 `Check` 拿掉或改成 `nullable=True` 可以讓紅字消失，但消失的是**訊息**不是問題——
> 下游的成本估算、學院歸屬會繼續跑，只是算出來的數字從此不對。
>
> 正確的動作是：先弄清楚上游改了什麼，再決定斷言該怎麼改，並在註解裡寫下
> 「什麼時候、為什麼放寬」。

### 加欄位 vs 移除欄位

兩者的處理方式不對稱，因為 `LITE_REQUEST_SCHEMA` 是 `strict=True`。

**上游多了一個欄位**——你有選擇。不打算用就不必動任何東西：`extract_lite.COLUMNS`
沒列到的欄位根本不會被抽出來，`strict=True` 也就不會有意見。要用的話三個地方一起改：
`extract_lite.COLUMNS`、`DTYPES`、`flatten()`，再到 `schema_lite` 補一條 `Column`。
少改任何一邊，`check_columns_match()` 會在驗證的第一步就擋下來——那條檢查存在的
理由正是「同一份契約寫在兩個地方，只改一邊必須立刻失敗」。

**上游少了一個欄位**——你沒有選擇，一定要動。`flatten()` 會把它讀成 `None`，
於是整欄變 null。這時候要判斷的是**這一欄有沒有人在用**：

- 沒人用 → 從 `COLUMNS`、`DTYPES`、`flatten()`、`schema_lite` 四個地方一起刪掉。
- 有人用 → 停下來。這不是 schema 維護問題，是「某個指標從此算不出來」的問題，
  要先決定那個指標怎麼辦，再回來改 schema。把它留成一整欄 null 是最糟的選擇：
  驗證會過，統計會照跑，只是分母悄悄變了。

欄位改名等於「少一個 + 多一個」，照上面兩條各做一次。

---

## 每批必驗三件事

新資料進來之後，**在做任何分析之前**先跑這三個。它們檢查的都是「跨批次可比性」——
一旦破掉，你手上所有的時間序列都會在某一天悄悄斷掉，而圖看起來完全正常。

### 1. 雜湊 salt 有沒有換

最重要的一件事。`anonymous_user_id` 是上游算出來的匿名 ID，如果它換了 salt，
同一個人在新舊批會拿到不同的 uid，於是「使用者數」暴增、「人均用量」暴跌，
而每一筆資料本身都是對的。

做法是**在抽取新批之前先留一份舊批的快照**，抽完再比對共同的 `request_id`。
順序很重要：`extract_lite` 的 `request_id` 去重會擋掉重複的請求，所以新批寫進去
之後，舊的那些列還在原地、uid 是舊的——直接比對現有輸出是比不出 salt 有沒有換的。

抽取新批**之前**先存一份：

```bash
python -c "
from src import extract_lite
f = extract_lite.load_dataset()[['request_id', 'anonymous_user_id']]
f.to_parquet('uid_snapshot_before.parquet', index=False)
print(f'{len(f):,} 筆快照')
"
```

抽取之後比對：

```python
import pandas as pd
from src import extract_lite

old = pd.read_parquet("uid_snapshot_before.parquet")
new = extract_lite.load_dataset()[["request_id", "anonymous_user_id"]]
m = old.merge(new, on="request_id", suffixes=("_old", "_new"))
same = m["anonymous_user_id_old"] == m["anonymous_user_id_new"]
print(f"{same.sum():,}/{len(m):,} = {same.mean():.6%}")
```

**本批基準：120,520/120,520 相同（100.000000%）。**

不是 100% 就停下來，先弄清楚為什麼，再決定新舊批能不能放在同一張圖上。
只差幾筆也要查——salt 沒有「只換一部分」這種事，少數不符更可能代表別的東西壞了。

### 2. 有沒有新的系代號

```bash
python -m src.college
```

看「registry 有但對照表查不到」那一節。這是拿 clean 的 `user_registry.csv` 比對；
lite 這邊要看 `ref/lite_user_registry.csv` 的 `dept_code` 欄。

再看 `docs/data_lite/unmapped_dept_codes_lite.csv` 有沒有列。**空表是正常狀態**，
表示全部學生識別碼都已歸屬。若出現任何一列，表示有新的系所代碼進入使用而對照表
尚未涵蓋——取得該代碼的系所與學院之後補進 `ref/college_mapping.csv`。

這裡刻意不列「目前對不到哪幾個代碼」。列出來的話，代碼補齊之後那份清單就成了
一段沒人會發現已經過期的斷言——本文件曾經列過六個，六個後來全部補上了。
要知道現在缺哪些，去看那張表；它就是為了回答這個問題而存在的。

補表時要確認 `dept_key`：研究所代碼可能與同系所的大學部代碼不同（生醫 09／01、
醫工 26／31、人工智慧 28／61），成對的兩個代碼要填同一個 `dept_key`，否則同一個系
會被切成兩個。見 [DESIGN_NOTES.md](DESIGN_NOTES.md) 的〈同一個系所在不同學制使用
不同的系代號〉。

### 3. 有沒有新的模型

```bash
python -m src.pricing_check data/01_request_lite
```

**這一條是雙軌檢查。** 2026-08-27 補齊價目之後，`unpriced_no_table` 這個桶已經
清到「有 token 的請求 0 筆」，所以單看佔比會失去解析度——從 0% 起跳的任何變動
在比例上都是無限大。兩條各自負責一種訊號：

**軌一：絕對筆數（偵測新模型）**

    unpriced_no_table 中 token > 0 的筆數 > 0  →  列出 model_family 清單並補價

目前基準是 **0 筆**。這個桶現在只剩 230 筆**全部 token 為 0** 的請求
（24 個 model_family，多為 codex / realtime / image 系列的失敗或空回應），
它們本來就沒有 token 可計價，不需要價目。所以**任何 token > 0 的殘留都是新模型**，
不需要判斷佔比高低——直接查。

**軌二：token 佔比（偵測量級事故）**

    unpriced_no_table 的 token 佔應計費 token > 0.5%  →  告警

軌一漏掉一批的話（例如查價當天官方頁面掛掉），軌二會在它吃掉可觀的量時叫出來。
0.5% 這個門檻的參考點：補價前那批 18 個模型合計是 **0.2287%**，也就是說
即使整批漏掉也不會觸發軌二——**軌二不是軌一的備援，是量級事故的獨立警報**。

兩軌都跳的話先看軌一的清單。跳升代表有模型不在 `ref/pricing_table.csv` 裡，
成本估算會把它整個漏掉——而且是無聲的，總成本只會偏低，不會報錯。

**token = 0 的請求不需要價目。** 它們在 `cost_by_model_family_lite` 裡的
`cost_usd` 是 NA 而不是 0（我們不知道，不是不花錢），但因為沒有 token，
補了價算出來也還是 0。不要為了「把桶清空」去替它們建價目列。

**補價的時機**：發現就補，不要等。官方頁面只列**現價**，三個月後再補就成了考古
——`gpt-5.6-sol` 在 2026-08-22 之前的快取寫入價就是靠「快取寫入 = 標準輸入 ×
1.25」這條規律推出來的，那筆金額至今標著 `inferred`（佔總額 13.55%）。

---

## 情境三：重跑用途分類的前置規則

四條前置規則決定分類母數（見 OVERVIEW 4.2）。上游工具改版、或新增一批資料
之後要重跑一次，確認判定字串還認得出來。

分兩段，**成本差兩個數量級**：

```bash
python -m src.classify_lite.markers      # 讀原始 JSON，約 3.5 分鐘
python -m src.classify_lite.prefilter    # 只吃上一段的輸出，秒級
```

> **不要被「讀原始 JSON」嚇到——它只讀 31,011 個代表檔，不是 12 萬個全檔。**
> 每個相異 prompt 只取一筆代表（最早的那筆），實測 **3 分 24 秒**。
>
> 這條特別寫出來，是因為一開始估的是 25 分鐘（用全檔數 × 80 檔/秒 推的），
> 而那個估計值會讓下一個人以為重跑很貴、於是不敢跑。**規則改了就重跑，
> 三分半而已。**

第二段秒級，**所以調整判定字串時只要重跑第二段**——`markers_lite.parquet`
記的是每個相異內容命中了哪些標記，規則怎麼組合是第二段的事。

第一段分七塊、每塊存一次進度到 `data/_manifest_lite/markers_progress.parquet`，
中斷可續（重跑會跳過已掃過的 sha256）。要分段執行用 `--max-files`。

判定字串在 `ref/prefilter_markers.csv`，不在程式裡。新增一條標記是改表，
不是改程式——理由與 `college_mapping.csv`、`pricing_table.csv` 相同。
**缺表時第一段會直接 raise 而不是用空清單繼續**：空清單會讓四條規則全部
零命中，而母數看起來仍是個合理的數字。

驗收看兩件事：

1. `prefilter` 印出的四項相加 + 剩餘母數 **必須正好等於相異內容總數**。
   對不上就是指派層級與去重層級不一致，見 DESIGN_NOTES 對應章節。
2. 規則 4 的兩條標記目前 100% 共現（各 10,507）。**不再相等就表示那支
   ollama 分類器的外殼改版了**，要去看新版長什麼樣。

本期間基準（2026-09-05 重跑）：

    母數        31,011 個相異內容 / 106,993 筆請求
    1 向量化       587 /  1,010
    2 影像生成     401 /    442
    3 工具自動發出 2,387 / 11,116
    4 流程中繼處理 10,271 / 36,325
    需送分類器   17,365 / 58,100

---

## 已知的坑

**1. 絕對不要把 lite 檔案放進 `data/00_raw/`——但兄弟目錄是安全的。**
clean 的 `extract.py:658` 是 `config.DATA_RAW.rglob("*.json")`，而
`DATA_RAW = data/00_raw`。`rglob` 從那裡**往下**遞迴，所以：

- **危險**：`data/00_raw/` 底下的任何位置，包括再深的子目錄。
- **安全**：`data/00_raw_lite/`。它是 `00_raw` 的**兄弟目錄**，不在遞迴範圍內。
  名字前綴看起來相像，但 `rglob` 走的是目錄樹不是字串比對。

放錯的後果是 12 萬列幾乎全 null 的資料——而 `request_id` 在兩套 schema 裡剛好都是
頂層同名欄位、**會有值**，所以 schema 驗證未必攔得住，你會拿到一批「看起來有資料」
的垃圾。

`config.lite_raw_dir()` 會擋掉指向 `data/00_raw/` 底下的路徑（用 `Path.relative_to`
判斷，不是字串前綴——字串比對會把合法的 `00_raw_lite` 誤判成子目錄）。但它只管得住
路徑設定，擋不住有人手動把檔案複製進 `00_raw/`。

**2. 分區鍵是 `received_at` 轉台北時間。**
不是 `created_at`，也不是來源資料夾名。三種口徑在全期有 **10 筆**跨午夜的請求會分到
不同日期，其中一筆 `received 2026-08-19T23:59:48Z / created 2026-08-20T00:00:03Z`
正好落在 08-19 與 08-20 的分界。
選 `received_at` 是為了跟 clean 的 `date_taipei` 對齊——那邊走的就是
`time.received_at → 台北`。順帶一提 `created_at - received_at` **不是延遲**：
與 `latency_ms` 的相關係數只有 0.06，它是日誌落地端的固定偏移（lite 約 27.6 秒、
clean 約 17.5~20.8 秒），`schema_lite` 拿它當上游流程變更的哨兵。

**3. 明文 prompt_text 不落地。**
原始 JSON 有 `request.prompt_text`，抽取時只寫 `prompt_text_len` 與
`prompt_text_sha256`。長度夠做分布分析，雜湊夠判斷「是不是同一份 payload 重送」。
明文一旦寫進 parquet 就會跟著 `data/` 被備份、被複製，再也收不回來。
`schema_lite.check_columns_match()` 有一條專門的檢查：輸出裡出現 `prompt_text`
就直接 raise。

**4. 首尾分區必然不完整。**
台北是 UTC+8，UTC 的某日 D 換算成台北是 `D 08:00` 到 `D+1 07:59`，所以**每個完整的
台北日都要兩個 UTC 資料夾才湊得滿**。資料範圍最早那天只有 16 小時、最晚那天只有
8 小時（本批的 `2026-08-24` 只有 2 小時，因為 UTC 08-23 的資料本身就在下午結束）。
**逐日趨勢一定要排除或標記頭尾兩天**，否則會看起來像用量在期初期末暴跌。
`python -m src.schema_lite` 會印出分區完整度表，並區分「連續缺口」與「零星缺口」——
凌晨 3–7 點缺一兩個小時是沒人用，不是資料問題；連續缺好幾小時才值得查。

**5. 08-07 到 08-10 那段空白是服務中斷，不是漏匯出。**
UTC 的 `2026-08-08/` 與 `2026-08-09/` 資料夾**根本不存在**，很容易讓人以為是上游漏了
兩天沒匯出。實測不是：

```
空白前最後一筆  2026-08-07 05:23:02.586148Z  （台北 13:23:02）
空白後最早一筆  2026-08-10 06:30:06.912865Z  （台北 14:30:06）
中間請求數      0
實際空白長度    3 天 01:07:04  = 73.12 小時
```

判準是**邊界的形狀**。漏匯出會整個日期資料夾消失，但前後兩天仍然完整；這裡不是——
`2026-08-07/` 的 1,565 筆全部落在 UTC 00:00–05:59（下午整個沒有），`2026-08-10/`
則從 UTC 06:30 才開始。兩端都在半天處被切斷，而且兩天的 `_prepare_report.json` 都寫著
`skipped_records: 0, failed_files: 0`——匯出程式把它找到的檔案全部處理完了，
它就只找到那麼多。這是服務端在那段時間沒有產生請求，不是匯出端漏掉。

所以那三天不該被當成「資料缺漏、要回頭補」，也不該被當成「使用量下降」——
那段時間根本沒有服務可用。逐日趨勢要把它標出來，不是內插補值。

**6. manifest 不會自己清掉消失的來源檔。**
刪掉來源資料夾之後，manifest 裡那些列還在。多半無害（`select_pending()` 只用它判斷
「這個路徑處理過沒」），但會讓 manifest 筆數與實際檔數對不起來。要清就明講：

```bash
python -m src.extract_lite --prune-manifest
```

刻意不自動清：來源檔暫時不在（外接碟沒掛、網路磁碟斷線）跟「這批不要了」
長得一模一樣，自動清會把前者的增量狀態一起洗掉，下次得整批重跑。

**7. 在 `ref/` 建新檔案要同步改 `.gitignore` 的白名單。**
`.gitignore` 對 `ref/` 是白名單制：`ref/*` 擋掉全部，再逐項 `!` 放行。
新增一張對照表而忘了放行的話，**`git status` 不會提醒你**——被 ignore 的檔案
根本不會出現在那份清單裡。於是它安靜地只留在你本機，直到有人 clone 之後
點到文件裡的連結才發現。

已經發生過一次：`ref/prefilter_markers.csv` 建立時漏了放行，而 OVERVIEW 4.2
已經明文引用它。判斷放不放行看兩件事——**有沒有個資、是不是人工維護的判斷**。
兩者皆是就放行（對照表、價目表、判定字串、分類字典），含帳號的一律不放
（`user_registry.csv`、`lite_user_registry.csv`）。

```bash
git check-ignore -v ref/<新檔名>   # 有輸出就代表被擋住了
```

**8. `git clean -xfd` 會把原始資料一起刪掉。**
`-x` 的意思是「連被 gitignore 的檔案也刪」，而 `/data/` 正是被 gitignore 的。
原始資料現在住在 `data/00_raw_lite/`，所以在專案根目錄跑這個指令會連 **680 MB
原始 JSON**（以及 `data/01_request_lite/` 的 parquet 和 manifest）一起清掉。

Drive 上還有壓縮檔可以重下，所以這是麻煩不是災難——但重下、解壓、重跑抽取
大概要花掉一個小時。要清乾淨工作目錄的話用 `git clean -fd`（不加 `-x`），
或先確認 `git clean -xfdn`（`-n` 是 dry run）列出來的東西你都真的不要。

**9. 重算成本一定要走 `extract_lite.load_dataset()`，不能自己拼 parquet。**
`data/01_request_lite/` 是 Hive 分區目錄，`date_taipei` 這一欄**不存在於任何
parquet 檔內**——它只在用目錄路徑讀取時由 pandas 從資料夾名產生。

```python
# 對
from src import extract_lite
frame = extract_lite.load_dataset()

# 錯：少了 date_taipei，總額會變成 $2,754.80 而不是 $2,749.32
frame = pd.concat([pd.read_parquet(f) for f in glob('data/01_request_lite/*/*.parquet')])
```

`pricing` 要靠 `date_taipei` 判斷請求落在 gpt-5.6-sol 2026-08-22 降價的前面還是
後面。少了那一欄，生效日分段會**靜默**挑到另一組價目：不拋例外、不產生 NA，
四段金額照樣加總等於總額，每一項內部一致性檢查都會通過。唯一的訊號是跟正式
輸出比對時差了幾塊錢。

**這一條對外也成立。** 拿 `docs/data_lite/` 的 csv 或原始 parquet 想自行重算的人
會得到不同的數字，而他沒有理由知道要走那個函式。要對外提供可重算的成本數字，
必須連同這條說明一起給。

---

## 跨批次去重是有效的（已驗證）

「輸出 0 列、丟棄 N 筆」這個結果，光看數字分不出是「真的沒重複」還是「去重根本沒被觸發」。
所以做過一次受控測試：把 `2026-08-23/` 原樣複製成 `2026-08-23-dup/` 再跑一次抽取。

```
掃描 121,864 檔，待處理 1,344 檔，跳過 120,520 檔
第 1/2 塊：讀 1000 檔，寫 0 列
第 2/2 塊：讀 344 檔，寫 0 列
去重：與既有輸出或批次內重複 1,344 筆
輸出列數 0（去重丟棄 1,344）
```

總列數維持 120,520、parquet 檔數維持 52。**新路徑會被當成新檔讀進來**（manifest 認路徑，
不認內容），但第二層的 `request_id` 去重把它們全部擋掉了。

值得記住的是這兩層各自擋什麼：manifest 擋的是「同一個檔案不要重讀」，
`request_id` 擋的是「同一筆請求不要重複寫入」。**只有後者能擋住換了路徑的同一批資料**，
而換路徑正是擴充資料時最容易發生的事。
