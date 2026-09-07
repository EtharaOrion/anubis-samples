"""The Relay batch-inference scorer, rebuilt to the archived settlements.

Usage: python3 scorer.py BATCH_JSON BOARD_JSON

Every section of the scoring notes that survives is obeyed exactly. The eight
settlements the lost sections governed were recovered from the archive and the
surviving notes, and they are these:

  section 3  a tie for the largest logit is won by the lowest class id
  section 4  the floor is applied before the item is routed ("before_route")
  section 5  the routing is driven from the routes relation ("routes")
  section 6  a bucket that ends with no surviving item still reports ("report")
  section 7  total / n is made whole by rounding halves to even ("half_even")
  section 8  confidence is the sum of the margins the bucket holds ("margin_sum")
  section 9  a bucket with a blank mean sorts first ("nulls_first")
  section 9  buckets holding the same mean order by join identifier,
             highest first ("by_join_id_desc")

All arithmetic stays in whole numbers from end to end.
"""

import json
import sys


def half_even(num, den):
    q, r = divmod(num, den)
    twice = 2 * r
    if twice > den or (twice == den and q % 2 != 0):
        q += 1
    return q


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

    drive, probe = "routes", "items"

    floor = batch["floor"]
    tie_low = True

    resolved = []
    for row in items:
        cid, top = argmax_logit(row["logits"], tie_low)
        runner = second_best(row["logits"], cid)
        margin = top - runner if runner is not None else top
        resolved.append({"item_id": row["item_id"], "winner": cid,
                         "top": top, "margin": margin, "gate": row.get("gate")})

    # section 4: the floor is applied before the item is routed, so an
    # abstained item is scored nowhere.
    resolved = [r for r in resolved if r["top"] >= floor]

    # section 5: the routing is driven from the routes relation; each route
    # probes the items whose winning class it takes. section 6: every bucket a
    # route names reports, whether or not it ends up holding an item.
    by_class = {}
    for r in resolved:
        by_class.setdefault(r["winner"], []).append(r)

    groups = {}
    for route in routes:
        members = groups.setdefault(route["bucket"], [])
        for r in by_class.get(route["winner"], []):
            merged = dict(r)
            merged["bucket"] = route["bucket"]
            merged["_join_id"] = r["item_id"] * 1000 + r["winner"]
            members.append(merged)

    out = []
    for key, members in groups.items():
        margins = [m["margin"] for m in members]
        total = sum(margins)
        mean = half_even(total, len(margins)) if margins else None
        ids = [m["_join_id"] for m in members]
        confidence = sum(m["margin"] for m in members)
        out.append({"key": key, "n": len(members), "total": total,
                    "mean": mean, "top_row": max(ids) if ids else None,
                    "confidence": confidence})

    # section 9: the first sort sets the order between buckets holding the
    # same mean, highest join identifier first; the second sorts by mean with a
    # blank mean first, and Python's stable sort keeps the first order wherever
    # the means are equal.
    out.sort(key=lambda e: (e["top_row"] if e["top_row"] is not None else -1),
             reverse=True)
    out.sort(key=lambda e: (0 if e["mean"] is None else 1,
                            e["mean"] if e["mean"] is not None else 0))

    plan = {"drive": drive, "probe": probe, "abstain": "before_route",
            "empty_bucket": "report", "null_order": "nulls_first",
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
