"""The Relay batch-inference scorer, restored to the archived settlements.

Usage: python3 scorer.py BATCH_JSON BOARD_JSON

Each constant below names the decision one of the lost sections of the scoring
notes governed, so the reading this scorer implements can be read beside the
notes it obeys.  The alternative each section could have been settled on is
kept in the comment beside it.

Everything is whole-number arithmetic from end to end, nothing outside the
standard library is used, and nothing is read except the batch document named
on the command line.
"""

import json
import sys

# Section 3 - which class wins when two tie for the largest logit.
TIE_WINNER = "highest_class"         # or "lowest_class"
# Section 4 - whether the abstain floor is applied before or after routing.
ABSTAIN = "before_route"             # or "after_route"
# Section 5 - which relation drives the routing and which is probed.
DRIVE, PROBE = "routes", "items"     # or "items", "routes"
# Section 6 - whether a bucket that ends with no surviving item still reports.
EMPTY_BUCKET = "report"              # or "omit"
# Section 7 - how the mean's division becomes a whole number.
ROUNDING = "half_even"               # or "floor"
# Section 8 - what a bucket's confidence is taken over.
CONFIDENCE = "top_sum"               # or "margin_sum"
# Section 9 - where a blank mean sorts, and how equal means order.
NULL_ORDER = "nulls_last"            # or "nulls_first"
TIE_ORDER = "by_join_id_asc"         # or "by_join_id_desc"


def divide(total, count):
    """Section 7: the total divided by the count, as a whole number."""
    quotient, rest = divmod(total, count)
    if ROUNDING == "floor":
        return quotient
    twice = 2 * rest
    if twice > count or (twice == count and quotient % 2 != 0):
        quotient += 1
    return quotient


def argmax_logit(logits):
    """Section 3: the class holding the largest logit, ties settled by id."""
    best_id = None
    best_val = None
    for entry in logits:
        if "class" not in entry or "logit" not in entry:
            continue                          # a blank cell scores nothing
        cid, val = entry["class"], entry["logit"]
        if best_val is None or val > best_val:
            best_id, best_val = cid, val
        elif val == best_val:
            if TIE_WINNER == "lowest_class":
                if cid < best_id:
                    best_id = cid
            elif cid > best_id:
                best_id = cid
    return best_id, best_val


def second_best(logits, winner_id):
    """Section 3: the largest logit among the remaining classes, or blank."""
    best_val = None
    for entry in logits:
        if entry.get("class") == winner_id or "logit" not in entry:
            continue
        if best_val is None or entry["logit"] > best_val:
            best_val = entry["logit"]
    return best_val


def resolve_items(items, floor):
    """Sections 3 and 4: the winning class, the margin, and the floor.

    The floor is applied here, before anything is routed: an item below it is
    abstained and scored nowhere.
    """
    resolved = []
    for row in items:
        logits = row.get("logits") or []
        cid, top = argmax_logit(logits)
        if cid is None or top < floor:
            continue
        runner = second_best(logits, cid)
        resolved.append({"winner": cid, "top": top,
                         "margin": top - runner if runner is not None else top,
                         "join_id": row["item_id"] * 1000 + cid})
    return resolved


def gather(routes, resolved):
    """Sections 5 and 6: drive the routing from `routes`, probe the items.

    Driving from the routes means every bucket the routes name is enumerated,
    so a bucket that ends with no surviving item is still there to report.
    """
    held = {}
    order = []
    for route in routes:
        bucket = route.get("bucket")
        if bucket not in held:
            held[bucket] = []
            order.append(bucket)
    matched = set()
    for route in routes:
        cid = route["winner"]
        if cid in matched:
            continue                          # a class is matched to one route
        matched.add(cid)
        for item in resolved:
            if item["winner"] == cid:
                held[route.get("bucket")].append(item)
    return order, held


def bucket_row(key, items):
    """Sections 6, 7 and 8: what a bucket reports."""
    total = sum(item["margin"] for item in items)
    if CONFIDENCE == "top_sum":
        confidence = sum(item["top"] for item in items)
    else:
        confidence = sum(item["margin"] for item in items)
    return {"key": key,
            "n": len(items),
            "total": total,
            "mean": divide(total, len(items)) if items else None,
            "top_row": max(item["join_id"] for item in items) if items else None,
            "confidence": confidence}


def tie_key(row):
    """Section 9: how two buckets holding the same mean order between them."""
    join_id = row["top_row"] if row["top_row"] is not None else -1
    return -join_id if TIE_ORDER == "by_join_id_desc" else join_id


def mean_key(row):
    """Section 9: the order by mean, and where a blank mean sorts."""
    blank = row["mean"] is None
    if NULL_ORDER == "nulls_first":
        return (0 if blank else 1, 0 if blank else row["mean"])
    return (1 if blank else 0, 0 if blank else row["mean"])


def score_batch(batch, tables):
    items = list(tables[batch["items"]])
    routes = list(tables[batch["routes"]])

    resolved = resolve_items(items, batch["floor"])
    order, held = gather(routes, resolved)

    rows = []
    for key in order:
        if not held[key] and EMPTY_BUCKET == "omit":
            continue
        rows.append(bucket_row(key, held[key]))

    rows.sort(key=tie_key)
    rows.sort(key=mean_key)

    plan = {"drive": DRIVE, "probe": PROBE, "abstain": ABSTAIN,
            "empty_bucket": EMPTY_BUCKET, "null_order": NULL_ORDER,
            "tie_order": TIE_ORDER, "confidence": CONFIDENCE,
            "buckets": len(rows)}
    return plan, rows


def main(argv):
    if len(argv) != 3:
        sys.stderr.write("usage: scorer.py BATCH_JSON BOARD_JSON\n")
        return 2
    with open(argv[1], encoding="utf-8") as handle:
        document = json.load(handle)
    plan, rows = score_batch(document["batch"], document["tables"])
    with open(argv[2], "w", encoding="utf-8") as handle:
        json.dump({"batch": document["batch"]["name"], "plan": plan,
                   "rows": rows}, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
