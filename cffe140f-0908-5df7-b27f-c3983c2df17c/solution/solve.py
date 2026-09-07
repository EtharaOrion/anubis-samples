"""Resolution core for sealed replay journals."""

import hashlib
import json
import os
import re
from itertools import combinations
from math import gcd

S_MASK32 = 0xFFFFFFFF
S_GUARD_NAMES = [
    "keystream",
    "chain",
    "delta2",
    "driftkey",
    "segperm",
    "backref",
    "endian",
    "countbias",
    "lenshadow",
    "rotate",
    "alias",
    "escape",
]
S_TIERS = ("champion", "contender", "provisional", "qualifier")
S_KIND_NAMES = {1: "deploy", 2: "output", 3: "eliminate", 4: "disband", 5: "concede", 6: "transfer", 8: "close"}
S_VALID_KINDS = frozenset(S_KIND_NAMES)


# ---------------------------------------------------------------- input load


def s_read_files(data_dir):
    blobs = {}
    for name in sorted(os.listdir(data_dir)):
        path = os.path.join(data_dir, name)
        if os.path.isfile(path):
            with open(path, "rb") as handle:
                blobs[name] = handle.read()
    return blobs


def s_split_sources(blobs):
    captures = {}
    texts = []
    for _name, raw in sorted(blobs.items()):
        if raw[:4] == b"RJNL":
            role = int.from_bytes(raw[24:28], "little") % 10
            captures[role] = raw
            continue
        try:
            texts.append(raw.decode("utf-8"))
        except UnicodeDecodeError:
            continue
    return captures, texts


def s_table(texts, header):
    cols = header.split(",")
    for text in texts:
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if not lines or lines[0] != header:
            continue
        rows = []
        for line in lines[1:]:
            parts = [p.strip() for p in line.split(",")]
            if len(parts) == len(cols):
                rows.append(dict(zip(cols, parts)))
        return rows
    return []


def s_roster(texts, label):
    pattern = re.compile(r"^" + re.escape(label) + r":\s*(.+)$", re.M)
    for text in texts:
        found = pattern.search(text)
        if found:
            return re.findall(r"`([^`]+)`", found.group(1))
    return []


def s_scalar(texts, label):
    pattern = re.compile(r"^" + re.escape(label) + r":\s*`(-?\d+)`\s*$", re.M)
    for text in texts:
        found = pattern.search(text)
        if found:
            return int(found.group(1))
    raise ValueError("missing scalar " + label)


def s_controls(texts):
    pattern = re.compile(r"^-\s*C(\d+)\s+([^:]+):\s*(-?\d+)\s*$", re.M)
    figures = {}
    names = {}
    for text in texts:
        for found in pattern.finditer(text):
            figures[int(found.group(1))] = int(found.group(3))
            names[int(found.group(1))] = found.group(2).strip()
    return figures, names


def s_load(data_dir):
    captures, texts = s_split_sources(s_read_files(data_dir))
    divisions = {}
    for row in s_table(texts, "division,conference,ladder_weight"):
        divisions[row["division"]] = {"conference": row["conference"], "weight": int(row["ladder_weight"])}
    licences = {}
    for row in s_table(texts, "licence_class,segment,levy_rate"):
        licences[row["licence_class"]] = {"segment": row["segment"], "levy": int(row["levy_rate"])}
    registry = {}
    for row in s_table(texts, "entrant_id,squad_key,division,licence_class,is_ranked"):
        registry[row["squad_key"]] = {
            "entrant_id": row["entrant_id"],
            "division": row["division"],
            "licence_class": row["licence_class"],
            "ranked": row["is_ranked"] == "yes",
        }
    figures, names = s_controls(texts)
    policy = {
        "standing_floor": s_scalar(texts, "Standing points floor"),
        "weight_floor": s_scalar(texts, "Combined ladder weight floor"),
        "output_floor": s_scalar(texts, "Combined gross output floor"),
        "weight_prominence": s_scalar(texts, "Ladder weight prominence"),
        "docket_size": s_scalar(texts, "Docket size"),
        "docket_cap": s_scalar(texts, "Docket conference cap"),
        "review_weight": s_scalar(texts, "Review weight"),
        "eligibility_floor": s_scalar(texts, "Eligibility match floor"),
    }
    return {
        "captures": captures,
        "divisions": divisions,
        "licences": licences,
        "registry": registry,
        "controls": figures,
        "control_names": names,
        "policy": policy,
        "tag_roster": s_roster(texts, "Engine tag roster"),
        "window_roster": [int(v) for v in s_roster(texts, "Window depth roster")],
        "sentinel_roster": [int(v, 16) for v in s_roster(texts, "Sentinel roster")],
        "cap_roster": [int(v) for v in s_roster(texts, "Budget cap roster")],
        "threshold_roster": [int(v) for v in s_roster(texts, "Promotion threshold roster")],
        "conferences": sorted({d["conference"] for d in divisions.values()}),
        "segments": sorted({l["segment"] for l in licences.values()}),
    }


