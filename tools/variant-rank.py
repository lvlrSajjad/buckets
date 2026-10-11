import json, subprocess, sys, difflib, os
ts, decls, S = sys.argv[1:4]
A, B = (sys.argv[4:6] + ["app", "app-pro"])[:2]  # the two variant folder names
for d in sorted(os.listdir(S)):
    a_dir, b_dir = f"{S}/{d}/{A}", f"{S}/{d}/{B}"
    if not (os.path.isdir(a_dir) and os.path.isdir(b_dir)): continue
    pairs = [f for f in os.listdir(a_dir) if f.endswith((".ts", ".tsx")) and os.path.exists(f"{b_dir}/{f}")]
    tot = {"same": 0, "near": 0, "diff": 0}; only = 0; size = 0
    for f in pairs:
        a, b = f"{a_dir}/{f}", f"{b_dir}/{f}"
        out = json.loads(subprocess.run(["node", decls, ts, a, b], capture_output=True, text=True, check=True).stdout)
        # leaves: declarations with no named declaration inside them (no double counting)
        def leaves(lst):
            return {x["name"]: x for x in lst if not any(y is not x and y["lo"] >= x["lo"] and y["hi"] <= x["hi"] and y["depth"] > x["depth"] for y in lst)}
        A, B = leaves(out[a]), leaves(out[b])
        for n in A.keys() & B.keys():
            r = 1.0 if A[n]["text"] == B[n]["text"] else difflib.SequenceMatcher(None, A[n]["text"], B[n]["text"], autojunk=False).ratio()
            tot["same" if r == 1 else "near" if r >= .85 else "diff"] += A[n]["hi"] - A[n]["lo"] + 1
        only += sum(x["hi"] - x["lo"] + 1 for n, x in A.items() if n not in B) + sum(x["hi"] - x["lo"] + 1 for n, x in B.items() if n not in A)
        size += sum(1 for _ in open(a)) + sum(1 for _ in open(b))
    unpaired = len([f for f in os.listdir(a_dir) if f.endswith(("ts","tsx"))]) - len(pairs)
    print(f"{d:32} {size:5} lines | in matching declarations: same {tot['same']:4} near {tot['near']:4} diff {tot['diff']:4} | one side only {only:4} | unpaired files {unpaired}")
