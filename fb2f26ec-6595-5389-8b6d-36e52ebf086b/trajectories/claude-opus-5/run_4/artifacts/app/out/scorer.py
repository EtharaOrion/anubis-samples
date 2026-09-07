"""A Relay batch scorer written to the surviving scoring notes.

Usage:  python3 scorer.py BATCH_JSON BOARD_JSON

The sections of the notes that survive are obeyed to the letter.  The eight
decisions the lost sections governed are settled by the constants below; every
one of them is named after the section it comes from.
"""

import json
import sys

# ---------------------------------------------------------------------------
# The settlements the lost sections governed.
# ---------------------------------------------------------------------------

# Section 3 -- which class wins when two classes tie for the largest logit.
#   "low"  : the smaller class id wins.
#   "high" : the larger class id wins.
TIE_CLASS = "low"

# Section 4 -- when the abstain floor is applied.
#   "before_route" : abstained items never reach a bucket.
#   "after_route"  : items are routed first and dropped afterwards, so a bucket
#                    can end up holding nothing.
ABSTAIN = "before_route"

# Section 5 -- which relation the scorer drives the routing from.
#   "items" or "routes".  Driving from routes puts every routed bucket on the
#   board; driving from items only puts up the buckets items reached.
DRIVE = "routes"

# Section 6 -- whether a bucket that ends with no surviving item still reports.
#   "omit" or "keep".
EMPTY_BUCKET = "keep"

# Section 7 -- how the mean's division becomes a whole number.
#   "floor", "ceil", "half_even", "half_up" or "half_down".
ROUNDING = "half_even"

# Section 8 -- what the reported confidence is taken over.
#   "top_sum"    : the sum of the winning logits of the items the bucket holds.
#   "margin_sum" : the sum of their margins.
CONFIDENCE = "top_sum"

# Section 9 -- where a blank mean sorts, and how equal means order.
#   NULL_ORDER : "nulls_last" or "nulls_first".
#   TIE_ORDER  : "by_join_id_asc", "by_join_id_desc", "by_key_asc" or
#                "by_key_desc".
NULL_ORDER = "nulls_last"
TIE_ORDER = "by_join_id_asc"


# ---------------------------------------------------------------------------
# Whole-number arithmetic.  Nothing here ever leaves the integers.
# ---------------------------------------------------------------------------

def divide(num, den):
    """Divide two whole numbers into a whole number, section 7's way."""
    quo, rem = divmod(num, den)          # floors, remainder has den's sign
    if rem == 0:
        return quo
    if ROUNDING == "floor":
        return quo
    if ROUNDING == "ceil":
        return quo + 1
    twice = 2 * rem
    if ROUNDING == "half_up":
        return quo + 1 if twice >= den else quo
    if ROUNDING == "half_down":
        return quo + 1 if twice > den else quo
    # half_even
    if twice > den or (twice == den and quo % 2 != 0):
        return quo + 1
    return quo


def winning_class(logits):
    """Section 3: the class holding the largest logit, and that logit."""
    best_id = None
    best_val = None
    for entry in logits:
        if not isinstance(entry, dict):
            continue
        if "class" not in entry or "logit" not in entry:
            continue
        cid, val = entry["class"], entry["logit"]
        if best_val is None or val > best_val:
            best_id, best_val = cid, val
        elif val == best_val:
            if TIE_CLASS == "low":
                if cid < best_id:
                    best_id = cid
            elif cid > best_id:
                best_id = cid
    return best_id, best_val


def runner_up(logits, winner_id):
    """Section 3: the largest logit among the remaining classes, or None."""
    best_val = None
    for entry in logits:
        if not isinstance(entry, dict):
            continue
        if "class" not in entry or "logit" not in entry:
            continue
        if entry["class"] == winner_id:
            continue
        if best_val is None or entry["logit"] > best_val:
            best_val = entry["logit"]
    return best_val


def score_batch(batch, tables):
    items = list(tables.get(batch.get("items"), []))
    routes = list(tables.get(batch.get("routes"), []))
    floor = batch.get("floor")

    # Section 5: the routes, keyed by the class they route.
    route_of = {}
    bucket_order = []
    for row in routes:
        if not isinstance(row, dict) or "winner" not in row or "bucket" not in row:
            continue
        route_of[row["winner"]] = row["bucket"]
        if row["bucket"] not in bucket_order:
            bucket_order.append(row["bucket"])

    # Section 3: resolve every item, then section 4's floor and section 5's join.
    matched = []
    for row in items:
        if not isinstance(row, dict) or "item_id" not in row:
            continue
        logits = row.get("logits") or []
        cid, top = winning_class(logits)
        if cid is None:
            continue
        runner = runner_up(logits, cid)
        margin = top - runner if runner is not None else top
        survives = floor is None or top >= floor
        if ABSTAIN == "before_route" and not survives:
            continue
        if cid not in route_of:
            continue
        matched.append({"bucket": route_of[cid],
                        "top": top,
                        "margin": margin,
                        "join_id": row["item_id"] * 1000 + cid,
                        "survives": survives})

    live = [m for m in matched if m["survives"]]

    # Section 6: the buckets are the ones the routes name, in the order the
    # route table names them.  Section 6's settlement decides only whether the
    # ones no surviving item reached still report.
    universe = list(bucket_order)

    held = {}
    for m in live:
        held.setdefault(m["bucket"], []).append(m)

    rows = []
    for key in universe:
        members = held.get(key, [])
        if not members and EMPTY_BUCKET == "omit":
            continue
        count = len(members)
        total = sum(m["margin"] for m in members)
        confidence = sum((m["top"] if CONFIDENCE == "top_sum" else m["margin"])
                         for m in members)
        rows.append({"key": key,
                     "n": count,
                     "total": total,
                     "mean": divide(total, count) if count else None,
                     "top_row": max(m["join_id"] for m in members) if members else None,
                     "confidence": confidence})

    # Section 9: the tie-break first, then the mean, so the mean wins.
    if TIE_ORDER == "by_join_id_asc":
        rows.sort(key=lambda e: (e["top_row"] is None, e["top_row"] or 0))
    elif TIE_ORDER == "by_join_id_desc":
        rows.sort(key=lambda e: (e["top_row"] is None, -(e["top_row"] or 0)))
    elif TIE_ORDER == "by_key_asc":
        rows.sort(key=lambda e: str(e["key"]))
    else:
        rows.sort(key=lambda e: str(e["key"]), reverse=True)

    blank_last = 1 if NULL_ORDER == "nulls_last" else -1
    rows.sort(key=lambda e: (blank_last if e["mean"] is None else 0,
                             e["mean"] if e["mean"] is not None else 0))

    plan = {"drive": DRIVE,
            "probe": "routes" if DRIVE == "items" else "items",
            "abstain": ABSTAIN,
            "empty_bucket": EMPTY_BUCKET,
            "null_order": NULL_ORDER,
            "tie_order": TIE_ORDER,
            "confidence": CONFIDENCE,
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
        json.dump({"batch": document["batch"]["name"], "plan": plan, "rows": rows},
                  handle, indent=2, sort_keys=True)
        handle.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