# ---------------------------------------------------------------- decoding


def s_stride(nonce, count):
    base = 2 + ((nonce >> 8) % count)
    step = base
    while gcd(step, count) != 1:
        step += 1
    return step


def s_decode(raw, params, roster_dict):
    if len(raw) < 36 or raw[:4] != b"RJNL":
        raise ValueError("envelope magic")
    flags = int.from_bytes(raw[6:8], "little")

    def on(bit):
        return bool((flags >> bit) & 1)

    if int.from_bytes(raw[32:36], "little") != (sum(raw[0:32]) & S_MASK32):
        raise ValueError("header checksum")
    nonce = int.from_bytes(raw[8:12], "big" if on(6) else "little")
    label = params["label"]
    bias = (sum(label.encode("utf-8")) % 11) if on(7) else 0
    declared_segments = int.from_bytes(raw[12:16], "little") - bias
    declared_events = int.from_bytes(raw[16:20], "little") - bias
    base_tick = int.from_bytes(raw[20:24], "little")
    capture_id = int.from_bytes(raw[24:28], "little")
    declared_roster = int.from_bytes(raw[28:32], "little") - bias

    slots = []
    pos = 36
    while pos + 28 <= len(raw):
        if raw[pos : pos + 4] != b"SEG0":
            raise ValueError("segment magic")
        match_id = int.from_bytes(raw[pos + 8 : pos + 12], "little")
        rec_count = int.from_bytes(raw[pos + 12 : pos + 16], "little") - bias
        salt = int.from_bytes(raw[pos + 16 : pos + 20], "little")
        plen = int.from_bytes(raw[pos + 20 : pos + 24], "little")
        pchk = int.from_bytes(raw[pos + 24 : pos + 28], "little")
        payload = raw[pos + 28 : pos + 28 + plen]
        if len(payload) != plen:
            raise ValueError("payload truncated")
        if (sum(payload) & S_MASK32) != pchk:
            raise ValueError("payload checksum")
        if rec_count < 0:
            raise ValueError("record count")
        slots.append({"match_id": match_id, "records": rec_count, "salt": salt, "payload": payload})
        pos += 28 + plen
    if pos != len(raw):
        raise ValueError("trailing bytes")
    count = len(slots)
    if count != declared_segments or count == 0:
        raise ValueError("segment count")
    if on(4):
        offset = nonce % count
        stride = s_stride(nonce, count)
        logical = [(offset + i * stride) % count for i in range(count)]
    else:
        logical = list(range(count))
    order = sorted(range(count), key=lambda i: logical[i])

    window = params["window"]
    sentinel = params["sentinel"]
    records = []
    recent = []
    chain_prev = nonce & S_MASK32
    global_index = 0
    running = 0
    violations = 0
    for mi, slot_index in enumerate(order):
        slot = slots[slot_index]
        salt = slot["salt"]
        effective = (salt ^ (running & S_MASK32)) & S_MASK32 if on(3) else salt
        payload = slot["payload"]
        cursor = 0
        delta = 0
        prev_tick = 0
        segment_sum = 0
        for ri in range(slot["records"]):
            if cursor + 2 > len(payload):
                raise ValueError("frame overflow")
            visible = payload[cursor]
            shadow = payload[cursor + 1]
            true_len = (shadow ^ (effective & 0xFF)) if on(8) else visible
            if not 13 <= true_len <= 23 or cursor + 2 + true_len > len(payload):
                raise ValueError("record length")
            blob = bytearray(payload[cursor + 2 : cursor + 2 + true_len])
            cursor += 2 + true_len
            if on(0):
                stream = hashlib.sha256(
                    ("%s:%d:%d:%d" % (label, slot["match_id"], effective, ri)).encode("utf-8")
                ).digest()
                blob = bytearray(byte ^ stream[i] for i, byte in enumerate(blob))
            if on(9):
                shift = (effective + global_index) % true_len
                blob = blob[true_len - shift :] + blob[: true_len - shift]
            directives = []
            if on(11):
                while len(blob) >= 2 and blob[0] == sentinel:
                    directives.append((blob[1] >> 4, blob[1] & 0x0F))
                    blob = blob[2:]
            if len(blob) < 13:
                raise ValueError("body short")
            kind = blob[0]
            akind = blob[1]
            if kind not in S_VALID_KINDS:
                raise ValueError("event kind")
            if akind == 0:
                try:
                    key = blob[2:5].decode("ascii")
                except UnicodeDecodeError:
                    raise ValueError("attribution text")
                cut = 5
            elif akind == 1:
                if blob[2] >= len(recent):
                    raise ValueError("back reference")
                key = recent[blob[2]]
                cut = 3
            elif akind == 2:
                if roster_dict is None or blob[2] >= len(roster_dict):
                    raise ValueError("dictionary alias")
                key = roster_dict[blob[2]]
                cut = 3
            else:
                raise ValueError("attribution kind")
            unit = int.from_bytes(blob[cut : cut + 2], "little")
            stored_tick = int.from_bytes(blob[cut + 2 : cut + 6], "little")
            stored_value = int.from_bytes(blob[cut + 6 : cut + 10], "little")
            if cut + 10 != len(blob):
                raise ValueError("body length")
            if on(2):
                delta = (delta + stored_tick) & S_MASK32
                tick = (prev_tick + delta) & S_MASK32
            else:
                tick = stored_tick
            if tick < prev_tick:
                violations += 1
            prev_tick = tick
            value = (stored_value ^ chain_prev) & S_MASK32 if on(1) else stored_value
            chain_prev = tick
            if key in recent:
                recent.remove(key)
            recent.append(key)
            while len(recent) > window:
                recent.pop(0)
            final = key
            for op, nib in directives:
                if op == 1:
                    if roster_dict is None or nib >= len(roster_dict):
                        raise ValueError("redirect target")
                    final = roster_dict[nib]
            for op, nib in directives:
                if op == 2:
                    if roster_dict is None or final not in roster_dict:
                        raise ValueError("advance origin")
                    final = roster_dict[(roster_dict.index(final) + nib) % len(roster_dict)]
            segment_sum = (segment_sum + value) & S_MASK32
            records.append(
                {
                    "mi": mi,
                    "seq": global_index,
                    "match_id": slot["match_id"],
                    "kind": kind,
                    "key": final,
                    "wire_key": key,
                    "unit": unit,
                    "tick": tick,
                    "value": value,
                    "directives": directives,
                }
            )
            global_index += 1
        running = (running + segment_sum) & S_MASK32
    return {
        "flags": flags,
        "nonce": nonce,
        "base_tick": base_tick,
        "capture_id": capture_id,
        "declared_segments": declared_segments,
        "declared_events": declared_events,
        "declared_roster": declared_roster,
        "segments": count,
        "records": records,
        "tick_order_violations": violations,
    }


