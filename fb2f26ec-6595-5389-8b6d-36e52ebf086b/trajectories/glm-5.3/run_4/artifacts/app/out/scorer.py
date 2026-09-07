"""The Relay batch-inference scorer, rebuilt to the archive's settlements.

Run as:  python3 scorer.py BATCH_JSON BOARD_JSON

It reads the batch document from the first path and writes its scoreboard and
the rows it produced as JSON to the second. Every number it reports is a whole
number and every bit of arithmetic stays in whole numbers end to end.

The settlements carried here, section by section:

  3  winning class     the class holding the largest logit; where two classes
                       tie for the largest logit the higher class id wins
  4  abstain floor     applied after the surviving item has been routed to its
                       bucket ("after_route")
  5  driving relation  the scorer drives from `items` and probes `routes`
  6  empty buckets     a bucket that ends with no surviving item is omitted
  7  reported mean     total divided by n by round-half-to-even ("half_even")
  8  confidence        the sum of the winning logits the bucket holds ("top_sum")
  9  order             by mean ascending; a blank mean sorts last ("nulls_last")
                       and equal means order by join id ascending
                       ("by_join_id_asc")

Nothing is read from anywhere but the batch named on the command line, nothing
is imported from beside this file, and nothing but the board document is
written.
"""

import json
import sys


def half_even(num, den):
    """Whole-number division that rounds an exact half to the even neighbour."""
    q, r = divmod(num, den)
    twice = 2 * r
    if twice > den or (twice == den and q % 2 != 0):
        q += 1
    return q


def argmax_logit(logits):
    """The class holding the largest logit; the higher class id wins a tie."""
    best_id = None
    best_val = None
    for entry in logits:
        cid, val = entry["class"], entry["logit"]
        if best_val is None or val > best_val:
            best_id, best_val = cid, val
        elif val == best_val and cid > best_id:
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

    resolved = []
    for row in items:
        cid, top = argmax_logit(row["logits"])
        runner = second_best(row["logits"], cid)
        margin = top - runner if runner is not None else top
        resolved.append({"item_id": row["item_id"], "winner": cid,
                         "top": top, "margin": margin})

    route_of = {}
    for r in routes:
        # a blank cell leaves the row unusable, so it routes nothing
        if r.get("winner") is None or r.get("bucket") is None:
            continue
        route_of[r["winner"]] = r["bucket"]

    joined = []
    for r in resolved:
        if r["winner"] in route_of:
            merged = dict(r)
            merged["bucket"] = route_of[r["winner"]]
            merged["_join_id"] = r["item_id"] * 1000 + r["winner"]
            joined.append(merged)

    joined = [r for r in joined if r["top"] >= floor]

    groups = {}
    for row in joined:
        groups.setdefault(row["bucket"], []).append(row)

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

    plan = {"drive": drive, "probe": probe, "abstain": "after_route",
            "empty_bucket": "omit", "null_order": "nulls_last",
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
