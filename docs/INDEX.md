# 指標索引

本檔由 `python -m src.render_index` 從 `src/metrics/registry.py` 的
註冊表產生，請勿手動編輯。要改內容請改指標的 `@metric` 參數。

已註冊指標：19 個

| 指標名 | 回答什麼 | 單位 | 來源表 | 分母 | 覆蓋率 | 注意事項 | 版本 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `anomaly_profile` | 有哪些使用者的行為明顯偏離其他人？各是什麼樣的行為？ | user | request | 三個各自定義的切片：context_length_exceeded 錯誤、舊模型族 gpt-4o-mini 的請求、台北時間 0–7 時的離峰請求。每個切片只在單人佔比超過 80% 時列出。 | 17.1% | **這張表描述行為特徵，不指名個人**。輸出不含 username，也不含任何能直接還原到帳號的欄位。要把某一列對應到具體帳號，請查 `ref/user_registry.csv`——該檔不進版控，且需要另外的授權。「佔比」是該類異常佔全體請求的比例，分母固定是全部請求。**三列不必然是三個人**：同一個帳號可能同時觸發多個判準，n_users 是該列自己的人數，不可跨列相加。判準是描述性的門檻（見 denominator），不是偵測規則——沒被列出來不代表沒有異常，只代表沒有超過這三條門檻。**不做歸因**：規律的請求間隔與自動化一致，但也與任何固定輪詢的正常用途一致，這張表不判斷那是濫用還是正當使用。 | 1.0 |
| `cache_hit_by_request_position` | 在一個 turn 內，第幾個請求開始吃到 prompt 快取？ | request | request | turn_id 非 null 且 usage_missing=False 且 prompt_tokens>0 的請求，依 turn 內時間序分成第 1、第 2、第 3 個以後 | 99.3% | **這是協定層的機制特性，不是使用者行為差異**。第一個請求快取率低是因為前綴尚未被快取，與該使用者「用得好不好」無關，不可拿來比較人或族群。這個指標不依賴樣本量，1.8 天的資料也足以觀察機制。**兩條排除規則各自成欄**：n_excluded_usage_missing（token 遺失，比值算不出來）與 n_excluded_zero_prompt_tokens（分母為零）。兩者相加才是母數與覆蓋數的差額；合成一欄的話，讀者無從判斷少掉的請求是資料品質問題還是結構性的零分母。 | 1.1 |
| `context_length_exceeded_profile` | 撞到上下文長度上限的請求長什麼樣？ | request | request | error_code = context_length_exceeded 的 47 筆請求 | 0.5% | **母數只有 47 筆，只做描述、不出任何比例**。不可據此估算「多少比例的使用者會撞到上限」，也不可拿來比較模型或族群——樣本量不支持任何比較。這些請求被拒絕於模型之前，usage_details 為空，其 token 欄位不可信（見 n_usage_missing）；prompt_len 字元數才是可用的長度訊號。 | 1.0 |
| `dataset_scale` | 這批資料有多大？四層母數各是多少、涵蓋哪幾天？ | request | request | 無分母。這是規模描述，不是比例。 | 100.0% | 只涵蓋 2026-07-21 至 2026-07-23 約 1.8 天，且第一天與最後一天都不完整。不可據此推估日均量或月量，也不可視為 gateway 的完整歷史。四層母數逐層下降是 agent 展開造成的，不是資料遺失。每日一列的備註附上星期與當日使用人數（原 requests_by_weekday 指標已併入此處）——3 個 weekday 值、其中兩個是不完整的一天，『平日 vs 週末』之類的比較在這份資料上沒有意義，因此不再單獨出表。「codex 請求無 turn_id」一列是刻意保留的缺口：那些請求不屬於任何 turn，也沒有被塞進任何 turn，所以以 turn 為母體的指標（turn_expansion_depth、cache_hit_by_request_position）的母數會比 client_type=codex 的請求數少這幾筆。 | 1.1 |
| `model_consistency` | gateway 回報的模型，與回應摘要裡記的模型一致嗎？ | request | request | model_returned 與 response_model 兩欄都有值的請求 | 19.7% | **這是資料勾稽，不是服務品質指標**。現有的『模型替換率 0.01%』是拿 gateway 自己的 model_requested 與 model_returned 相比得出的——兩欄都由同一段程式寫入，若那段程式有系統性錯誤，比對結果會一致地錯，從數字上看不出來。response_model 來自回應摘要，是目前唯一能獨立佐證的第三個欄位。怎麼讀：兩者完全一致，代表 gateway 忠實轉發，原本的替換率數字可以採信；出現不一致，那個差異才是真正需要追的替換。**最重要的限制：這個檢查涵蓋不到 Codex 流量**。response_model 只出現在 1,958 筆請求上（全體的 19.7%），而且**全部都是 direct 客戶端**——7,115 筆 codex 請求無一有這個欄位。因此即使一致率 100%，能佐證的也只有直呼那一段；佔七成的 Codex 流量仍然只能靠 gateway 自己的兩個欄位互證，無法獨立驗證。要補這個洞只能從上游想辦法，不是這份資料能解決的。**一致不等於正確**——兩欄仍可能來自同一次上游回應，這個檢查排除得了轉發過程的竄改，排除不了上游本身回報錯誤。版本後綴（-YYYY-MM-DD）已在比對前剝除，否則同一模型的不同快照會被算成不一致。 | 1.0 |
| `prompt_length_distribution` | 送出去的 prompt 有多長（字元數）？ | request | request | prompt_len > 0 的請求（9,450 筆，佔全體 95.1%） | 95.1% | **只有長度沒有內容**——L1 刻意只保留字元數，原文含個資與本機路徑，不進表。字元數不等於 token 數，中英文比例不同會讓兩者差異很大，不可互相換算。**這個欄位只含本次送出的輸入，不含對話歷史**（已驗證，非推論）：1,025 個 turn 中有 997 個（97.3%）的 prompt_len 在 turn 內完全不變，而同批 turn 的 message_count 全數成長；相鄰請求對有 99.2% 的 prompt_len 不動，其變動與 user_message_count 變動的一致率 99.8%；與 message_count 的相關性為負（spearman -0.34），與 user_message_count 亦為負（spearman -0.41）——若含歷史，這兩個相關性都該是正的。反面案例：message_count=560 而 prompt_len=1。另注意欄內混有客戶端自動產生的輸入（任務說明、附件包裝、建議指令），不全是使用者鍵入的內容。最長一筆達 900,179 字元，與 context_length_exceeded 錯誤可能相關（見 context_length_exceeded_profile）。direct 這一組被單一使用者主導，分位數會被抑制——該組的長度分布實際上是一支腳本的行為。**但這裡的抑制擋不住任何東西**：本表同時輸出未抑制的「全體」列，拿它與 codex 列對照就能讀出 direct 的分布形狀（全體 p99 遠高於 codex p99，差額只可能來自 direct）。留著全體列是因為它本身有用；suppression_reason 欄會把這件事寫在被抑制的那一列上，不假裝擋住了。 | 1.1 |
| `reasoning_effort_distribution` | Codex 請求用了哪些推理強度設定？ | request | request | 7,115 個 client_type=codex 的請求（direct 客戶端不帶這個參數） | 100.0% | **這是客戶端預設值的分布，不是使用者偏好的分布**。同一批請求的 include_options 唯一值只有一個（reasoning.encrypted_content，7,115 筆全同、覆蓋率 71.6% 與 codex 請求數完全吻合），顯示沒有任何使用者調整過推理相關設定。因此 effort 的高低只反映 Codex 各版本／各操作路徑的內建預設，不可解讀成「使用者想要更深的推理」。reasoning_effort 分的是請求不是人，**不套用抑制**，請對照 n_users 讀。 | 1.0 |
| `requests_by_account_type` | 各身分別（學生／教職員／服務帳號）各有多少人、發了多少請求？ | request | user | 全部 9,937 個請求，依 username 對應的 account_type 分組 | 100.0% | account_type 來自被遮罩的 user_account（末三碼為 XXX），同前綴的不同人會合併，但身分別的判定本身可信。不可回推到個別帳號。service 是 link-*/open* 服務憑證，不是人，做人均統計時必須先剔除。佔比欄位會依集中度規則自動抑制；被抑制的組仍保留計數。 | 1.1 |
| `requests_by_client_type` | Codex 客戶端與直呼（SDK/curl/自寫程式）各佔多少請求、多少人？ | request | request | 全部 9,937 個請求，依 thread_id 是否存在二分 | 100.0% | direct 這一組不能當成「25 個人的行為」：單一使用者佔該組 token 的 83.6%、請求數的 61.0%，其行為特徵基本上是一個人的腳本。direct 的所有比例欄位因此會被抑制，計數保留。另外 client_type 是用 thread_id 是否為 null 判定的，有 9 筆 direct 請求帶著 store、6 筆帶著 prompt_cache_key，代表少數直呼客戶端會手動帶 Responses API 參數。 | 1.0 |
| `requests_by_endpoint` | 使用者拿 gateway 做什麼？各 API 端點的請求數與使用人數。 | request | request | 全部 9,937 個請求 | 100.0% | 端點是「拿來做什麼」最可靠的結構訊號（對話／語音／轉錄／生圖），因為它不依賴任何內容判讀。但它只說明呼叫了什麼 API，不說明使用者的任務目的——同一個 /v1/responses 可能是寫程式也可能是翻譯。endpoint 分的是請求不是人，因此**不套用抑制**：低請求量的端點（如 /v1/images/edits 僅 1 筆、1 人）比例照樣輸出，請自行對照 n_users 判斷母數——一個人的 1 筆請求佔 0.01%，這個 0.01% 是事實，但它描述的是一個人。 | 1.0 |
| `requests_by_hour` | 這 1.8 天內，請求落在台北時間的哪些小時？ | request | request | 全部 9,937 個請求，依 hour_taipei 分組 | 100.0% | 資料只涵蓋約 1.8 天（2026-07-21 13:20 至 07-23 07:56，台北時間），且頭尾兩天都不完整。**完全不足以談週期性、尖峰時段或作息模式**。這張表只能當成「這 1.8 天內請求落在哪些時段」的事實描述，不可外推、不可與其他期間比較、不可用來排程或估算容量。時段分的是請求不是人，**不套用抑制**——「凌晨 3 點只有 2 個人在用」本身就是要報的事實，把它清成 NA 只會讓讀者以為資料缺漏。代價是低量時段的比例可能出自一兩個人，請一律對照 n_users 讀。 | 1.0 |
| `requests_by_model_family` | 實際服務請求的是哪些模型族？ | request | request | 有 model_family 的請求（9,935 筆）。缺值的 2 筆單列一行，不進分母。 | 100.0% | 使用 model_family（已剝除 -YYYY-MM-DD 版本後綴）而非 model_returned。不做這步的話，同一個模型的不同快照會被當成不同模型：『請求的模型 ≠ 回傳的模型』的比率會顯示為 57.96%，剝除版本後實際只有 0.01%（9,935 筆中僅 1 筆真正被替換）。這個欄位回答『誰在服務』，不回答『使用者選了什麼』——後者要看 model_requested。**request_share 的分母是 9,935（有 model_family 的請求）不是 9,937**。表尾的 `(未記錄)` 一列是 model_family 為 null 的 2 筆，計數照列但不出比例——它不屬於任何模型族，放進分母會讓其他列的比例失真。因此比例欄加總為 1.0000，計數欄加總為 9,937。捨入殘差補在請求數最大的那一組（見 _shares_exact），該格最多偏離 0.0001。model_family 分的是請求不是人，**不套用抑制**；多數模型族只有個位數使用者，請對照 n_users 再引用比例。 | 1.1 |
| `status_and_errors` | 請求的成功率如何？失敗的是哪些錯誤？ | request | request | 全部 9,937 個請求 | 100.0% | 429（額度限制）、402（付款要求）、403（禁止）**皆為零筆**，代表這段期間沒有任何技術性額度阻擋——因此『使用量低』不能被解釋成『被擋住了』。唯一的 520 是上游異常，唯一的 404 是端點不存在。400 全部是 invalid_request_error，屬於客戶端送錯參數，不是服務故障。錯誤筆數極少（86/9937），任何依錯誤分組的比例都不可用。**error_code 區塊含一列 `(無)`**：部分 4xx/5xx 請求的 error 物件沒有 code 欄位（只有 type 與 param，520 則連 error 物件都沒有）。列出來是為了讓 error_code 區塊加總等於 error_4xx_5xx；少了它，兩個數字差幾筆而讀者查不出差在哪。 | 1.1 |
| `thread_tool_message_ratio` | 一個 thread 的對話歷史裡，有多少比例是工具訊息而非人／模型的話？ | thread | thread | 435 個 thread，比值 =（peak - user - assistant）/ peak | 100.0% | **這是訊息「數量」的比例，不是時間比例也不是成本比例**。一則工具訊息可能只有幾個 token，一則使用者訊息可能有幾千個，不可據此推論 agent 消耗了七成資源。message_count_peak 是該 turn/thread 內送出過的**最長歷史長度**，不是最終訊息數。有 19 個 turn 的 has_compaction=True（歷史在同一個 turn_id 內被重置過），對這些 turn，峰值高於壓縮後的實際狀態，而壓縮前後兩段被合併成同一列（見 n_segments）。 | 1.0 |
| `token_inflation_by_client_type` | 原始 prompt_tokens 相對於實際未快取部分放大了幾倍？ | request | request | usage_missing=False 且 prompt_tokens - cached_tokens > 0 的請求，比值 = prompt_tokens /（prompt_tokens - cached_tokens） | 97.5% | **用來說明「原始 token 數會高估實際資源消耗」，不是計費依據**。快取命中的 token 仍會出現在 prompt_tokens 裡，直接加總 prompt_tokens 會把同一段前綴重複計算數十次。分母為 0（完全命中快取）的請求無法計算比值，已排除並記在 n_excluded_zero_denominator。token 加總已排除 usage_missing=True 的請求。direct 這一組被單一使用者主導，其分位數會被抑制——那不是計算失敗，是因為該組的中位數其實在描述一個人。 | 1.0 |
| `tool_types_distribution` | thread 裡掛載了哪些類別的工具？ | thread | thread | 有 tool_types_union 的 thread（其餘 thread 從未回報工具設定） | 42.5% | （a）這是**宣告掛載**不是**實際呼叫**。一個 thread 掛了 web_search 不代表它真的搜尋過，資料裡沒有任何欄位能區分掛載與呼叫。（b）只有工具**類別**（function/custom/web_search…），沒有工具名稱，因此無法知道使用者實際用了哪些具體功能。（c）缺失非隨機：沒有 tool_types 的請求集中在 subagent 與標題生成之類的輔助呼叫，這些本來就不掛工具，所以「沒有工具的 thread 比例」不能解讀成「使用者不用工具」。（d）**比例的分母是有工具宣告的 thread，不是全部 thread**。欄名為此改成 declared_thread_share。function 的 share = 1.0 意思是「所有**有宣告**的 thread 都掛了 function」，不是「所有 thread 都掛了 function」——後者的分母大了 2.35 倍。分母本身列在表尾兩列（`（分母）有工具宣告的 thread`／`（分母）無 tool_types 欄位的 thread`），不必回頭查 coverage 才知道。維度不在 concentration.csv 內，無法自動抑制，請自行對照 n_users。 | 1.1 |
| `turn_expansion_depth` | 一次使用者動作被 agent 展開成幾個 API 請求？ | turn | turn | 1,025 個 turn_id 非 null 的 turn（direct 客戶端沒有 turn，不計入） | 100.0% | **不是效能指標也不是成本指標**。展開深度高只代表 agent 往返多，不代表使用者等待久或花費高。務必看 p50 而非 mean：mean=6.94 但 p50=1，一半的使用者動作只產生一個請求，平均值被長尾嚴重拉高。輸出並列兩欄：「全體」與「排除有壓縮的 turn」。有壓縮的 turn 內對話歷史被重置過，n_requests 把壓縮前後兩段合併計算，一個 turn_id 實際上是兩次以上的動作。兩欄並列是為了讓讀者直接看到這批 turn 對 p99 與 max 的影響有多大——若兩欄的 p50 相同、只有尾端不同，代表壓縮只污染極值，中位數仍可用。 | 1.2 |
| `usage_missing_impact` | 有多少請求的 token 用量沒有被記錄？它們是什麼樣的請求？ | request | request | 全部 9,937 個請求中 usage_missing=True 的 188 筆 | 1.9% | **這 188 筆的 token 是「遺失」不是「0」**。其中 102 筆是 status=200 的串流請求，total_tokens 全部記成 0——直接 sum(total_tokens) 會把它們當成零消耗而低估總量。所有 token 加總都應排除這些列，並在結果中註明排除筆數。另外 84 筆是 400 錯誤，請求根本沒送到模型，沒有用量是正確的。兩種成因必須分開理解，不可一律當成資料品質問題。 | 1.0 |
| `users_by_degree_and_entry_year` | 有哪些學位別與入學學年的學生在使用？ | user | user | 43 個 account_type=student 的 username | 100.0% | **只回答「有誰在用」，不回答「誰用得多」**。幾乎所有分組都會觸發抑制（母數 < 10 或單人佔比 > 30%），比例欄位大量為 NA，這是預期行為而非計算失敗。degree/entry_year 來自 user_account 的可見前綴，遮罩會把同前綴的不同人合併，人數是下界不是精確值。staff 與 service 帳號沒有這兩個屬性，不在此表內。 | 1.0 |