def s_roster_dictionary(baseline):
    seen = []
    for record in baseline["records"]:
        if record["wire_key"] not in seen:
            seen.append(record["wire_key"])
    return seen


# ---------------------------------------------------------------- settlement


def s_settle(keys, direct):
    cyclemin = {}
    for start in keys:
        seen = {}
        cur = start
        step = 0
        while cur in direct:
            if cur in seen:
                cycle = [node for node, idx in seen.items() if idx >= seen[cur]]
                least = min(cycle)
                for node in cycle:
                    cyclemin[node] = least
                break
            seen[cur] = step
            step += 1
            cur = direct[cur]

    def advance(node):
        if node in cyclemin:
            return cyclemin[node]
        return direct.get(node, node)

    owner = {key: key for key in keys}
    passes = 0
    while True:
        passes += 1
        nxt = {key: advance(owner[key]) for key in keys}
        if nxt == owner:
            break
        owner = nxt
    return owner, passes


# ---------------------------------------------------------------- domain work


def s_domain(dec, inputs, cap, threshold):
    divisions = inputs["divisions"]
    licences = inputs["licences"]
    registry = inputs["registry"]
    policy = inputs["policy"]
    roster_dict = inputs["roster_dict"]

    grouped = {}
    for record in dec["records"]:
        grouped.setdefault(record["mi"], []).append(record)

    per_match = []
    transfer_events = []
    for mi in sorted(grouped):
        rows = grouped[mi]
        closes = [r["tick"] for r in rows if r["kind"] == 8]
        close = max(closes) if closes else max(r["tick"] for r in rows)
        concede = {}
        units = {}
        outputs = []
        participants = set()
        redirects = 0
        for row in rows:
            participants.add(row["key"])
            if row["directives"]:
                redirects += 1
            kind = row["kind"]
            if kind == 1:
                units.setdefault(
                    (row["key"], row["unit"]),
                    {"deploy": row["tick"], "rate": row["value"], "elim": None, "disband": None},
                )
            elif kind == 2:
                outputs.append(row)
            elif kind == 3:
                unit = units.get((row["key"], row["unit"]))
                if unit is not None and unit["elim"] is None:
                    unit["elim"] = row["tick"]
            elif kind == 4:
                unit = units.get((row["key"], row["unit"]))
                if unit is not None and unit["disband"] is None:
                    unit["disband"] = row["tick"]
            elif kind == 5:
                if row["key"] not in concede:
                    concede[row["key"]] = row["tick"]
            elif kind == 6:
                transfer_events.append(row)
        gross = {}
        voided = {}
        raw_upkeep = {}
        for (key, _unit), state in units.items():
            end = close
            if state["disband"] is not None and state["disband"] < end:
                end = state["disband"]
            span = end - state["deploy"]
            if span < 0:
                span = 0
            raw_upkeep[key] = raw_upkeep.get(key, 0) + state["rate"] * span
        for row in outputs:
            state = units.get((row["key"], row["unit"]))
            dead = state is not None and state["elim"] is not None and row["tick"] >= state["elim"]
            quit_tick = concede.get(row["key"])
            gone = quit_tick is not None and row["tick"] >= quit_tick
            if dead or gone:
                voided[row["key"]] = voided.get(row["key"], 0) + row["value"]
            else:
                gross[row["key"]] = gross.get(row["key"], 0) + row["value"]
        per_match.append(
            {
                "mi": mi,
                "match_id": rows[0]["match_id"],
                "close": close,
                "participants": participants,
                "concede": concede,
                "gross": gross,
                "voided": voided,
                "raw_upkeep": raw_upkeep,
                "redirects": redirects,
                "output_total": sum(r["value"] for r in outputs),
            }
        )

    cumulative = {}
    for match in per_match:
        match["charged"] = {}
        match["overflow"] = {}
        for key in sorted(match["raw_upkeep"]):
            amount = match["raw_upkeep"][key]
            used = cumulative.get(key, 0)
            room = cap - used
            if room < 0:
                room = 0
            charged = amount if amount < room else room
            match["charged"][key] = charged
            match["overflow"][key] = amount - charged
            cumulative[key] = used + charged

    def weight_of(key):
        row = registry.get(key)
        if row is None:
            return 1
        return divisions.get(row["division"], {"weight": 1})["weight"]

    def levy_of(key):
        row = registry.get(key)
        if row is None:
            return 1
        return licences.get(row["licence_class"], {"levy": 1})["levy"]

    for match in per_match:
        score = {}
        for key in match["participants"]:
            score[key] = match["gross"].get(key, 0) * weight_of(key) - match["charged"].get(key, 0) * levy_of(key)
        match["score"] = score
        eligible = [k for k in match["participants"] if k not in match["concede"]]
        points = {k: 0 for k in match["participants"]}
        if eligible:
            best = max(score[k] for k in eligible)
            winners = sorted(k for k in eligible if score[k] == best)
            if len(winners) == 1:
                points[winners[0]] = 3
            else:
                for key in winners:
                    points[key] = 1
        match["points"] = points

    keys = sorted({k for match in per_match for k in match["participants"]})
    aggregate = {}
    for key in keys:
        aggregate[key] = {
            "squad_key": key,
            "matches": 0,
            "gross_output": 0,
            "charged_upkeep": 0,
            "overflow_upkeep": 0,
            "voided_output": 0,
            "raw_upkeep": 0,
            "standing_points": 0,
            "season_score": 0,
        }
    for key in keys:
        run = 0
        credit = 0
        for match in per_match:
            if key not in match["participants"]:
                continue
            row = aggregate[key]
            row["matches"] += 1
            row["gross_output"] += match["gross"].get(key, 0)
            row["charged_upkeep"] += match["charged"].get(key, 0)
            row["overflow_upkeep"] += match["overflow"].get(key, 0)
            row["voided_output"] += match["voided"].get(key, 0)
            row["raw_upkeep"] += match["raw_upkeep"].get(key, 0)
            row["standing_points"] += match["points"][key]
            row["season_score"] += match["score"][key]
            if match["points"][key] == 3:
                run += 1
                if run > 1:
                    credit += 2
            else:
                run = 0
        aggregate[key]["standing_points"] += credit
        aggregate[key]["streak_credit"] = credit

    direct = {}
    for row in sorted(transfer_events, key=lambda r: (r["mi"], r["seq"])):
        if not roster_dict:
            continue
        target = roster_dict[row["value"] % len(roster_dict)]
        if target == row["key"]:
            continue
        direct[row["key"]] = target
    owner, passes = s_settle(keys, direct)

    for key in keys:
        score = aggregate[key]["season_score"]
        if score >= 3 * threshold:
            tier = "champion"
        elif score >= 2 * threshold:
            tier = "contender"
        elif score >= threshold:
            tier = "qualifier"
        else:
            tier = "provisional"
        aggregate[key]["tier"] = tier

    eligible_pool = sorted(
        key
        for key in keys
        if key in registry and aggregate[key]["matches"] >= policy["eligibility_floor"] and owner.get(key) == key
    )
    best_bracket = None
    satisfied_max = -1
    bracket_verdicts = []
    for combo in combinations(eligible_pool, 3):
        checks = s_bracket_checks(combo, aggregate, registry, divisions, licences, policy, direct)
        hit = sum(1 for _name, ok in checks if ok)
        upkeep = sum(aggregate[k]["charged_upkeep"] for k in combo)
        candidate = (-hit, upkeep, combo)
        if best_bracket is None or candidate < best_bracket:
            best_bracket = candidate
        if hit > satisfied_max:
            satisfied_max = hit
    for combo in combinations(eligible_pool, 3):
        checks = s_bracket_checks(combo, aggregate, registry, divisions, licences, policy, direct)
        hit = sum(1 for _name, ok in checks if ok)
        if hit == satisfied_max:
            bracket_verdicts.append(
                {
                    "members": list(combo),
                    "satisfied": hit,
                    "charged_upkeep": sum(aggregate[k]["charged_upkeep"] for k in combo),
                    "unmet": [name for name, ok in checks if not ok],
                }
            )
    bracket = list(best_bracket[2]) if best_bracket else []

    conference_of_match = {}
    for match in per_match:
        ranked = sorted(
            (k for k in match["participants"] if k in registry),
            key=lambda k: (-match["gross"].get(k, 0), k),
        )
        if ranked:
            conference_of_match[match["match_id"]] = divisions[registry[ranked[0]]["division"]]["conference"]
        else:
            conference_of_match[match["match_id"]] = inputs["conferences"][0]
    review = {
        match["match_id"]: sum(match["voided"].values()) + policy["review_weight"] * match["redirects"]
        for match in per_match
    }
    docket = None
    ids = sorted(review)
    size = policy["docket_size"]
    if len(ids) >= size:
        for combo in combinations(ids, size):
            tally = {}
            for match_id in combo:
                conference = conference_of_match[match_id]
                tally[conference] = tally.get(conference, 0) + 1
            if max(tally.values()) > policy["docket_cap"]:
                continue
            total = sum(review[m] for m in combo)
            candidate = (-total, combo)
            if docket is None or candidate < docket:
                docket = candidate
    docket_ids = list(docket[1]) if docket else []
    docket_index = -docket[0] if docket else 0

    return {
        "per_match": per_match,
        "aggregate": aggregate,
        "keys": keys,
        "owner": owner,
        "direct": direct,
        "settlement_passes": passes,
        "bracket": bracket,
        "bracket_pool": eligible_pool,
        "bracket_candidates": len(list(combinations(eligible_pool, 3))),
        "bracket_satisfied_max": satisfied_max,
        "bracket_verdicts": bracket_verdicts,
        "docket": docket_ids,
        "docket_index": docket_index,
        "review": review,
        "conference_of_match": conference_of_match,
        "cap": cap,
        "threshold": threshold,
    }


