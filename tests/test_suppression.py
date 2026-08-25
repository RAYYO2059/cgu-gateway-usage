"""registry.apply_suppression() 的維度分派測試。

這裡驗的不是「抑制算得對不對」（那是 concentration 的事），而是**一個維度
會被送進哪條路**：抑制、明確豁免、或未登記。

分派錯了的後果不對稱：把該抑制的送進豁免 → 數字靜默流出，沒有人會發現；
把該豁免的送進抑制 → 數字變 NA，有人會來問。所以預設值必須偏向後者，
而這些測試就是鎖住那個偏向。

合成資料，不讀 ref/ 或 data/，clone 下來即可執行。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import aggregate, config  # noqa: E402
from src.metrics import registry  # noqa: E402


def make_spec(group_by: list[str]) -> registry.MetricSpec:
    return registry.MetricSpec(
        name="t", question="q", unit="request", source="request",
        denominator="d", caveat=None, needs_dedup=False,
        group_by=group_by, version="1.0", fn=lambda tables: None)


def make_result(dimension: str, values: list[str], n_users: bool = True):
    data = pd.DataFrame({
        dimension: values,
        "n_requests": [10] * len(values),
        "request_share": [round(1 / len(values), 4)] * len(values),
    })
    if n_users:
        data["n_users"] = [5] * len(values)
    return registry.MetricResult(data=data, n_total=len(values),
                                 n_covered=len(values))


def make_rules(dimension: str, value: str, *, below=False, dominant=False):
    return pd.DataFrame({
        "維度": [dimension], "分組值": [value],
        "n_users": [3], "top1_user_share": [0.61],
        "below_min_group_size": [below], "dominant": [dominant],
    })


EMPTY_RULES = pd.DataFrame(columns=registry._EMPTY_RULES)


# --- 在 CONCENTRATION_DIMENSIONS → 正常抑制 ---------------------------------
def test_登記為受抑制的維度會被抑制():
    spec = make_spec(["account_type"])
    result = make_result("account_type", ["student", "staff"])
    rules = make_rules("account_type", "student", dominant=True)
    out = registry.apply_suppression(spec, result, rules)

    student = out.data[out.data["account_type"] == "student"].iloc[0]
    staff = out.data[out.data["account_type"] == "staff"].iloc[0]
    assert pd.isna(student["request_share"])       # 被抑制
    assert not pd.isna(staff["request_share"])     # 未觸發
    assert "單人佔" in student[registry.REASON_COLUMN]
    assert len(out.suppressed) == 1


# --- 在 EXEMPT_DIMENSIONS → 標豁免，措辭不變 --------------------------------
def test_登記為豁免的維度標豁免且措辭與現行相同():
    spec = make_spec(["hour_taipei"])
    result = make_result("hour_taipei", ["0", "1"])
    out = registry.apply_suppression(spec, result, EMPTY_RULES)

    assert out.data[registry.REASON_COLUMN].tolist() == [
        registry._EXEMPT_REASON] * 2
    # 這串字會寫進 csv，是對外契約，不可改。
    assert registry._EXEMPT_REASON == "依政策豁免抑制（此維度分的是請求不是人）"
    assert out.data["request_share"].notna().all()   # 豁免 = 不抑制
    assert out.warnings == []


def test_四個豁免維度都登記在案():
    # 這條鎖住盤點結果：clean 現有 19 個指標裡有 4 個靠豁免運作，
    # 少登記任何一個，它的 csv 就會從「標豁免」變成「未登記警告」。
    assert set(aggregate.EXEMPT_DIMENSIONS) == {
        "hour_taipei", "endpoint", "model_family", "reasoning_effort"}


def test_豁免但缺n_users會警告():
    spec = make_spec(["hour_taipei"])
    result = make_result("hour_taipei", ["0"], n_users=False)
    out = registry.apply_suppression(spec, result, EMPTY_RULES)
    assert any("缺少 n_users" in w for w in out.warnings)


# --- 兩份清單都沒有 → 進抑制流程 + 「請登記」警告 ---------------------------
def test_未登記的維度進抑制流程並要求登記():
    spec = make_spec(["unit"])          # lite 未來會用，目前兩份清單都沒有
    result = make_result("unit", ["醫學院", "工學院"])
    out = registry.apply_suppression(spec, result, EMPTY_RULES)

    # 不標豁免——這正是反轉的重點。
    assert registry._EXEMPT_REASON not in out.data[registry.REASON_COLUMN].tolist()
    assert any("請明確登記" in w for w in out.warnings), out.warnings
    assert any("EXEMPT_DIMENSIONS" in w for w in out.warnings)


def test_未登記與已登記的警告可以區分():
    # (b) 未登記：補救動作是「去登記」
    spec_b = make_spec(["unit"])
    out_b = registry.apply_suppression(
        spec_b, make_result("unit", ["醫學院"]),
        make_rules("account_type", "student"))     # 規則表有東西，但沒有 unit
    warn_b = " ".join(out_b.warnings)

    # (a) 已登記卻查無紀錄：補救動作是「重跑 aggregate」
    spec_a = make_spec(["dept_code"])
    out_a = registry.apply_suppression(
        spec_a, make_result("dept_code", ["29"]),
        make_rules("account_type", "student"))     # 規則表有東西，但沒有 dept_code
    warn_a = " ".join(out_a.warnings)

    assert "請重跑 aggregate" in warn_a
    assert "請重跑 aggregate" not in warn_b
    assert "請明確登記" in warn_b
    assert "請明確登記" not in warn_a


# --- 自訂清單（lite 的使用情境）---------------------------------------------
def test_傳入自訂清單時依自訂清單分派():
    spec = make_spec(["college"])
    rules = make_rules("college", "醫學院", below=True)
    out = registry.apply_suppression(
        spec, make_result("college", ["醫學院", "工學院"]), rules,
        dimensions=("college", "unit"), exempt=("request_style",))
    medical = out.data[out.data["college"] == "醫學院"].iloc[0]
    assert pd.isna(medical["request_share"])
    assert f"母數 3 < {config.MIN_GROUP_SIZE}" in medical[registry.REASON_COLUMN]


def test_自訂豁免清單讓維度走豁免():
    spec = make_spec(["request_style"])
    out = registry.apply_suppression(
        spec, make_result("request_style", ["responses", "chat"]), EMPTY_RULES,
        dimensions=("college",), exempt=("request_style",))
    assert out.data[registry.REASON_COLUMN].tolist() == [
        registry._EXEMPT_REASON] * 2
    assert out.warnings == []


def test_自訂清單不影響模組層級常數():
    # 傳參數不該有副作用——clean 的預設行為必須原封不動。
    registry.apply_suppression(
        make_spec(["college"]), make_result("college", ["醫學院"]), EMPTY_RULES,
        dimensions=("college",), exempt=("request_style",))
    assert aggregate.CONCENTRATION_DIMENSIONS == (
        "account_type", "degree", "entry_year", "dept_code", "client_type")
    assert aggregate.EXEMPT_DIMENSIONS == (
        "hour_taipei", "endpoint", "model_family", "reasoning_effort")


def _main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, func in tests:
        try:
            func()
            print(f"ok   {name}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {name}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} 通過")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_main())