## 分組指標與抑制

宣告了 `group_by` 的指標共 9 個，其中 5 個受抑制、4 個依政策豁免。

### 受抑制的維度

只有**把人分群**的維度才抑制：`account_type`、`degree`、`entry_year`、`dept_code`、`client_type`。
依 `runs/<run_id>/concentration.csv` 判定，觸發任一條件即抑制：

- 分組人數 < `MIN_GROUP_SIZE`（10）
- 單一使用者佔該組流量 > `DOMINANT_THRESHOLD`（30%）

觸發時該列的比例欄位置為 NA，**計數欄位保留**。
計數是事實，比例才有再識別與誤導風險。

- `prompt_length_distribution`：分組維度 client_type
- `requests_by_account_type`：分組維度 account_type
- `requests_by_client_type`：分組維度 client_type
- `token_inflation_by_client_type`：分組維度 client_type
- `users_by_degree_and_entry_year`：分組維度 degree, entry_year

### 豁免的維度

時段、端點、模型、狀態碼這類維度分的是**請求**不是**人**，不抑制。
抑制它們只會把事實抹掉——「凌晨 3 點只有 2 個人在用」本身就是要報的事實，
清成 NA 反而讓讀者以為資料缺漏。
代價是低量分組的比例可能出自一兩個人，因此這些指標一律附上 `n_users`，
由讀者自行判斷母數厚薄；缺 `n_users` 時 registry 會發出警告。

- `reasoning_effort_distribution`：分組維度 reasoning_effort（附 n_users）
- `requests_by_endpoint`：分組維度 endpoint（附 n_users）
- `requests_by_hour`：分組維度 hour_taipei（附 n_users）
- `requests_by_model_family`：分組維度 model_family（附 n_users）

## 欄位說明

- **單位**：這個指標的分析粒度（request / turn / thread / user）。
- **來源表**：實際讀哪張表計算。
- **分母**：比例的母體是什麼。分母講不清楚的比例不能用。
- **覆蓋率**：最近一次執行時，母體中實際有值的比例。
  尚未執行過的指標標示為「未執行」。

