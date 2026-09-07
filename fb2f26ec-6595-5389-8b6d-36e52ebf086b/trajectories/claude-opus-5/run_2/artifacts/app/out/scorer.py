"""The Relay batch-inference scorer, restored to the archived settlements."""

import json
import sys

# ---------------------------------------------------------------------------
# The settlements the lost sections of the scoring notes carried.
#   TIE_LOW      (section 3) which class wins a tie for the largest logit
#   ABSTAIN      (section 4) whether the floor is applied before or after routing
#   DRIVE        (section 5) which relation the routing is driven from
#   EMPTY_BUCKET (section 6) whether a bucket that ends empty still reports
#   ROUNDING     (section 7) how the mean's division becomes a whole number
#   CONFIDENCE   (section 8) what the bucket confidence is taken over
#   NULL_ORDER   (section 9) where a blank mean sorts
#   TIE_ORDER    (section 9) how buckets holding the same mean order
# ---------------------------------------------------------------------------
TIE_LOW = True
ABSTAIN = "before_route"
DRIVE = "routes"
EMPTY_BUCKET = "report"
ROUNDING = "half_even"
CONFIDENCE = "top_sum"
NULL_ORDER = "nulls_last"
TIE_ORDER = "by_join_id_asc"


def whole_div(num, den, mode):
    """Turn num / den into a whole number the way the notes settled on."""
    if mode == "floor":
        return num // den
    if mode == "trunc":
        return -((-num) // den) if num < 0 else num // den
    if mode == "ceil":
        return -((-num) // den)
    q, r = divmod(num, den)
    twice = 2 * r
    if mode == "half_even":
        if twice > den or (twice == den and q % 2 != 0):
            q += 1
        return q
    if mode == "half_up":
        if twice >= den:
            q += 1
        return q
    if mode == "half_down":
        if twice > den:
            q += 1
        return q
    raise ValueError(mode)


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

    drive = DRIVE
    probe = "routes" if drive == "items" else "items"
    floor = batch["floor"]

    resolved = []
    for row in items:
        cid, top = argmax_logit(row["logits"], TIE_LOW)
        runner = second_best(row["logits"], cid)
        margin = top - runner if runner is not None else top
        resolved.append({"item_id": row["item_id"], "winner": cid,
                         "top": top, "margin": margin})

    route_of = {}
    for r in routes:
        route_of[r["winner"]] = r["bucket"]

    # The floor either thins the items before they are routed or thins the
    # matched rows afterwards; either way an abstained item is scored nowhere.
    if ABSTAIN == "before_route":
        candidates = [r for r in resolved if r["top"] >= floor]
    else:
        candidates = resolved

    matched = []
    for r in candidates:
        if r["winner"] in route_of:
            merged = dict(r)
            merged["bucket"] = route_of[r["winner"]]
            merged["join_id"] = r["item_id"] * 1000 + r["winner"]
            matched.append(merged)

    surviving = [r for r in matched if r["top"] >= floor]

    # Which relation drives the routing decides the order the buckets are first
    # seen in, and it decides whether buckets holding nothing can appear at all.
    if drive == "routes":
        seen = [r["bucket"] for r in routes]
    else:
        seen = [r["bucket"] for r in matched]

    if EMPTY_BUCKET == "report":
        allowed = None
    else:
        allowed = set(r["bucket"] for r in surviving)

    keys = []
    for key in seen:
        if (allowed is None or key in allowed) and key not in keys:
            keys.append(key)

    groups = dict((key, []) for key in keys)
    for row in surviving:
        if row["bucket"] in groups:
            groups[row["bucket"]].append(row)

    out = []
    for key in keys:
        members = groups[key]
        margins = [m["margin"] for m in members]
        if members:
            total = sum(margins)
            mean = whole_div(total, len(members), ROUNDING)
            top_row = max(m["join_id"] for m in members)
            if CONFIDENCE == "top_sum":
                confidence = sum(m["top"] for m in members)
            else:
                confidence = sum(margins)
        else:
            total = 0
            mean = None
            top_row = None
            confidence = 0
        out.append({"key": key, "n": len(members), "total": total, "mean": mean,
                    "top_row": top_row, "confidence": confidence})

    if TIE_ORDER == "by_join_id_asc":
        out.sort(key=lambda e: (e["top_row"] if e["top_row"] is not None else -1))
    elif TIE_ORDER == "by_join_id_desc":
        out.sort(key=lambda e: (e["top_row"] if e["top_row"] is not None else -1),
                 reverse=True)
    elif TIE_ORDER == "by_key_asc":
        out.sort(key=lambda e: e["key"])
    elif TIE_ORDER == "by_key_desc":
        out.sort(key=lambda e: e["key"], reverse=True)

    last = 1 if NULL_ORDER == "nulls_last" else -1
    out.sort(key=lambda e: (last if e["mean"] is None else 0,
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
