"""Audit M4: the "read once" ledger of the sealed months lives BESIDE THE DATA and is keyed on the data hash, not in the output folder. A new --out folder or a
deleted output file no longer gives a clean second look. It is an honesty aid, not a lock: anyone who can delete the file next to the data can still do it."""
from __future__ import annotations

import json
import shutil

import pytest
from test_donchian_backtest import write_store as write_don_store
from test_rsi_backtest import write_store as write_rsi_store

from mba_bot import donchian_backtest as DB
from mba_bot import rsi_backtest as RB

RUNNERS = [(RB, "rsi_mr_v1"), (DB, "donchian_breakout_v1")]


@pytest.fixture(scope="module")
def master(tmp_path_factory):
    root = tmp_path_factory.mktemp("master")
    write_rsi_store(root)
    return root


@pytest.fixture
def store(master, tmp_path):
    root = tmp_path / "data"
    shutil.copytree(master, root)
    return root


@pytest.mark.parametrize("mod,name", RUNNERS, ids=["rsi", "donchian"])
def test_a_second_look_into_a_new_output_folder_is_refused_and_a_second_look_flag_is_contaminated(mod, name, store, tmp_path):
    mod.run_tune(store, tmp_path / "out1")
    assert mod.run_confirm(store, tmp_path / "out1", reason="first", confirm=True)["contaminated"] is False
    mod.run_tune(store, tmp_path / "out2")  # a brand new output folder, same data
    with pytest.raises(mod.SealedError, match="already read once"):
        mod.run_confirm(store, tmp_path / "out2", reason="again", confirm=True)
    res = mod.run_confirm(store, tmp_path / "out2", reason="forced", confirm=True, second_look=True)
    assert res["contaminated"] is True and "NOT the first look" in (tmp_path / "out2" / "final_report.md").read_text()


@pytest.mark.parametrize("mod,name", RUNNERS, ids=["rsi", "donchian"])
def test_deleting_output_files_does_not_reset_the_ledger(mod, name, store, tmp_path):
    out = tmp_path / "out"
    mod.run_tune(store, out)
    mod.run_confirm(store, out, reason="first", confirm=True)
    for f in out.glob("*"):
        f.unlink()
    mod.run_tune(store, out)
    with pytest.raises(mod.SealedError, match="already read once"):
        mod.run_confirm(store, out, reason="again", confirm=True)


@pytest.mark.parametrize("mod,name", RUNNERS, ids=["rsi", "donchian"])
def test_the_ledger_is_beside_the_data_keyed_on_the_data_hash_and_not_in_the_output_folder(mod, name, store, tmp_path):
    out = tmp_path / "out"
    mod.run_tune(store, out)
    assert not list(store.glob("*sealed*")) and not (out / "looks.json").exists()  # nothing before the sealed months are opened; tuning writes no ledger
    mod.run_confirm(store, out, reason="first", confirm=True)
    ledgers = list(store.glob("*sealed*"))
    assert len(ledgers) == 1 and name in ledgers[0].name and not (out / "looks.json").exists()
    content = json.loads(ledgers[0].read_text())
    dh = RB.data_hash(store)
    assert list(content) == [dh] and len(content[dh]) == 1 and content[dh][0]["reason"] == "first"


def test_other_data_gets_its_own_clean_ledger_and_the_two_runners_do_not_share_one(store, tmp_path):
    RB.run_tune(store, tmp_path / "r")
    RB.run_confirm(store, tmp_path / "r", reason="rsi", confirm=True)
    DB.run_tune(store, tmp_path / "d")
    assert DB.run_confirm(store, tmp_path / "d", reason="donchian", confirm=True)["contaminated"] is False  # its own registration
    other = tmp_path / "other"
    write_don_store(other, seed=2)
    RB.run_tune(other, tmp_path / "r2")
    assert RB.run_confirm(other, tmp_path / "r2", reason="other data", confirm=True)["contaminated"] is False


@pytest.mark.parametrize("mod,name", RUNNERS, ids=["rsi", "donchian"])
def test_refused_attempts_write_no_ledger(mod, name, store, tmp_path):
    out = tmp_path / "out"
    mod.run_tune(store, out)
    with pytest.raises(mod.SealedError):
        mod.run_confirm(store, out, reason="x", confirm=False)
    with pytest.raises(mod.SealedError):
        mod.run_confirm(store, out, reason="", confirm=True)
    assert not list(store.glob("*sealed*"))


def test_the_pre_registrations_call_it_an_honesty_aid_not_a_lock():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    for doc in ("rsi_mr_preregistration.md", "donchian_preregistration.md"):
        text = (root / "docs" / "research" / doc).read_text()
        assert "honesty aid, not a lock" in text and "beside the data" in text
