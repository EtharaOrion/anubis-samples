"""The Relay batch-inference scorer, restored to the settlements the archive was sealed under.

The settlements the lost sections carried, and what fixed each one:

  section 3, the class that wins a tie for the largest logit
      the lowest class id: the item is scored for the class that holds its
      largest logit, taken in the order the `logits` list presents the classes,
      so the first class to reach that largest value keeps it.

  section 4, when the abstain floor is applied
      before the item is routed.  Section 5 routes "each surviving item", and an
      abstained item is "scored nowhere", so it never reaches the routing.

  section 5, the relation the scorer drives from
      `routes` drives and `items` is probed.  Section 6 provides for a bucket
      that "ends with no surviving item", and section 7 for the blank mean such
      a bucket reports; a bucket with no item only reaches the scoreboard when
      the routes are what the scorer walks.

  section 6, whether an empty bucket reports
      it reports.  Section 6 gives it a blank `top_row`, section 7 gives it a
      blank mean, and section 9 sorts a bucket holding a blank mean.

  section 7, how the mean becomes a whole number
      half to even.

  section 8, the confidence
      the sum of the winning logits of the items the bucket holds.  The sum of
      the margins is already reported as `total` in section 6.

  section 9, the order buckets come back in
      buckets holding a blank mean sort last, and buckets holding the same mean
      order by the join identifier reported as `top_row`, ascending.
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
        if top is None or top < floor:
            continue
        runner = second_best(row["logits"], cid)
        margin = top - runner if runner is not None else top
        resolved.append({"item_id": row["item_id"], "winner": cid,
                         "top": top, "margin": margin, "gate": row.get("gate")})

    by_class = {}
    for r in resolved:
        by_class.setdefault(r["winner"], []).append(r)

    groups = {}
    for route in routes:
        members = groups.setdefault(route["bucket"], [])
        for r in by_class.get(route["winner"], ()):
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
        confidence = sum(m["top"] for m in members)
        out.append({"key": key, "n": len(members), "total": total,
                    "mean": mean, "top_row": max(ids) if ids else None,
                    "confidence": confidence})

    out.sort(key=lambda e: (e["top_row"] if e["top_row"] is not None else -1))
    out.sort(key=lambda e: (1 if e["mean"] is None else 0,
                            e["mean"] if e["mean"] is not None else 0))

    plan = {"drive": drive, "probe": probe, "abstain": "before_route",
            "empty_bucket": "report", "null_order": "nulls_last",
            "tie_order": "by_join_id_asc", "confidence": "top_sum",
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
