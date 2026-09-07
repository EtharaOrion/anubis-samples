"""The Relay batch scorer, rebuilt to the settlements the archive was sealed under.

Sections of the scoring notes that went missing, and the settlement each one
is carried out under here:

  section 3  the lowest class id wins when two classes tie for the largest logit
  section 4  the floor is applied before the item is routed to its bucket
  section 5  the routing is driven from the items relation, probing the routes
  section 6  a bucket that ends with no surviving item still reports
  section 7  the mean is rounded to the nearer whole number, halves to even
  section 8  the confidence is the sum of the winning logits of the items held
  section 9  a blank mean sorts last; equal means order by join identifier

Whole numbers are kept whole from end to end: every value the scoreboard
reports is computed with integer arithmetic alone.
"""

import json
import sys


def half_even(num, den):
    """num / den as a whole number, a half going to the even neighbour."""
    q, r = divmod(num, den)
    twice = 2 * r
    if twice > den or (twice == den and q % 2 != 0):
        q += 1
    return q


def argmax_logit(logits, tie_low=True):
    """The class holding the largest logit; the lowest id wins a tie."""
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
    """The largest logit among the classes that did not win, or None."""
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

    drive, probe = "items", "routes"

    floor = batch["floor"]

    route_of = {}
    for r in routes:
        route_of[r["winner"]] = r["bucket"]

    # Every routed bucket reports, held or not.
    groups = {}
    order = []
    for r in routes:
        if r["bucket"] not in groups:
            groups[r["bucket"]] = []
            order.append(r["bucket"])

    joined = []
    for row in items:
        if not row["logits"]:
            continue
        cid, top = argmax_logit(row["logits"])
        if top < floor:
            # abstained before it is routed, so it is scored nowhere
            continue
        if cid not in route_of:
            continue
        runner = second_best(row["logits"], cid)
        margin = top - runner if runner is not None else top
        merged = {"item_id": row["item_id"], "winner": cid, "top": top,
                  "margin": margin, "bucket": route_of[cid],
                  "_join_id": row["item_id"] * 1000 + cid}
        joined.append(merged)
        groups[merged["bucket"]].append(merged)

    out = []
    for key in order:
        members = groups[key]
        margins = [m["margin"] for m in members]
        total = sum(margins)
        mean = half_even(total, len(margins)) if margins else None
        ids = [m["_join_id"] for m in members]
        confidence = sum(m["top"] for m in members)
        out.append({"key": key, "n": len(members), "total": total,
                    "mean": mean, "top_row": max(ids) if ids else None,
                    "confidence": confidence})

    # equal means order by join identifier, a blank mean last
    out.sort(key=lambda e: e["top_row"] if e["top_row"] is not None else -1)
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
