"""Build Drillion (business/drillion.html) from the game source and the four question banks.

Usage: python3 build_dive.py [banks_dir] [out_file]
Banks are JSON files named operations.json, marketing.json, finance.json and hr.json
(format described in dive/README.md). Every bank is validated before the build.
"""
import json, os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
BANKS = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "dive")
OUT = sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "..", "drillion.html")
TOPICS = ["operations", "marketing", "finance", "hr"]

data = {}
for t in TOPICS:
    path = os.path.join(BANKS, t + ".json")
    res = subprocess.run([sys.executable, os.path.join(HERE, "validate_bank.py"), path], capture_output=True, text=True)
    if "errors" in res.stdout.splitlines()[-1]:
        sys.exit(res.stdout)
    bank = json.load(open(path, encoding="utf-8"))
    # keep only what the game reads
    data[t] = {"prompts": [
        {k: p[k] for k in ("id", "sec", "q", "hint", "learn", "answers", "near") if k in p}
        for p in bank["prompts"]
    ]}

src = open(os.path.join(HERE, "drillion_src.html"), encoding="utf-8").read()
blob = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
html = src.replace("/*__BANKS__*/null", blob)
open(OUT, "w", encoding="utf-8").write(html)
print(f"wrote {OUT}: {len(html)//1024} KB, " + ", ".join(f"{t} {len(data[t]['prompts'])}" for t in TOPICS))
