ERROR: rate limited on key #2

import json
with open("code_text.txt", "r") as f:
    _code_text = f.read()
try:
    _ok, _reason = precheck_error:_rate_limited_on_key_#2(_code_text)
    print(json.dumps({"ok": bool(_ok), "reason": str(_reason)}))
except Exception as e:
    print(json.dumps({"ok": True, "reason": "precheck errored: " + str(e)}))
