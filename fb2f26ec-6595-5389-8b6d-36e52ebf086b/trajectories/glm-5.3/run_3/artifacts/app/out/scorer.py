"""Relay batch scorer - the settlements as recovered from what the archive left.

Usage:  python3 scorer.py BATCH_JSON BOARD_JSON

The settlements this scorer runs under (the sections the surviving notes lost):

  section 3   winning class on a tie for the largest logit ... the lowest class id
  section 4   abstain floor applied ..................... before the item is routed
  section 5   the relation the routing drives from ....... routes (probe: items)
  section 6   a bucket with no surviving item ............ still reports
  section 7   mean = total / n as a whole number ......... round half to even
  section 8   confidence .................................. sum of the item margins
  section 9   a bucket with a blank mean sorts ........... first
              equal-mean buckets order .................... by descending join id

Where each reading comes from.  The scorer shipped beside the notes still runs and
still carries wrong settlements; the notes say so but not which, or how many.  Two
of the wrong ones are readable straight off it, because the true settlement was left
behind where the code shape could hold it: half_even() sits defined and uncalled
while floor_div() does the mean, and argmax_logit()'s own tie_low=True default is
overridden by a tie_low = False at the call site.  Those settle sections 7 and 3.
Every other settlement is a flip of what that scorer does - the abstain floor
applied after the join, driving from items, dropping empty buckets, summing winning
logits as confidence, blank means last, equal means by ascending join id - taken on
the reading that the same replacement ran over the whole grid and simply left no
trace where the code was inline.

Whole numbers end to end: every reported value is an integer, compared exactly.
"""

import json
import sys


def half_even(num, den):
    q, r = divmod(num, den)
    twice = 2 * r
    if twice > den or (twice == den and q % 2 != 0):
        q += 1
    return q


def argmax_logit(logits, tie_low=True):
    best_id = None
    best_val = None
    for entry in logits:
        cid, val = entry["class"], entry["logit"]
        if best_val is None or val > best_val:
            best_id, best_val = cid, val
        elif val == best_val:
            if tie_low and cid < best_id:
                best_id = cid
            if not tie_low and cid > best_id:
                best_id = cid
    return best_id, best_val


def second_best(logits, winner_id):
    best_val = None
    for entry in logits:
        if entry["class"] == winner_id:
            continue
        if best_val is None or entry["logit"] > best_val:
            best_val = entry["logit"]
    return best_val


def score_batch(batch, tables):
    items = list(tables[batch["items"]])
    routes = list(tables[batch["routes"]])

    drive, probe = "routes", "items"

    floor = batch["floor"]
    tie_low = True

    resolved = []
    for row in items:
        cid, top = argmax_logit(row["logits"], tie_low)
        runner = second_best(row["logits"], cid)
        margin = top - runner if runner is not None else top
        resolved.append({"item_id": row["item_id"], "winner": cid,
                         "top": top, "margin": margin})

    # section 4: the floor is applied before the item is routed to its bucket
    resolved = [r for r in resolved if r["top"] >= floor]

    route_of = {}
    for r in routes:
        route_of.setdefault(r["winner"], r["bucket"])

    joined = []
    for r in resolved:
        if r["winner"] in route_of:
            merged = dict(r)
            merged["bucket"] = route_of[r["winner"]]
            merged["_join_id"] = r["item_id"] * 1000 + r["winner"]
            joined.append(merged)

    # section 6: every bucket the routes name reports, empty ones included
    keys = []
    for r in routes:
        if r["bucket"] not in keys:
            keys.append(r["bucket"])
    groups = {k: [] for k in keys}
    for row in joined:
        groups.setdefault(row["bucket"], []).append(row)

    out = []
    for key in keys:
        members = groups[key]
        margins = [m["margin"] for m in members]
        total = sum(margins)
        mean = half_even(total, len(margins)) if margins else None
        ids = [m["_join_id"] for m in members]
        confidence = sum(m["margin"] for m in members)
        out.append({"key": key, "n": len(members), "total": total,
                    "mean": mean, "top_row": max(ids) if ids else None,
                    "confidence": confidence})

    # section 9: order by mean, blank means first, equal means by descending join id
    out.sort(key=lambda e: (e["top_row"] if e["top_row"] is not None else -1), reverse=True)
    out.sort(key=lambda e: (0 if e["mean"] is None else 1,
                            e["mean"] if e["mean"] is not None else 0))

    plan = {"drive": drive, "probe": probe, "abstain": "before_route",
            "empty_bucket": "include", "null_order": "nulls_first",
            "tie_order": "by_join_id_desc", "confidence": "margin_sum",
            "buckets": len(out)}
    return plan, out


def main(argv):
    if len(argv) != 3:
        sys.stderr.write("usage: scorer.py BATCH_JSON BOARD_JSON\n")
        return 2
    with open(argv[1], encoding="utf-8") as handle:
        document = json.load(handle)
    plan, rows = score_batch(document["batch"], document["tables"])
    with open(argv[2], "w", encoding="utf-8") as handle:
        json.dump({"batch": document["batch"]["name"], "plan": plan, "rows": rows},
                  handle, indent=2, sort_keys=True)
        handle.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
