"""套用四條前置規則，回報命中量與剩餘母數。不呼叫任何分類器。

四條規則決定分類母數。**母數錯了，後面每一個比例都跟著錯，而且不會有
任何檢查抓得到**——分類器照跑、佔比照算，只是分母不對。所以這一支的
輸出要能被驗算：四項相加必須正好等於被排除的總量。

    規則 1  向量化        model_returned 開頭為 EMBEDDING_MODELS
    規則 2  影像生成      model_returned 開頭為 IMAGE_MODELS
    規則 3  工具自動發出  請求文字含客戶端框架標記（六種）
    規則 4  流程中繼處理  請求文字含 ollama 分類器的外框（兩種）

規則 1、2 用 **model_returned.startswith()** 不是 endpoint：兩者的結果不同，
而且不是小差異。endpoint 判定會把 bge-m3（本地向量模型，名字裡沒有 embed）
算漏、把 chatgpt-image-latest 多算進來。實測 endpoint 版是 586/397，
模型名版是 587/401——差異雖小，但它是母數定義，不能靠「差不多」。

**指派在 sha256 層不在請求層。** 同一段內容可能被不同模型送過，逐請求指派
再逐組去重的話，那個 sha256 會在兩組各被數一次，四項相加就會比實際排除量
多幾個（實測多 3）。這張表是分類母數的定義，讀者要能用它驗算 17,365——
能在資料層解決的不要留給文字說明。跨規則的 sha256 數另外報，現象仍然看得見。
"""

from __future__ import annotations

import argparse
import logging

import pandas as pd

from src import config, extract_lite, schema_lite
from src.classify_lite import markers as markers_mod

logger = logging.getLogger(__name__)

MIN_LEN = markers_mod.MIN_LEN

# 用 startswith 不是 in：`in` 會讓任何含這些字的模型名都命中，
# 而模型名是上游給的字串，我們控制不了它未來長什麼樣。
EMBEDDING_MODELS = ("text-embedding-", "bge-m3")
IMAGE_MODELS = ("gpt-image-",)

# 指派的優先序：數字小的贏。一段內容同時命中多條時要有唯一歸屬，
# 否則四項相加不等於排除總量。順序沿用當初那支腳本（4→3→2→1 覆蓋，
# 等價於這裡的 1→2→3→4 優先）。
RULE_LABELS = {
    1: "1 向量化",
    2: "2 影像生成",
    3: "3 工具自動發出",
    4: "4 流程中繼處理",
}
UNASSIGNED = "需送分類器"


def _rule_of_column(column: str) -> int | None:
    """旗標欄名 → 規則編號。exclude_* 不是規則，回 None。

    欄名要求 `r<數字>_` 的完整形狀，不是 `startswith("tool_")` 那種前綴掃描：
    命名慣例沒有型別保證，同一個前綴底下混進一個整數欄，`.sum()` 加的就會是
    數值而不是筆數，而且完全不會報錯。這正是上一版踩過的坑，成因與代價見
    ENGINEERING_NOTES〈子集的筆數不得超過母體——量級大時自證，量級小時隱形〉。
    """
    if column.startswith("exclude_"):
        return None
    head = column.split("_", 1)[0]
    return int(head[1:]) if head.startswith("r") and head[1:].isdigit() else None


def assign_rules(per_sha: pd.DataFrame, flag_columns: list[str]) -> pd.DataFrame:
    """把 r3/r4/n_rules_hit/rule 四欄加上去。

    **純函式，不讀任何檔案**——所以在只有 repo、沒有資料的環境裡也測得動。
    這條規則沿用 college.validate_mapping()：指派邏輯對不對是它自己的事，
    不該因為本機剛好沒有那 680 MB 原始資料就驗不了。

    輸入的 per_sha 每列是一個相異內容，已備妥 r1／r2 與各標記旗標欄。
    """
    per_sha = per_sha.copy()
    for number in (3, 4):
        columns = [c for c in flag_columns if _rule_of_column(c) == number]
        per_sha[f"r{number}"] = (per_sha[columns].any(axis=1) if columns
                                 else False)

    per_sha["n_rules_hit"] = per_sha[[f"r{n}" for n in RULE_LABELS]].sum(axis=1)
    # 由後往前覆蓋，等價於「編號小的優先」。一段內容同時命中多條時要有唯一
    # 歸屬，否則四項相加不等於排除總量，這張表就沒辦法拿來驗算母數。
    per_sha["rule"] = UNASSIGNED
    for number in sorted(RULE_LABELS, reverse=True):
        per_sha.loc[per_sha[f"r{number}"], "rule"] = RULE_LABELS[number]
    return per_sha


