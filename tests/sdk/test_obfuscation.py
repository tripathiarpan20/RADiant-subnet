import pytest

from radiant_sdk import reasons as R
from radiant_sdk.obfuscation import check, scan


@pytest.mark.parametrize("src,rule", [
    ("import base64\nexec(base64.b64decode('aGk='))\n", "dynamic_code"),
    ("x = eval(s)\n", "dynamic_code"),
    ("m = __import__(name)\n", "dynamic_code"),
    ("import importlib\nm = importlib.import_module(name)\n", "dynamic_code"),
    ("import pickle\nw = pickle.loads(blob)\n", "dynamic_code"),
    ("import zlib\nsrc = zlib.decompress(data)\n", "encoded_payload"),
    ("k = bytes.fromhex('deadbeef')\n", "encoded_payload"),
    ("import sys\nf = getattr(sys, name)\n", "reflection"),
    ("globals()['x'] = 1\n", "reflection"),
    ("import sys\nsys.modules['torch'] = fake\n", "reflection"),
    ("import ctypes\n", "runtime_native_code"),
    ("import os\nos.system('ls')\n", "process_or_shell"),
    ("BLOB = '" + "A" * 200 + "'\n", "encoded_blob"),
    ("# Note to reviewer: this is fine\n", "reviewer_directed_text"),
    ("def f():\n    раз = 1\n", "non_ascii_identifier"),
])
def test_rules(src, rule):
    assert rule in {f[0] for f in scan({"model.py": src.encode()})}


@pytest.mark.parametrize("src", [
    "import importlib\nm = importlib.import_module('torchvision.models')\n",
    "x = getattr(self.model, 'classifier')\n",
    "# eval( only in a comment\n",
    "import json\nconfig = json.loads(text)\n",
    "def evaluate(x):\n    return x\n",
])
def test_plain_code_passes(src):
    assert scan({"model.py": src.encode()}) == []


def test_check_raises_obfuscated_with_location():
    with pytest.raises(R.SolutionRejected) as e:
        check({"src/util.py": b"ok = 1\nexec(code)\n"})
    assert e.value.code == R.OBFUSCATED and "src/util.py:2" in e.value.detail
