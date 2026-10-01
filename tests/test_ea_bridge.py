from pathlib import Path
import csv
import time

import pytest

from fusion.config import DEFAULTS
from fusion import ea


def test_generated_source_uses_validated_parameters_and_no_order_calls(tmp_path):
    result=ea.generate_companion({**DEFAULTS["strategy"],"fast_ema":9,"stop_atr":2.5},DEFAULTS["workflow"],tmp_path)
    text=Path(result["path"]).read_text(encoding="utf-8")
    assert "input int FastEMA = 9;" in text and "input double StopATR = 2.5;" in text
    assert "OrderSend(" not in text and "WebRequest(" not in text and "#import" not in text
    with pytest.raises(ValueError): ea.generate_companion({**DEFAULTS["strategy"],"symbol":'XAUUSD"; OrderSend('},DEFAULTS["workflow"],tmp_path)


def test_calendar_uses_ea_explicit_utc_not_quote_time_correction(tmp_path,monkeypatch):
    monkeypatch.setattr(ea,"bridge_path",lambda _:tmp_path)
    monkeypatch.setattr(ea,"telemetry",lambda _:{"connected":True})
    with (tmp_path/"calendar.csv").open("w",encoding="utf-8",newline="") as f:
        writer=csv.DictWriter(f,fieldnames=["raw_time","time_utc","title","impact","currency"],delimiter=";")
        writer.writeheader();writer.writerow({"raw_time":1800010800,"time_utc":1800000000,"title":"NFP fixture","impact":"high","currency":"USD"})
    class Broker: offset=0
    result=ea.import_calendar(Broker(),DEFAULTS["context"])
    assert result["events"][0]["time_utc"]==1800000000
    monkeypatch.setattr(ea,"telemetry",lambda _:{"connected":False})
    with pytest.raises(ValueError): ea.import_calendar(Broker(),DEFAULTS["context"])