def s_bracket_checks(combo, aggregate, registry, divisions, licences, policy, direct):
    rows = [registry[k] for k in combo]
    weights = [divisions[r["division"]]["weight"] for r in rows]
    conferences = [divisions[r["division"]]["conference"] for r in rows]
    segments = {licences[r["licence_class"]]["segment"] for r in rows}
    return [
        ("prominent weight", max(weights) >= policy["weight_prominence"]),
        ("both segments", len(segments) >= 2),
        ("standing floor", all(aggregate[k]["standing_points"] >= policy["standing_floor"] for k in combo)),
        ("conference spread", len(set(conferences)) == 3),
        ("ranked member", any(r["ranked"] for r in rows)),
        ("weight floor", sum(weights) >= policy["weight_floor"]),
        ("no overflow", all(aggregate[k]["overflow_upkeep"] == 0 for k in combo)),
        ("champion member", any(aggregate[k]["tier"] == "champion" for k in combo)),
        ("output floor", sum(aggregate[k]["gross_output"] for k in combo) >= policy["output_floor"]),
        ("no transfer source", all(k not in direct for k in combo)),
    ]


def s_figures(dec, res, inputs):
    aggregate = res["aggregate"]
    tiers = {tier: 0 for tier in S_TIERS}
    for row in aggregate.values():
        tiers[row["tier"]] += 1
    return [
        len(res["per_match"]),
        len(dec["records"]),
        len(res["keys"]),
        sum(row["gross_output"] for row in aggregate.values()),
        sum(row["voided_output"] for row in aggregate.values()),
        sum(row["raw_upkeep"] for row in aggregate.values()),
        sum(row["charged_upkeep"] for row in aggregate.values()),
        sum(row["overflow_upkeep"] for row in aggregate.values()),
        tiers["champion"],
        tiers["qualifier"],
        tiers["provisional"],
    ]


