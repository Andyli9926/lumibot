from datetime import datetime, timezone

import pandas as pd
import pytest

from lumibot.components.vti_sgov.paper import initialize, check, replay_exercise
from lumibot.components.vti_sgov.execution import load_private
from tests.test_vti_sgov_research import frames


def test_paper_starts_in_future_and_check_is_restart_idempotent(tmp_path):
    path = tmp_path / "paper.json"
    now = datetime(2025, 1, 6, 21, 30, tzinfo=timezone.utc)
    initial = initialize(path, now, 10000)
    assert initial["start"] == "2025-01-07"
    result = check(path, frames(), datetime(2025, 1, 8, 15, tzinfo=timezone.utc))
    assert result["last_session"] == "2025-01-07"  # today's bar incomplete
    saved = load_private(path)
    assert saved["ledger"]["lots"]["VTI"] == 0
    assert saved["ledger"]["lots"]["SGOV"] > 0
    result = check(path, frames(), datetime(2025, 1, 8, 15, tzinfo=timezone.utc))
    assert result["new_sessions"] == 0
    assert load_private(path)["trades"] == saved["trades"]
    with pytest.raises(ValueError, match="exists"):
        initialize(path, now, 10000)


def test_paper_missing_data_preserves_existing_ledger(tmp_path):
    path = tmp_path / "paper.json"
    initialize(path, datetime(2025, 1, 6, 22, tzinfo=timezone.utc))
    original = path.read_bytes()
    data = frames()
    data["VTI"] = data["VTI"].drop(pd.Timestamp("2025-01-07"))
    with pytest.raises(ValueError, match="Missing"):
        check(path, data, datetime(2025, 1, 8, 22, tzinfo=timezone.utc))
    assert path.read_bytes() == original


def test_complete_synthetic_buy_partial_sale_settlement_exit_exercise():
    proof = replay_exercise()
    assert proof["duplicate_fill_ignored"]
    assert proof["unsettled_purchase_blocked"]
    assert proof["final_lots"]["VTI"] == 0
    assert proof["final_lots"]["SGOV"] > 0
