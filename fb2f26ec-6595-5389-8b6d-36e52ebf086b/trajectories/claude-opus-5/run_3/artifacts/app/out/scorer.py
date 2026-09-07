"""The Relay batch-inference scorer, restored to the archived settlements."""
import json
import sys

# --- the settlements the archive was sealed under -------------------------
TIE_LOW = True            # S3: lowest class id wins a tie for the top logit
ABSTAIN = "after_route"          # S4: floor applied before/after routing
DRIVE = "routes"              # S5: relation the routing drives from
EMPTY_BUCKET = "report"       # S6: whether an itemless bucket still reports
ROUNDING = "half_even"           # S7: how total/n becomes a whole number
CONFIDENCE = "top_sum"          # S8: what confidence sums
NULL_ORDER = "nulls_last"         # S9: where a blank mean sorts
TIE_ORDER = "by_join_id_asc"           # S9: how equal means order


def divide(num, den):
    """Integer division under the settled rounding."""
    if ROUNDING == "floor":
        return num // den
    if ROUNDING == "trunc":
        q = abs(num) // abs(den)
        return q if (num < 0) == (den < 0) else -q
    if ROUNDING == "ceil":
        return -((-num) // den)
    q, r = divmod(num, den)
    twice = 2 * r
    if twice > den:
        q += 1
    elif twice == den:
        if ROUNDING == "half_up":
            q += 1
        elif q % 2 != 0:            # half_even
            q += 1
    return q


def argmax_logit(logits):
    """Winning class id and its logit."""
    best_id = None
    best_val = None
    for entry in logits:
        cid, val = entry["class"], entry["logit"]
        if best_val is None or val > best_val:
            best_id, best_val = cid, val
        elif val == best_val:
            if TIE_LOW:
                if cid < best_id:
                    best_id = cid
            elif cid > best_id:
                best_id = cid
    return best_id, best_val


def second_best(logits, winner_id):
    """Largest logit among the classes that did not win, or None."""
    best_val = None
    for entry in logits:
        if entry["class"] == winner_id:
            continue
        if best_val is None or entry["logit"] > best_val:
            best_val = entry["logit"]
    return best_val


def resolve(items):
    """One record per item: winning class, winning logit, margin."""
    out = []
    for row in items:
        logits = row.get("logits") or []
        if not logits:
            continue
        cid, top = argmax_logit(logits)
        runner = second_best(logits, cid)
        margin = top - runner if runner is not None else top
        out.append({"item_id": row["item_id"], "winner": cid, "top": top,
                    "margin": margin})
    return out


def score_batch(batch, tables):
    items = list(tables[batch["items"]])
    routes = list(tables[batch["routes"]])
    floor = batch["floor"]
    resolved = resolve(items)
    if ABSTAIN == "before_route":
        resolved = [r for r in resolved if r["top"] >= floor]
    joined = []
    seen_buckets = []

    def note(bucket):
        if bucket not in seen_buckets:
            seen_buckets.append(bucket)

    def match(rec, bucket):
        merged = dict(rec)
        merged["bucket"] = bucket
        merged["join_id"] = rec["item_id"] * 1000 + rec["winner"]
        joined.append(merged)

    if DRIVE == "routes":
        # every route is a driving row, so a route whose class drew no item
        # still brings its bucket back
        by_class = {}
        for rec in resolved:
            by_class.setdefault(rec["winner"], []).append(rec)
        for route in routes:
            note(route["bucket"])
            for rec in by_class.get(route["winner"], ()):
                match(rec, route["bucket"])
    else:
        # every item is a driving row, so only buckets an item reached appear
        route_of = {}
        for route in routes:
            route_of.setdefault(route["winner"], route["bucket"])
        for rec in resolved:
            if rec["winner"] in route_of:
                note(route_of[rec["winner"]])
                match(rec, route_of[rec["winner"]])
    if ABSTAIN != "before_route":
        joined = [r for r in joined if r["top"] >= floor]
    groups = {}
    for row in joined:
        groups.setdefault(row["bucket"], []).append(row)
    keys = list(seen_buckets) if EMPTY_BUCKET != "omit" else \
        [k for k in seen_buckets if k in groups]
    out = []
    for key in keys:
        members = groups.get(key, [])
        margins = [m["margin"] for m in members]
        total = sum(margins)
        mean = divide(total, len(margins)) if margins else None
        ids = [m["join_id"] for m in members]
        if CONFIDENCE == "margin_sum":
            confidence = total
        else:
            confidence = sum(m["top"] for m in members)
        out.append({"key": key, "n": len(members), "total": total, "mean": mean,
                    "top_row": max(ids) if ids else None,
                    "confidence": confidence})
    if TIE_ORDER == "by_join_id_desc":
        out.sort(key=lambda e: -(e["top_row"] if e["top_row"] is not None else -1))
    elif TIE_ORDER == "by_key_asc":
        out.sort(key=lambda e: e["key"])
    elif TIE_ORDER == "by_key_desc":
        out.sort(key=lambda e: e["key"], reverse=True)
    else:
        out.sort(key=lambda e: (e["top_row"] if e["top_row"] is not None else -1))
    late = 1 if NULL_ORDER == "nulls_last" else -1
    out.sort(key=lambda e: (late if e["mean"] is None else 0,
                            e["mean"] if e["mean"] is not None else 0))
    plan = {"drive": DRIVE, "probe": "routes" if DRIVE == "items" else "items",
            "abstain": ABSTAIN, "empty_bucket": EMPTY_BUCKET,
            "null_order": NULL_ORDER, "tie_order": TIE_ORDER,
            "confidence": CONFIDENCE, "buckets": len(out)}
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
