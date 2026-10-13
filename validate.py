#!/usr/bin/env python3
"""Sanity-check parsed cause lists: print items with missing case no. or
parties, and item numbers that are not 1,2,3… within a list."""
import json, sys, glob, re
bad = 0
for f in sorted(glob.glob(sys.argv[1] if len(sys.argv) > 1 else "data/2*.json")):
    d = json.load(open(f))
    for b in d["benches"]:
        # Numbering must run 1,2,3… inside a list. It may restart (a second bench panel in
        # the same list) or start above 1 (a supplementary list continues the main one),
        # but a gap or a repeat inside a run is a parse error.
        nums = [it["n"] for s in b["sections"] for it in s["items"]]
        ints = [int(n.split(".")[0]) for n in nums if "." not in n]
        odd = [ints[i] for i in range(1, len(ints)) if ints[i] not in (ints[i-1], ints[i-1] + 1, 1)]
        miss = [(it["n"], "case" if not it["c"] else "party") for s in b["sections"] for it in s["items"] if not it["c"] or not it["p"]]
        if odd or miss or not nums:
            bad += 1
            print(f"{d['date']} {b['list']}: items={len(nums)} odd={odd[:6]} missing={miss[:6]}")
print("lists with problems:", bad)
