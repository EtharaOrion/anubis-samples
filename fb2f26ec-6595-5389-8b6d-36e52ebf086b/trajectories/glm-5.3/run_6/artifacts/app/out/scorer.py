"""The Relay batch-inference scorer, restored to the settlements the archive holds.

Reads a batch document from the first path and writes the scoreboard plus its
rows to the second.  Everything stays in whole numbers.

Two of the eight settlements differ from the scorer shipped beside the notes,
and in both cases the shipped scorer still carries the leftover of the settling
it replaced: `half_even` sits defined but uncalled while `floor_div` runs, and
the None-guards that only an empty bucket can reach sit unreachable while the
plan claims such buckets are omitted.  The other six settlements are left as
shipped; nothing in the surviving material counts against them.
"""

import json
import sys


# --------------------------------------------------------------------------
# The eight settlements the lost sections governed.
# --------------------------------------------------------------------------

# 3. which class wins when two classes tie for the largest logit
TIE_LOW = False

# 4. whether the abstain floor is applied before or after routing
ABSTAIN = "after_route"

# 5. which relation the scorer drives the routing from
DRIVE = "items"

# 6. whether a bucket that ends with no surviving item still reports.  Sections 6
#    and 7 survive with "or blank when it holds none" and "a bucket with no item
#    reports a blank mean", which only bite when such a bucket comes back; the
#    shipped scorer guards that case in code it can never reach.
EMPTY_BUCKET = "report"

# 7. how total/n becomes a whole number.  The shipped scorer defines half_even
#    and never calls it, running floor_div instead -- the leftover of the switch.
MEAN_MODE = "half_even"

# 8. what the confidence sums
CONFIDENCE = "top_sum"

# 9. where a bucket with a blank mean sorts
NULL_ORDER = "nulls_last"

# 9. how two buckets holding the same mean order between themselves
TIE_ORDER = "by_join_id_asc"


def half_even(num, den):
    q, r = divmod(num, den)
    twice = 2 * r
    if twice > den or (twice == den and q % 2 != 0):
        q += 1
    return q


def half_up(num, den):
    q, r = divmod(num, den)
    return q + 1 if 2 * r >= den else q


def half_down(num, den):
    q, r = divmod(num, den)
    return q + 1 if 2 * r > den else q


def ceil_div(num, den):
    return -((-num) // den)


def floor_div(num, den):
    return num // den


MEAN_FNS = {
    "floor": floor_div,
    "ceil": ceil_div,
    "half_even": half_even,
    "half_up": half_up,
    "half_down": half_down,
}


def argmax_logit(logits, tie_low):
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

    drive, probe = DRIVE, ("routes" if DRIVE == "items" else "items")

    floor = batch["floor"]

    resolved = []
    for row in items:
        cid, top = argmax_logit(row["logits"], TIE_LOW)
        runner = second_best(row["logits"], cid)
        margin = top - runner if runner is not None else top
        resolved.append({"item_id": row["item_id"], "winner": cid,
                         "top": top, "margin": margin})

    route_of = {}
    bucket_order = []
    for r in routes:
        if r["winner"] not in route_of:
            route_of[r["winner"]] = r["bucket"]
            if r["bucket"] not in bucket_order:
                bucket_order.append(r["bucket"])

    if ABSTAIN == "before_route":
        resolved = [r for r in resolved if r["top"] >= floor]

    joined = []
    for r in resolved:
        if r["winner"] in route_of:
            merged = dict(r)
            merged["bucket"] = route_of[r["winner"]]
            merged["_join_id"] = r["item_id"] * 1000 + r["winner"]
            joined.append(merged)

    if ABSTAIN != "before_route":
        joined = [r for r in joined if r["top"] >= floor]

    groups = {}
    for row in joined:
        groups.setdefault(row["bucket"], []).append(row)

    mean_fn = MEAN_FNS[MEAN_MODE]

    keys = list(bucket_order) if EMPTY_BUCKET == "report" else list(groups.keys())
    seen = set(keys)
    for key in groups:
        if key not in seen:
            keys.append(key)
            seen.add(key)

    out = []
    for key in keys:
        members = groups.get(key)
        if members is None:
            out.append({"key": key, "n": 0, "total": 0,
                        "mean": None, "top_row": None, "confidence": 0})
            continue
        margins = [m["margin"] for m in members]
        total = sum(margins)
        mean = mean_fn(total, len(margins))
        ids = [m["_join_id"] for m in members]
        if CONFIDENCE == "margin_sum":
            confidence = total
        else:
            confidence = sum(m["top"] for m in members)
        out.append({"key": key, "n": len(members), "total": total,
                    "mean": mean, "top_row": max(ids) if ids else None,
                    "confidence": confidence})

    if TIE_ORDER == "by_join_id_asc":
        out.sort(key=lambda e: (e["top_row"] if e["top_row"] is not None else -1))
    elif TIE_ORDER == "by_join_id_desc":
        out.sort(key=lambda e: (e["top_row"] if e["top_row"] is not None else -1),
                 reverse=True)

    if NULL_ORDER == "nulls_first":
        out.sort(key=lambda e: (0 if e["mean"] is None else 1,
                                e["mean"] if e["mean"] is not None else 0))
    else:
        out.sort(key=lambda e: (1 if e["mean"] is None else 0,
                                e["mean"] if e["mean"] is not None else 0))

    plan = {"drive": drive, "probe": probe, "abstain": ABSTAIN,
            "empty_bucket": EMPTY_BUCKET, "null_order": NULL_ORDER,
            "tie_order": TIE_ORDER, "confidence": CONFIDENCE,
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