def check_partition(summary: pd.DataFrame, n_contents: int,
                    n_requests: int) -> None:
    """五組必須是母體的互斥分割：兩欄的總和都要正好等於母體。

    **這條檢查的存在理由，正是「不需要它」那個論證。** 互斥是 assign_rules()
    的設計意圖，這裡驗的是這一次的輸出確實符合——意圖與事實是兩件事，而
    結構性保證的前提是「指派邏輯永遠正確」，那正是會出錯的地方。

    這個階段被同一類錯誤咬過兩次：一次算出 383,678 筆（超過母體 106,993，
    一眼看得出來），一次只多算 3（分類母數 17,368，看起來完全合理）。
    見 ENGINEERING_NOTES〈子集的筆數不得超過母體——量級大時自證，量級小時
    隱形〉與〈指派層級與去重層級不一致，會讓分組計數多算〉。**第二次是靠「四項
    相加不等於總數」這個矛盾發現的，而那就是這條檢查在驗的東西。**

    純函式，不讀檔——所以「它真的會擋下來」這件事測得到。
    """
    for column, total in (("相異內容", n_contents), ("請求數", n_requests)):
        problems = schema_lite.check_subset_counts(
            total, dict(zip(summary["rule"], summary[column])),
            label=f"前置規則（{column}）", exclusive=True)
        if problems:
            raise AssertionError(
                "前置規則的分組計數不健全：" + "；".join(problems)
                + "。五組是互斥分割，總和必須等於母體——不相等代表指派層級"
                  "與去重層級不一致，或有內容被指到兩組。"
            )


def build(marks: pd.DataFrame | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """回傳 (逐 sha256 的規則指派, 逐規則的彙總)。"""
    if marks is None:
        if not markers_mod.MARKERS_PATH.exists():
            raise FileNotFoundError(
                f"找不到 {markers_mod.MARKERS_PATH}，"
                "請先執行 python -m src.classify_lite.markers"
            )
        marks = pd.read_parquet(markers_mod.MARKERS_PATH)

    frame = extract_lite.load_dataset()
    sub = frame[frame["prompt_text_sha256"].notna()
                & (frame["prompt_text_len"] >= MIN_LEN)].copy()

    model = sub["model_returned"].fillna("")
    sub["r1"] = model.str.startswith(EMBEDDING_MODELS)
    sub["r2"] = model.str.startswith(IMAGE_MODELS)

    # 逐 sha256 收斂：規則 1、2 看模型（逐請求可能不同），規則 3、4 看內容
    # （逐 sha256 固定）。前兩者用 any()——同一段內容只要有一次是用向量模型
    # 送的，它就是向量化用途的內容。
    per_sha = sub.groupby("prompt_text_sha256").agg(
        n_requests=("request_id", "size"),
        r1=("r1", "any"),
        r2=("r2", "any"),
    ).reset_index()

    flag_columns = [c for c in marks.columns if c != "prompt_text_sha256"]
    per_sha = per_sha.merge(marks, on="prompt_text_sha256", how="left")
    for column in flag_columns:
        per_sha[column] = per_sha[column].fillna(False).astype(bool)
    per_sha = assign_rules(per_sha, flag_columns)

    summary = (per_sha.groupby("rule")
               .agg(相異內容=("prompt_text_sha256", "size"),
                    請求數=("n_requests", "sum"))
               .sort_index()
               .reset_index())
    markers_mod.check_columns(per_sha)

    check_partition(summary, len(per_sha), int(per_sha["n_requests"].sum()))
    return per_sha, summary


def run(run_id: str | None = None) -> int:
    per_sha, summary = build()
    total_sha = len(per_sha)
    total_req = int(per_sha["n_requests"].sum())
    rest = per_sha[per_sha["rule"] == UNASSIGNED]
    removed = total_sha - len(rest)
    multi = int((per_sha["n_rules_hit"] > 1).sum())

    print("=" * 72)
    print("四條前置規則（指派在 sha256 層，四項相加即排除總量）")
    print("=" * 72)
    print(summary.to_string(index=False,
                            formatters={"相異內容": "{:,}".format,
                                        "請求數": "{:,}".format}))
    print()
    print(f"  母數          {total_sha:,} 個相異內容 / {total_req:,} 筆請求")
    print(f"  四項排除      {removed:,} 個相異內容"
          f"（{removed / total_sha:.1%}）")
    print(f"  剩餘需分類    {len(rest):,} 個相異內容 /"
          f" {int(rest['n_requests'].sum()):,} 筆請求")
    print(f"  命中多於一條規則的相異內容：{multi}"
          "（已指派到優先序最高的規則，不重複計數）")

    if run_id:
        out_dir = config.RUNS_DIR / run_id / "classify_lite"
        out_dir.mkdir(parents=True, exist_ok=True)
        summary.to_csv(out_dir / "prefilter_counts.csv",
                       index=False, encoding="utf-8-sig")
        keep = ["prompt_text_sha256", "rule", "n_rules_hit", "n_requests"]
        markers_mod.check_columns(per_sha[keep])
        per_sha[keep].to_parquet(out_dir / "prefilter_hits.parquet", index=False)
        logger.info("→ %s", out_dir)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="套用四條前置規則（只讀 markers 與已抽取的 parquet，秒級）")
    parser.add_argument("--run-id", default=None,
                        help="給了就把計數寫進 runs/<run_id>/classify_lite/")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S")
    return run(run_id=args.run_id)


if __name__ == "__main__":
    raise SystemExit(main())
