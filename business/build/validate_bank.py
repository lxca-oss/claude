"""Validate a Drillion question bank and simulate the game's answer matcher.

Usage: python3 validate_bank.py bank.json [--try "prompt-id" "typed answer" ...]
The matcher here mirrors the one in the game exactly (normalise -> tokens -> fuzzy match).
"""
import json, re, sys

STOP = set("a an the of to and or in for with on by is are be its it their as at from".split())

def stem(t):
    if len(t) > 4 and t.endswith("ies"): return t[:-3] + "y"
    if len(t) > 3 and t.endswith("s") and not t.endswith("ss"): return t[:-1]
    return t

def norm(s):
    s = s.lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9%]+", " ", s)
    return [stem(t) for t in s.split() if t not in STOP]

def lev(a, b):
    if a == b: return 0
    if not a: return len(b)
    if not b: return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]

def allow(n):
    return 0 if n < 5 else 1 if n < 9 else 2 if n < 15 else 3

NEG = ("non", "un", "in", "im", "ir", "il", "dis")

def neg_pair(a, b):
    """True when one word is the other with a negating prefix (tangible / intangible)."""
    return any(a == p + b or b == p + a for p in NEG)

ROLE = {"ee", "er", "or"}

def role_pair(a, b):
    """True for employee / employer, trainee / trainer, lessee / lessor."""
    return len(a) > 4 and len(b) > 4 and a[:-2] == b[:-2] and a[-2:] != b[-2:] and {a[-2:], b[-2:]} <= ROLE

def tok_eq(a, b):
    if a == b: return True
    if neg_pair(a, b) or role_pair(a, b): return False
    m = min(len(a), len(b))
    if m >= 5 and lev(a, b) <= (2 if m >= 9 else 1): return True
    return False

def match_alias(inp, al):
    """Return set of input token indices used, or None."""
    if not al: return None
    ji, ja = " ".join(inp), " ".join(al)
    clash = any(neg_pair(x, y) or role_pair(x, y) for x in inp for y in al)
    # Whole-phrase typo tolerance only for run-together or split words ("staffturnover"); with equal word
    # counts each word must match on its own, so "gp ratio" never passes for "np ratio".
    if ji and len(inp) != len(al) and not clash and lev(ji, ja) <= allow(len(ja)): return set(range(len(inp)))
    used = set()
    for t in al:
        hit = next((i for i, x in enumerate(inp) if i not in used and tok_eq(x, t)), None)
        if hit is None: return None
        used.add(hit)
    return used

def aliases(ans):
    return [ans["a"]] + list(ans.get("k", []))

def best_match(text, answers):
    inp = norm(text)
    hits = []
    for idx, ans in enumerate(answers):
        best = None
        for al in aliases(ans):
            at = norm(al)
            u = match_alias(inp, at)
            if u is not None:
                spec = sum(len(t) for t in at)
                if best is None or spec > best[0]: best = (spec, u)
        if best: hits.append((best[0], idx, best[1]))
    hits.sort(key=lambda h: -h[0])
    return hits

def resolve(text, answers):
    hits = best_match(text, answers)
    if not hits: return None, False
    top = hits[0]
    multi = [h for h in hits[1:] if not h[2] <= top[2]]
    if multi:
        low = min([top] + multi, key=lambda h: answers[h[1]]["p"])
        return low[1], True
    return top[1], False

def main():
    path = sys.argv[1]
    bank = json.load(open(path))
    errs, warns = [], []
    prompts = bank["prompts"]
    ids = set()
    secs = {}
    for p in prompts:
        pid = p.get("id", "?")
        if pid in ids: errs.append(f"{pid}: duplicate id")
        ids.add(pid)
        for f in ("id", "sec", "q", "answers", "learn"):
            if f not in p: errs.append(f"{pid}: missing {f}")
        if p.get("sec") not in list("RIPSE"): errs.append(f"{pid}: sec must be R/I/P/S/E")
        secs[p.get("sec")] = secs.get(p.get("sec"), 0) + 1
        A = p.get("answers", [])
        if len(A) < 4: errs.append(f"{pid}: needs >= 4 answers (has {len(A)})")
        pts = [a.get("p", 0) for a in A]
        if any(not isinstance(x, int) or x < 5 or x > 100 or x % 5 for x in pts): errs.append(f"{pid}: points must be ints 5..100 in steps of 5")
        if pts and max(pts) < 70: warns.append(f"{pid}: no rare answer (max {max(pts)})")
        if pts and min(pts) > 25: warns.append(f"{pid}: no common answer (min {min(pts)})")
        for a in A:
            if "why" not in a: warns.append(f"{pid}/{a.get('a')}: missing why")
            for al in aliases(a):
                at = norm(al)
                if not at: errs.append(f"{pid}/{a['a']}: alias '{al}' normalises to nothing")
                elif len(at) == 1 and len(at[0]) < 2: errs.append(f"{pid}/{a['a']}: alias '{al}' too short")
        # every alias must resolve to its own answer
        for i, a in enumerate(A):
            for al in aliases(a):
                got, multi = resolve(al, A)
                if got != i:
                    who = A[got]["a"] if got is not None else None
                    errs.append(f"{pid}: alias '{al}' of '{a['a']}' resolves to {who!r}{' (multi)' if multi else ''}")
        for n in p.get("near", []):
            for al in n.get("k", []):
                got, _ = resolve(al, A)
                if got is not None: errs.append(f"{pid}: near alias '{al}' collides with answer '{A[got]['a']}'")
    print(f"{path}: {len(prompts)} prompts, sections {secs}")
    print(f"answers per prompt: min {min(len(p['answers']) for p in prompts)}, avg {sum(len(p['answers']) for p in prompts)/len(prompts):.1f}")
    for w in warns: print("WARN", w)
    for e in errs: print("ERROR", e)
    print("OK" if not errs else f"{len(errs)} errors")
    args = sys.argv[2:]
    if args and args[0] == "--try":
        byid = {p["id"]: p for p in prompts}
        rest = args[1:]
        for k in range(0, len(rest) - 1, 2):
            p = byid[rest[k]]
            got, multi = resolve(rest[k + 1], p["answers"])
            print(f"try [{rest[k]}] {rest[k+1]!r} -> {p['answers'][got]['a'] if got is not None else None}{' (multi)' if multi else ''}")

if __name__ == "__main__":
    main()