# ---------------------------------------------------------------- resolution


def s_resolve(inputs):
    baseline = s_decode(inputs["captures"][1], {"label": "", "window": 1, "sentinel": 0}, None)
    roster_dict = s_roster_dictionary(baseline)
    inputs["roster_dict"] = roster_dict
    inputs["baseline"] = baseline
    controls = inputs["controls"]
    wanted = [controls[i] for i in range(1, 12)]

    tag_verdicts = []
    accepted = []
    for tag in inputs["tag_roster"]:
        for window in inputs["window_roster"]:
            for sentinel in inputs["sentinel_roster"]:
                params = {"label": tag, "window": window, "sentinel": sentinel}
                entry = {"tag": tag, "window": window, "sentinel": "0x%02X" % sentinel}
                try:
                    dec = s_decode(inputs["captures"][2], params, roster_dict)
                    res = s_domain(dec, inputs, 1 << 40, 1)
                    figures = s_figures(dec, res, inputs)
                except Exception as exc:  # noqa: BLE001
                    entry["verdict"] = "rejected"
                    entry["falsifier"] = "decode halted at " + str(exc)
                    tag_verdicts.append(entry)
                    continue
                mismatch = [i for i in range(6) if figures[i] != wanted[i]]
                if mismatch:
                    entry["verdict"] = "rejected"
                    entry["falsifier"] = "control figure C%d reads %d against %d" % (
                        mismatch[0] + 1,
                        figures[mismatch[0]],
                        wanted[mismatch[0]],
                    )
                else:
                    entry["verdict"] = "accepted"
                    entry["falsifier"] = ""
                    accepted.append(params)
                tag_verdicts.append(entry)
    if len(accepted) != 1:
        raise ValueError("guard parameters underdetermined: %d survivors" % len(accepted))
    params = accepted[0]

    control_dec = s_decode(inputs["captures"][2], params, roster_dict)
    cap_verdicts = []
    cap_accepted = []
    for cap in inputs["cap_roster"]:
        res = s_domain(control_dec, inputs, cap, 1)
        figures = s_figures(control_dec, res, inputs)
        entry = {"cap": cap}
        mismatch = [i for i in (6, 7) if figures[i] != wanted[i]]
        if mismatch:
            entry["verdict"] = "rejected"
            entry["falsifier"] = "control figure C%d reads %d against %d" % (
                mismatch[0] + 1,
                figures[mismatch[0]],
                wanted[mismatch[0]],
            )
        else:
            entry["verdict"] = "accepted"
            entry["falsifier"] = ""
            cap_accepted.append(cap)
        cap_verdicts.append(entry)
    if len(cap_accepted) != 1:
        raise ValueError("budget cap underdetermined: %d survivors" % len(cap_accepted))
    cap = cap_accepted[0]

    threshold_verdicts = []
    threshold_accepted = []
    for threshold in inputs["threshold_roster"]:
        res = s_domain(control_dec, inputs, cap, threshold)
        figures = s_figures(control_dec, res, inputs)
        entry = {"threshold": threshold}
        mismatch = [i for i in (8, 9, 10) if figures[i] != wanted[i]]
        if mismatch:
            entry["verdict"] = "rejected"
            entry["falsifier"] = "control figure C%d reads %d against %d" % (
                mismatch[0] + 1,
                figures[mismatch[0]],
                wanted[mismatch[0]],
            )
        else:
            entry["verdict"] = "accepted"
            entry["falsifier"] = ""
            threshold_accepted.append(threshold)
        threshold_verdicts.append(entry)
    if len(threshold_accepted) != 1:
        raise ValueError("promotion threshold underdetermined: %d survivors" % len(threshold_accepted))
    threshold = threshold_accepted[0]

    control_res = s_domain(control_dec, inputs, cap, threshold)
    return {
        "params": params,
        "cap": cap,
        "threshold": threshold,
        "tag_verdicts": tag_verdicts,
        "cap_verdicts": cap_verdicts,
        "threshold_verdicts": threshold_verdicts,
        "control_dec": control_dec,
        "control_res": control_res,
        "roster_dict": roster_dict,
    }


