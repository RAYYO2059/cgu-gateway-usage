"""各項使用統計指標。

指標以 ``@metric`` 裝飾器註冊到 ``registry.REGISTRY``，分兩類：

- ``basic``：直接數欄位。
- ``derived``：需要先算一步。

這裡 import 兩個模組是為了讓「載入套件」等同於「完成註冊」——
否則 ``--list`` 會依呼叫路徑不同而列出不一樣的指標。
"""

from src.metrics import basic, derived, registry  # noqa: F401

__all__ = ["basic", "derived", "registry"]
