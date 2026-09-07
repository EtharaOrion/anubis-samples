"""The Relay batch-inference scorer, as it was last left."""

import json
import sys


def half_even(num, den):
    q, r = divmod(num, den)
    twice = 2 * r
    if twice > den or (twice == den and q % 2 != 0):
        q += 1
    return q


def floor_div(num, den):
    return num // den


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

    drive, probe = "items", "routes"

    floor = batch["floor"]
    tie_low = False

    resolved = []
    for row in items:
        cid, top = argmax_logit(row["logits"], tie_low)
        runner = second_best(row["logits"], cid)
        margin = top - runner if runner is not None else top
        resolved.append({"item_id": row["item_id"], "winner": cid,
                         "top": top, "margin": margin, "gate": row.get("gate")})

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

    joined = [r for r in joined if r["top"] >= floor]

    groups = {}
    for row in joined:
        groups.setdefault(row["bucket"], []).append(row)

    out = []
    for key, members in groups.items():
        margins = [m["margin"] for m in members]
        total = sum(margins)
        mean = floor_div(total, len(margins)) if margins else None
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
