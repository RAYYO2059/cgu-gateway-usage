"""aggregate.build_concentration() 的測試。

這個函式算出來的每一格決定下游哪些數字會被抑制。改壞了不會報錯，只會讓本該
抑制的數字流出去，或反過來把該出的數字擋掉——兩種都不會有人立刻發現。

測試用小型合成資料，不讀 ref/ 或 data/ 底下任何東西，clone 下來即可執行。

**這些測試鎖住的是現有行為，不是「應該有的行為」。** 門檻的比較運算子尤其
如此：`below_min_group_size` 用嚴格小於、`dominant` 用嚴格大於，所以正好等於
門檻的組**不會**被標記。要改門檻語意就得同時改這裡，那正是重點——讓改動必須
是刻意的。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import aggregate, config  # noqa: E402


def make_frame(rows: list[tuple[str, str, int]], user_key: str = "username"):
    """rows = [(使用者, client_type, tokens), ...]，每列一個請求。"""
    frame = pd.DataFrame({
        user_key: [r[0] for r in rows],
        "client_type": [r[1] for r in rows],
        "total_tokens": [r[2] for r in rows],
        "request_id": [f"req{i:04d}" for i in range(len(rows))],
    })
    return frame


def make_registry(users: dict[str, str], user_key: str = "username"):
    """users = {使用者: account_type}。"""
    return pd.DataFrame({
        user_key: list(users),
        "account_type": list(users.values()),
    })


# --- user_key 參數 -----------------------------------------------------------
def test_預設的user_key是username():
    frame = make_frame([("u1", "codex", 10), ("u2", "codex", 10)])
    registry = make_registry({"u1": "student", "u2": "staff"})
    out = aggregate.build_concentration(frame, registry,
                                        dimensions=("account_type",))
    assert set(out["分組值"]) == {"student", "staff"}
    assert out["n_users"].tolist() == [1, 1]


def test_傳入不同的user_key時分組正確():
    # lite 那條線的人層級鍵是 anonymous_user_id。
    frame = make_frame([("a1", "codex", 10), ("a1", "codex", 10),
                        ("a2", "codex", 10)], user_key="anonymous_user_id")
    registry = make_registry({"a1": "student", "a2": "student"},
                             user_key="anonymous_user_id")
    out = aggregate.build_concentration(
        frame, registry, user_key="anonymous_user_id",
        dimensions=("account_type",))
    assert len(out) == 1
    row = out.iloc[0]
    assert row["分組值"] == "student"
    assert row["n_users"] == 2        # 兩個 uid，不是三個請求
    assert row["n_requests"] == 3


def test_換user_key之後不再認得username欄():
    # 防呆：user_key 傳錯時應該炸，不該安靜地算出一個看起來合理的結果。
    frame = make_frame([("u1", "codex", 10)])
    registry = make_registry({"u1": "student"})
    try:
        aggregate.build_concentration(frame, registry,
                                      user_key="anonymous_user_id",
                                      dimensions=("account_type",))
    except KeyError:
        pass
    else:
        raise AssertionError("user_key 指向不存在的欄位時應該拋 KeyError")


# --- dimensions 參數 ---------------------------------------------------------
def test_傳入自訂dimensions時只算那些維度():
    frame = make_frame([("u1", "codex", 10), ("u2", "direct", 10)])
    registry = make_registry({"u1": "student", "u2": "staff"})
    out = aggregate.build_concentration(frame, registry,
                                        dimensions=("client_type",))
    assert set(out["維度"]) == {"client_type"}
    assert set(out["分組值"]) == {"codex", "direct"}


def test_預設dimensions維持現有的五個():
    # 這條鎖住模組層級常數：registry.apply_suppression 與 render_index 都讀它，
    # 少一個維度就等於少一組抑制規則。
    assert aggregate.CONCENTRATION_DIMENSIONS == (
        "account_type", "degree", "entry_year", "dept_code", "client_type")


# --- below_min_group_size ----------------------------------------------------
def test_人數低於門檻標記below_min_group_size():
    users = {f"u{i}": "student" for i in range(config.MIN_GROUP_SIZE - 1)}
    frame = make_frame([(u, "codex", 10) for u in users])
    out = aggregate.build_concentration(frame, make_registry(users),
                                        dimensions=("account_type",))
    row = out.iloc[0]
    assert row["n_users"] == config.MIN_GROUP_SIZE - 1
    assert bool(row["below_min_group_size"]) is True


def test_人數正好等於門檻不標記():
    # 現有行為是 n_users < MIN_GROUP_SIZE，所以「正好等於」不算過小。
    users = {f"u{i}": "student" for i in range(config.MIN_GROUP_SIZE)}
    frame = make_frame([(u, "codex", 10) for u in users])
    out = aggregate.build_concentration(frame, make_registry(users),
                                        dimensions=("account_type",))
    row = out.iloc[0]
    assert row["n_users"] == config.MIN_GROUP_SIZE
    assert bool(row["below_min_group_size"]) is False


# --- dominant ----------------------------------------------------------------
def test_單人佔比超過門檻標記dominant():
    # u1 佔 4/10 = 40% > 30%
    rows = [("u1", "codex", 1)] * 4 + [(f"u{i}", "codex", 1) for i in range(2, 8)]
    users = {u: "student" for u, _, _ in rows}
    out = aggregate.build_concentration(make_frame(rows), make_registry(users),
                                        dimensions=("account_type",))
    row = out.iloc[0]
    assert row["top1_user_share"] == 0.4
    assert bool(row["dominant"]) is True


def test_單人佔比正好等於門檻不標記():
    # 現有行為是 request_share > DOMINANT_THRESHOLD，所以正好 30% 不算 dominant。
    # 3/10 = 0.3 恰好等於 config.DOMINANT_THRESHOLD。
    rows = [("u1", "codex", 1)] * 3 + [(f"u{i}", "codex", 1) for i in range(2, 9)]
    users = {u: "student" for u, _, _ in rows}
    out = aggregate.build_concentration(make_frame(rows), make_registry(users),
                                        dimensions=("account_type",))
    row = out.iloc[0]
    assert row["top1_user_share"] == config.DOMINANT_THRESHOLD == 0.3
    assert bool(row["dominant"]) is False


def test_單人佔比略低於門檻不標記():
    # 2/10 = 20%
    rows = [("u1", "codex", 1)] * 2 + [(f"u{i}", "codex", 1) for i in range(2, 10)]
    users = {u: "student" for u, _, _ in rows}
    out = aggregate.build_concentration(make_frame(rows), make_registry(users),
                                        dimensions=("account_type",))
    assert bool(out.iloc[0]["dominant"]) is False


def test_dominant只看請求佔比不看token佔比():
    # u1 只有 1/10 的請求但吃掉 90% 的 token：現有行為是**不**標 dominant，
    # token 佔比另外記在 top1_user_token_share 供讀者判斷。
    rows = [("u1", "codex", 900)] + [(f"u{i}", "codex", 10) for i in range(2, 11)]
    users = {u: "student" for u, _, _ in rows}
    out = aggregate.build_concentration(make_frame(rows), make_registry(users),
                                        dimensions=("account_type",))
    row = out.iloc[0]
    assert row["top1_user_share"] == 0.1
    assert row["top1_user_token_share"] > 0.9
    assert bool(row["dominant"]) is False


# --- 輸出形狀 ----------------------------------------------------------------
def test_輸出欄位與順序固定():
    # concentration.csv 的欄位順序是對外契約：下游 registry.apply_suppression
    # 靠欄位名取值，而檔案本身會被逐字比對。
    frame = make_frame([("u1", "codex", 10)])
    out = aggregate.build_concentration(frame, make_registry({"u1": "student"}),
                                        dimensions=("account_type",))
    assert list(out.columns) == [
        "維度", "分組值", "n_users", "n_requests", "n_tokens",
        "top1_user_share", "top1_user_token_share",
        "below_min_group_size", "dominant"]


def test_多維度時依維度與請求數排序():
    rows = [("u1", "codex", 10), ("u2", "direct", 10), ("u3", "direct", 10)]
    users = {"u1": "student", "u2": "staff", "u3": "staff"}
    out = aggregate.build_concentration(make_frame(rows), make_registry(users),
                                        dimensions=("account_type", "client_type"))
    assert out["維度"].tolist() == ["account_type"] * 2 + ["client_type"] * 2
    # 同維度內依 n_requests 由大到小
    for dim in ("account_type", "client_type"):
        part = out[out["維度"] == dim]["n_requests"].tolist()
        assert part == sorted(part, reverse=True)


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
