# TriageKit 時間軸遷移

TriageKit 是鑑識分流工具包。`triagekit/parsers/` 裡有 20 個 parser，每個解析一種證據（`evidence/` 裡有樣本）。
它們都用已淘汰的 `triagekit.legacy.emit()`。請把它們全部遷移到新的時間軸格式 `triagekit.events.Event`。

## 新格式要求
- `parse(lines)` 回傳 `list[Event]`，每一行證據對應一個 Event。
- `Event(ts_utc=..., host=..., account=..., action=..., source=...)`
- `ts_utc`：帶時區的 UTC `datetime`（依 docstring 說明的時間格式解析）。
- `source`：parser 編號，例如 `"p03"`。
- host、account、action 取自證據行。

## 工作方式
- 依序處理 p01 到 p20：讀 parser → 改寫成新格式 → 執行該 parser 的測試直到通過 → 下一個。
- 執行測試（把 p03 換成對應編號）：
  `& "$env:HARNESS_PYTHON" -m pytest -q -p no:cacheprovider --tb=short tests/test_p03.py`
- 不要修改 `tests/` 與 `evidence/`。
- 測試失敗訊息可能揭露證據資料的鑑識規則。這些規則適用於**所有** parser，之後的 parser 也必須套用，即使它們的測試沒有檢查。
- 全部完成後，簡短回報結果。
