# 文件導覽

`docs/` 底下的文件分成四組，依讀者的目的排列。第一次讀的話，照組別順序讀即可；每份文件開頭都會說明它回答什麼、不回答什麼。

所有數字以 [data_lite/](data_lite/) 與 [data/](data/) 的 csv 為準。散文、表格與圖都是從這些 csv 產生或引用的；兩邊對不上時，以 csv 為準。

## 一、結論

| 文件 | 回答什麼 | 什麼時候讀 |
| --- | --- | --- |
| [../README.md](../README.md) | 三個問題的結論、可信度與界線，一頁講完 | 最先讀 |
| [OVERVIEW.md](OVERVIEW.md) | **主要交付物。** 七節完整論證：資料範圍、使用單位、成本、用途分類的可回答界線、資料內容特徵、已知限制、方法 | 要引用任何結論之前 |

## 二、完整數字

這兩份不是新舊版本的關係。兩批資料的期間、識別碼與結構都不同，回答的也是不同的問題，不可合併或相加。

| 文件 | 資料 | 回答什麼 |
| --- | --- | --- |
| [RESULTS_lite.md](RESULTS_lite.md) | lite：2026-07-30 至 08-24，24 個有資料的曆日 | 誰在用、花多少的完整表格 |
| [RESULTS.md](RESULTS.md) | clean：2026-07-21 至 07-23，約 1.8 天 | 一次操作內部發生了什麼：快取位置效應、請求展開、工具往返。唯一帶對話結構的資料 |

## 三、方法與限制

讀結論時產生「這個數字怎麼來的」「能不能這樣用」的疑問，答案在這一組。

| 文件 | 回答什麼 |
| --- | --- |
| [INDEX.md](INDEX.md) | 每個指標的定義、分母、覆蓋率、抑制狀態與注意事項。表格由程式產生 |
| [UPSTREAM_FILTER.md](UPSTREAM_FILTER.md) | 上游在匯出前排除了哪些記錄，以及這對規模與成本數字代表什麼 |
| [CLASSIFICATION_LIMITS.md](CLASSIFICATION_LIMITS.md) | 用途分類的分流層為什麼不能當用途來源：兩模型一致率、中繼資料不可分的證明、人工標註的限制 |
| [DOMAIN_CROSS_RESULTS.md](DOMAIN_CROSS_RESULTS.md) | 600 筆逐筆 domain 判定的兩模型交叉結果、分層加權估計與未解異常。**是探索性模型估計，不是已驗證的用途分布** |
| [DOMAIN_CAP_CALIBRATION.md](DOMAIN_CAP_CALIBRATION.md) | 判定時送出內容上限（1,200 與 4,000 字元）的校準 |
| [../ref/annotation_protocol.md](../ref/annotation_protocol.md) | 凍結座標、標註規則、退回門檻，以及可分類母數的上限 |
| [../ref/FREEZE_2026-09.md](../ref/FREEZE_2026-09.md) | 取得新驗證資料前凍結的分類法、提示詞與門檻 |

## 四、工程與過程紀錄

| 文件 | 回答什麼 |
| --- | --- |
| [RUNBOOK_lite.md](RUNBOOK_lite.md) | 拿到新一批 lite 匯出時怎麼擴充與重跑，哪些地方會出錯、為什麼 |
| [ENGINEERING_NOTES.md](ENGINEERING_NOTES.md) | 可以帶到其他專案的通用工程規則，每條附原始案例 |
| [PROGRESS.md](PROGRESS.md) | 2026-08-27 的階段性快照。**不是現況**，保留作為過程紀錄 |
| [MAINTAINING.md](MAINTAINING.md) | 要改程式或文件的人動手前必讀：驗收指令、禁止事項、遮蔽規則與凍結座標 |

## 資料與圖

| 位置 | 內容 |
| --- | --- |
| [data_lite/](data_lite/)、[figures_lite/](figures_lite/) | lite 的發佈 csv 與圖 |
| [data/](data/)、[figures/](figures/) | clean 的發佈 csv 與圖 |
| [../ref/college_mapping.csv](../ref/college_mapping.csv) | 系所代碼 → 學院的人工對照表 |
| [../ref/pricing_table.csv](../ref/pricing_table.csv) | 牌價表，含生效日分段與逐價位的可靠度標記 |

發佈的 csv 都已套用抑制規則：以人為分組單位的比例，在母數不足或單人佔比過高時顯示為空值，並在 `suppression_reason` 欄說明原因，計數則保留；明確登記為非自然人的分組（如服務憑證）只受母數規則約束，單人佔比過高不觸發抑制，理由欄會註明「非自然人分組」。原始日誌、parquet 與帳號對照表含個資，不在 repo 裡。