# ---------------------------------------------------------------- reporting


def s_group_report(kind, name, members, aggregate, registry, divisions, licences):
    entries = []
    tiers = {tier: 0 for tier in S_TIERS}
    for key in sorted(members):
        row = aggregate[key]
        meta = registry[key]
        entries.append(
            {
                "squad_key": key,
                "entrant_id": meta["entrant_id"],
                "division": meta["division"],
                "conference": divisions[meta["division"]]["conference"],
                "licence_class": meta["licence_class"],
                "segment": licences[meta["licence_class"]]["segment"],
                "matches": row["matches"],
                "gross_output": row["gross_output"],
                "charged_upkeep": row["charged_upkeep"],
                "overflow_upkeep": row["overflow_upkeep"],
                "voided_output": row["voided_output"],
                "standing_points": row["standing_points"],
                "streak_credit": row["streak_credit"],
                "season_score": row["season_score"],
                "tier": row["tier"],
            }
        )
        tiers[row["tier"]] += 1
    report = {
        "group_kind": kind,
        "group_name": name,
        "entrants": len(entries),
        "entries": entries,
        "tier_counts": tiers,
        "gross_output": sum(e["gross_output"] for e in entries),
        "charged_upkeep": sum(e["charged_upkeep"] for e in entries),
        "overflow_upkeep": sum(e["overflow_upkeep"] for e in entries),
        "voided_output": sum(e["voided_output"] for e in entries),
        "standing_points": sum(e["standing_points"] for e in entries),
        "season_score": sum(e["season_score"] for e in entries),
    }
    if kind == "conference":
        report["divisions"] = sorted({e["division"] for e in entries})
    else:
        report["licence_classes"] = sorted({e["licence_class"] for e in entries})
    return report


