"""The Relay batch-inference scorer, rebuilt from the surviving notes.

Lost sections, settled as follows:

  section 3  winning class ...... the lowest class id wins a tie for largest logit
  section 4  abstain floor ...... applied after the item is routed to its bucket
  section 5  drive .............. driven from `items`, probing `routes`
  section 6  empty buckets ...... a bucket that ends with no survivor still reports
  section 7  reported mean ...... total / n rounded to nearest, ties to even
  section 8  confidence ......... the sum of the winning logits it holds
  section 9  order .............. by mean ascending, blank means last,
                                  equal means by the smaller join identifier
"""

import json
import sys


def half_even(num, den):
    """Divide and round to the nearest whole number, ties to even."""
    q, r = divmod(num, den)
    twice = 2 * r
    if twice > den or (twice == den and q % 2 != 0):
        q += 1
    return q


def argmax_logit(logits):
    """Winning class and its logit; the lowest class id wins a tie."""
    best_id = None
    best_val = None
    for entry in logits:
        cid, val = entry["class"], entry["logit"]
        if best_val is None or val > best_val or (val == best_val
                                                  and cid < best_id):
            best_id, best_val = cid, val
    return best_id, best_val


def second_best(logits, winner_id):
    """Largest logit among the classes that did not win, or blank."""
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
    tie_low = True

    floor = batch["floor"]

    # Section 3: resolve the winning class, the winning logit and the margin.
    resolved = []
    for row in items:
        cid, top = argmax_logit(row["logits"])
        runner = second_best(row["logits"], cid)
        margin = top - runner if runner is not None else top
        resolved.append({"item_id": row["item_id"], "winner": cid,
                         "top": top, "margin": margin})

    # Section 5: drive from `items`, matching each item to the route whose
    # winner equals its winning class, and stamping the join identifier.
    route_of = {}
    for r in routes:
        route_of[r["winner"]] = r["bucket"]

    joined = []
    for r in resolved:
        if r["winner"] in route_of:
            merged = dict(r)
            merged["bucket"] = route_of[r["winner"]]
            merged["_join_id"] = r["item_id"] * 1000 + r["winner"]
            joined.append(merged)

    # Section 4: the floor is applied after the item is routed, so a bucket can
    # receive an item and then end with none.
    joined = [r for r in joined if r["top"] >= floor]

    # Section 6: gather by bucket. Every bucket the routes name reports, even
    # the ones that end with no surviving item.
    groups = {}
    for r in routes:
        groups.setdefault(r["bucket"], [])
    for row in joined:
        groups.setdefault(row["bucket"], []).append(row)

    out = []
    for key, members in groups.items():
        margins = [m["margin"] for m in members]
        total = sum(margins)
        ids = [m["_join_id"] for m in members]
        confidence = sum(m["top"] for m in members)
        out.append({"key": key, "n": len(margins), "total": total,
                    "mean": half_even(total, len(margins)) if margins else None,
                    "top_row": max(ids) if ids else None,
                    "confidence": confidence})

    # Section 9: blank means sort last; equal means order by the smaller join
    # identifier.
    out.sort(key=lambda e: (e["top_row"] if e["top_row"] is not None else -1))
    out.sort(key=lambda e: (1 if e["mean"] is None else 0,
                            e["mean"] if e["mean"] is not None else 0))

    plan = {"drive": drive, "probe": probe, "abstain": "after_route",
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
