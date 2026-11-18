"""Import a skill script by its folder name, the way the skills run them side by side."""
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "tests", "fixtures", "india")
sys.path.insert(0, os.path.join(ROOT, "trader-kite"))


def load(folder, script):
    path = os.path.join(ROOT, folder, script)
    name = "%s_%s" % (folder.replace("-", "_"), script[:-3])
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def instruments(today=None):
    import datetime
    import kite
    texts = {}
    for ex in ("NSE", "NFO"):
        with open(os.path.join(FIX, "instruments-%s.csv" % ex), encoding="utf-8") as fh:
            texts[ex] = fh.read()
    return kite.Instruments(("NSE", "NFO"), texts=texts, today=today or datetime.date(2026, 11, 18))