def s_reports(dec, res, inputs):
    aggregate = res["aggregate"]
    registry = inputs["registry"]
    divisions = inputs["divisions"]
    licences = inputs["licences"]
    known = [k for k in res["keys"] if k in registry]
    unknown = sorted(k for k in res["keys"] if k not in registry)
    absent = sorted(k for k in registry if k not in res["keys"])

    conference_reports = {}
    for conference in inputs["conferences"]:
        members = [k for k in known if divisions[registry[k]["division"]]["conference"] == conference]
        conference_reports[conference] = s_group_report(
            "conference", conference, members, aggregate, registry, divisions, licences
        )
    segment_reports = {}
    for segment in inputs["segments"]:
        members = [k for k in known if licences[registry[k]["licence_class"]]["segment"] == segment]
        segment_reports[segment] = s_group_report(
            "segment", segment, members, aggregate, registry, divisions, licences
        )

    tiers = {tier: 0 for tier in S_TIERS}
    for key in res["keys"]:
        tiers[aggregate[key]["tier"]] += 1
    gross_total = sum(aggregate[k]["gross_output"] for k in res["keys"])
    voided_total = sum(aggregate[k]["voided_output"] for k in res["keys"])
    charged_total = sum(aggregate[k]["charged_upkeep"] for k in res["keys"])
    overflow_total = sum(aggregate[k]["overflow_upkeep"] for k in res["keys"])
    raw_total = sum(aggregate[k]["raw_upkeep"] for k in res["keys"])
    peak = sorted(res["keys"], key=lambda k: (-aggregate[k]["gross_output"], k))[0]

    summary = {
        "capture_id": dec["capture_id"],
        "matches_opened": len(res["per_match"]),
        "events_decoded": len(dec["records"]),
        "squads_observed": len(res["keys"]),
        "registered_entrants": len(known),
        "gross_output_total": gross_total,
        "voided_output_total": voided_total,
        "charged_upkeep_total": charged_total,
        "overflow_upkeep_total": overflow_total,
        "season_score_total": sum(aggregate[k]["season_score"] for k in res["keys"]),
        "standing_points_total": sum(aggregate[k]["standing_points"] for k in res["keys"]),
        "settlement_passes": res["settlement_passes"],
        "tier_counts": tiers,
        "finals_bracket": res["bracket"],
        "review_docket": res["docket"],
        "peak_output_key": peak,
        "roster_dictionary_size": len(inputs["roster_dict"]),
    }

    kind_ledger = {}
    for record in dec["records"]:
        name = S_KIND_NAMES[record["kind"]]
        kind_ledger[name] = kind_ledger.get(name, 0) + 1
    output_seen = sum(m["output_total"] for m in res["per_match"])
    data_quality = {
        "declared_event_residual": dec["declared_events"] - len(dec["records"]),
        "declared_segment_residual": dec["declared_segments"] - len(res["per_match"]),
        "declared_roster_residual": dec["declared_roster"] - len(res["keys"]),
        "attribution_residual": gross_total - sum(r["gross_output"] for r in conference_reports.values()),
        "output_residual": output_seen - (gross_total + voided_total),
        "upkeep_residual": raw_total - (charged_total + overflow_total),
        "tick_order_violations": dec["tick_order_violations"],
        "kind_ledger": kind_ledger,
        "unregistered_squads": unknown,
        "absent_entrants": absent,
        "redirected_records": sum(1 for r in dec["records"] if r["directives"]),
    }
    return summary, conference_reports, segment_reports, data_quality


