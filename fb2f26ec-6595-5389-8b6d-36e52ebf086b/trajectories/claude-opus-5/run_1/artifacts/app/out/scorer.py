"""The Relay batch-inference scorer, restored to the settlements the archive was sealed under."""

import json
import sys


def half_even(num, den):
    """Divide two whole numbers, rounding a half to the nearer even whole number."""
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

    # The routing is driven from the smaller relation and probes the larger one.
    if len(items) < len(routes):
        drive, probe = "items", "routes"
    else:
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

    # The floor is applied before the item is routed to its bucket.
    resolved = [r for r in resolved if r["top"] >= floor]

    route_of = {}
    bucket_order = []
    for r in routes:
        route_of[r["winner"]] = r["bucket"]
        if r["bucket"] not in bucket_order:
            bucket_order.append(r["bucket"])

    groups = {}
    for r in resolved:
        if r["winner"] in route_of:
            merged = dict(r)
            merged["bucket"] = route_of[r["winner"]]
            merged["_join_id"] = r["item_id"] * 1000 + r["winner"]
            groups.setdefault(merged["bucket"], []).append(merged)

    # Every bucket the routes name reports, even one that ends with no item.
    out = []
    for key in bucket_order:
        members = groups.get(key, [])
        margins = [m["margin"] for m in members]
        total = sum(margins)
        mean = half_even(total, len(margins)) if margins else None
        ids = [m["_join_id"] for m in members]
        confidence = sum(margins)
        out.append({"key": key, "n": len(members), "total": total,
                    "mean": mean, "top_row": max(ids) if ids else None,
                    "confidence": confidence})

    # Equal means order by the largest join identifier, descending; a blank mean sorts first.
    out.sort(key=lambda e: -(e["top_row"] if e["top_row"] is not None else -1))
    out.sort(key=lambda e: (0 if e["mean"] is None else 1,
                            e["mean"] if e["mean"] is not None else 0))

    plan = {"drive": drive, "probe": probe, "abstain": "before_route",
            "empty_bucket": "emit", "null_order": "nulls_first",
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
        json.dump({"plan": plan, "rows": rows}, handle,
                  sort_keys=True, separators=(",", ":"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