def s_audit(resolution, inputs, dec, res, summary):
    control_figures = s_figures(resolution["control_dec"], resolution["control_res"], inputs)
    comparison = []
    for index in range(1, 12):
        comparison.append(
            {
                "control": "C%d" % index,
                "figure": inputs["control_names"].get(index, "C%d" % index),
                "published": inputs["controls"][index],
                "reproduced": control_figures[index - 1],
                "agreement": inputs["controls"][index] == control_figures[index - 1],
            }
        )
    flags = dec["flags"]
    active = [name for bit, name in enumerate(S_GUARD_NAMES) if (flags >> bit) & 1]
    return {
        "guard_parameter_candidates": len(resolution["tag_verdicts"]),
        "guard_parameter_verdicts": resolution["tag_verdicts"],
        "guard_parameters": {
            "engine_tag": resolution["params"]["label"],
            "window_depth": resolution["params"]["window"],
            "sentinel_byte": "0x%02X" % resolution["params"]["sentinel"],
        },
        "upkeep_cap_candidates": len(resolution["cap_verdicts"]),
        "upkeep_cap_verdicts": resolution["cap_verdicts"],
        "upkeep_cap": resolution["cap"],
        "promotion_threshold_candidates": len(resolution["threshold_verdicts"]),
        "promotion_threshold_verdicts": resolution["threshold_verdicts"],
        "promotion_threshold": resolution["threshold"],
        "settlement_passes": res["settlement_passes"],
        "transfer_edges": [{"source": k, "target": v} for k, v in sorted(res["direct"].items())],
        "effective_owners": [{"squad_key": k, "owner": res["owner"][k]} for k in res["keys"] if res["owner"][k] != k],
        "bracket_pool": res["bracket_pool"],
        "bracket_candidates": res["bracket_candidates"],
        "bracket_satisfied_max": res["bracket_satisfied_max"],
        "bracket_verdicts": res["bracket_verdicts"],
        "finals_bracket": res["bracket"],
        "review_docket": res["docket"],
        "review_index_total": res["docket_index"],
        "docket_conferences": [res["conference_of_match"][m] for m in res["docket"]],
        "calibration_comparison": comparison,
        "guard_flags_live": active,
        "roster_dictionary_size": len(inputs["roster_dict"]),
        "roster_dictionary": inputs["roster_dict"],
    }


def s_dump(path, obj):
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(obj, indent=2, sort_keys=True))
        handle.write("\n")


def s_run(data_dir, out_dir):
    inputs = s_load(data_dir)
    resolution = s_resolve(inputs)
    dec = s_decode(inputs["captures"][3], resolution["params"], resolution["roster_dict"])
    res = s_domain(dec, inputs, resolution["cap"], resolution["threshold"])
    summary, conference_reports, segment_reports, data_quality = s_reports(dec, res, inputs)
    audit = s_audit(resolution, inputs, dec, res, summary)
    os.makedirs(out_dir, exist_ok=True)
    s_dump(os.path.join(out_dir, "summary.json"), summary)
    s_dump(os.path.join(out_dir, "data_quality.json"), data_quality)
    s_dump(os.path.join(out_dir, "policy_audit.json"), audit)
    for conference, report in conference_reports.items():
        s_dump(os.path.join(out_dir, "region_%s.json" % conference), report)
    for segment, report in segment_reports.items():
        s_dump(os.path.join(out_dir, "segment_%s.json" % segment), report)
    return {
        "summary": summary,
        "data_quality": data_quality,
        "policy_audit": audit,
        "conferences": conference_reports,
        "segments": segment_reports,
    }


def _entry(argv):
    if len(argv) != 3:
        raise SystemExit("usage: derive_report.py DATA_DIR OUT_DIR")
    s_run(argv[1], argv[2])
    return 0


if __name__ == "__main__":
    import sys as _sys

    raise SystemExit(_entry(_sys.argv))
