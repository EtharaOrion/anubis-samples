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


# Corpus writer used by the verifier.

import hashlib
import json
import os
import random
import struct

W_MASK32 = 0xFFFFFFFF

W_KEY_POOL = [
    "AKR", "BLZ", "CDR", "DVN", "EMB", "FLX", "GRT", "HVN", "IBX", "JDE",
    "KRN", "LMX", "MRA", "NVL", "ORC", "PYX", "QRM", "RVN", "SLT", "TDR",
    "UMB", "VLC", "WRN", "XCT", "YRD", "ZPH", "AXL", "BQT", "CVE", "DRF",
    "EGN", "FTH",
]

W_TAG_ROSTER = [
    "orion-fold", "wren-cadence", "tessel-drift", "halcyon-mark",
    "vector-plume", "umbral-stack", "quill-lattice", "nimbus-gable",
]
W_WINDOW_ROSTER = [4, 6, 8, 10]
W_SENTINEL_ROSTER = [0x1A, 0x1D, 0x20, 0x7F]
W_CAP_ROSTER = [600, 800, 1000, 1200, 1400, 1600]
W_THRESHOLD_ROSTER = [120, 180, 240, 300, 360, 420]

W_DIVISIONS = [
    ("D1", "North", 3),
    ("D2", "South", 2),
    ("D3", "West", 4),
    ("D4", "East", 2),
    ("D5", "North", 1),
    ("D6", "South", 3),
    ("D7", "West", 2),
    ("D8", "East", 5),
]
W_LICENCES = [
    ("LC1", "commercial", 2),
    ("LC2", "public", 1),
    ("LC3", "commercial", 3),
    ("LC4", "public", 2),
]


W_TPL = {
    "tags": "Engine tag roster: `orion-fold`, `wren-cadence`, `tessel-drift`, `halcyon-mark`, `vector-plume`, `umbral-stack`, `quill-lattice`, `nimbus-gable`",
    "windows": "Window depth roster: `4`, `6`, `8`, `10`",
    "sentinels": "Sentinel roster: `0x1A`, `0x1D`, `0x20`, `0x7F`",
    "caps": "Budget cap roster: `420`, `560`, `700`, `840`, `980`, `1120`",
    "thresholds": "Promotion threshold roster: `180`, `260`, `340`, `420`, `500`, `580`",
    "eligibility_floor": "Eligibility match floor: `2`",
    "weight_prominence": "Ladder weight prominence: `4`",
    "standing_floor": "Standing points floor: `8`",
    "weight_floor": "Combined ladder weight floor: `7`",
    "output_floor": "Combined gross output floor: `900`",
    "review_weight": "Review weight: `25`",
    "docket_size": "Docket size: `5`",
    "docket_cap": "Docket conference cap: `2`",
}


def w_spread(candidates, evaluate, count=6):
    distinct = []
    seen = set()
    for value in candidates:
        signature = evaluate(value)
        if signature in seen:
            continue
        seen.add(signature)
        distinct.append(value)
    if len(distinct) < count:
        raise ValueError("roster underdetermined: only %d separable figures" % len(distinct))
    picks = sorted({distinct[round(i * (len(distinct) - 1) / (count - 1))] for i in range(count)})
    while len(picks) < count:
        for value in distinct:
            if value not in picks:
                picks.append(value)
                break
        picks = sorted(set(picks))
    return sorted(picks)[:count]


def w_swap(text, template, replacement):
    if template not in text:
        raise ValueError("template line missing: " + template)
    return text.replace(template, replacement)


def w_default_config():
    return {
        "label": "tessel-drift",
        "window": 6,
        "sentinel": 0x1D,
        "cap_index": 4,
        "threshold_index": 4,
        "seed": 20260417,
        "baseline_matches": 6,
        "control_matches": 5,
        "season_matches": 18,
        "baseline_pool": 16,
        "control_pool": 20,
        "season_pool": 28,
        "season_skip": (17, 19),
        "registered": 24,
        "divisions": list(W_DIVISIONS),
        "licences": list(W_LICENCES),
        "tag_roster": list(W_TAG_ROSTER),
        "window_roster": list(W_WINDOW_ROSTER),
        "sentinel_roster": list(W_SENTINEL_ROSTER),
        "cap_roster": list(W_CAP_ROSTER),
        "threshold_roster": list(W_THRESHOLD_ROSTER),
        "flags": 0x0FFF,
        "close_lo": 90,
        "close_hi": 170,
        "units_lo": 2,
        "units_hi": 4,
        "out_lo": 2,
        "out_hi": 4,
        "rate_lo": 1,
        "rate_hi": 3,
        "val_lo": 12,
        "val_hi": 90,
        "elim_rate": 0.40,
        "disband_rate": 0.28,
        "concede_rate": 0.30,
        "squads_lo": 3,
        "squads_hi": 5,
        "p_backref": 0.55,
        "p_alias": 0.35,
        "directives_per_match": 2,
        "control_directives_per_match": 3,
        "policy": {
            "standing_floor": 3,
            "weight_floor": 7,
            "output_floor": 900,
            "weight_prominence": 4,
            "docket_size": 5,
            "docket_cap": 2,
            "review_weight": 25,
            "eligibility_floor": 2,
        },
        "season_transfers": [
            (20, 9), (9, 8), (8, 7), (7, 6), (6, 5),
            (1, 2), (2, 3), (3, 1), (21, 1),
        ],
        "control_transfers": [(11, 4), (4, 3), (12, 11)],
    }


W_MUTATIONS = [
    "tie_config", "cap_push", "empty_group", "boundary_shift", "add_entity",
    "counterfactual_flip", "scale", "contradict", "alias_chain", "nonapply",
    "cycle", "partition", "sentinel_heavy", "fresh_base",
]


def w_mutation(name):
    cfg = w_default_config()
    cfg["seed"] = 40000019 + sum(ord(c) for c in name) * 7919
    if name == "tie_config":
        cfg.update(val_lo=48, val_hi=48, rate_lo=2, rate_hi=2, elim_rate=0.0, disband_rate=0.0,
                   concede_rate=0.0, label="wren-cadence", window=4, sentinel=0x20, cap_index=4,
                   threshold_index=1, season_matches=16)
    elif name == "cap_push":
        cfg.update(rate_lo=4, rate_hi=7, close_lo=150, close_hi=240, label="halcyon-mark",
                   window=8, sentinel=0x7F, cap_index=1, threshold_index=0)
    elif name == "empty_group":
        cfg.update(registry_divisions=["D1", "D2", "D3", "D5", "D6", "D7"], label="orion-fold",
                   window=10, sentinel=0x1A, cap_index=0, threshold_index=5)
    elif name == "boundary_shift":
        cfg.update(cap_index=3, threshold_index=4, label="vector-plume", window=6, sentinel=0x1D)
    elif name == "add_entity":
        cfg.update(divisions=list(W_DIVISIONS) + [("D9", "North", 4), ("D10", "East", 1)],
                   licences=list(W_LICENCES) + [("LC5", "commercial", 1)],
                   season_pool=32, registered=28, control_pool=22, season_matches=20,
                   label="umbral-stack", window=8, sentinel=0x20, cap_index=5, threshold_index=3)
    elif name == "counterfactual_flip":
        cfg.update(elim_rate=0.95, disband_rate=0.05, label="quill-lattice", window=4,
                   sentinel=0x7F, cap_index=5, threshold_index=1)
    elif name == "scale":
        cfg.update(season_matches=26, control_matches=8, baseline_matches=8, out_hi=5,
                   label="nimbus-gable", window=10, sentinel=0x1A, cap_index=2, threshold_index=5)
    elif name == "contradict":
        cfg.update(registered=28, season_pool=32, season_skip=(17, 19, 21),
                   label="orion-fold", window=6, sentinel=0x20, cap_index=1, threshold_index=2)
    elif name == "alias_chain":
        cfg.update(p_alias=0.9, p_backref=0.85, label="tessel-drift", window=10, sentinel=0x1A,
                   cap_index=4, threshold_index=3,
                   season_transfers=[(20, 9), (9, 8), (8, 7), (7, 6), (6, 5), (5, 4), (4, 3),
                                     (21, 2), (22, 1)])
    elif name == "nonapply":
        cfg.update(season_transfers=[], directives_per_match=0, concede_rate=0.0, elim_rate=0.0,
                   label="halcyon-mark", window=4, sentinel=0x1D, cap_index=4, threshold_index=0)
    elif name == "cycle":
        cfg.update(season_transfers=[(1, 2), (2, 3), (3, 4), (4, 5), (5, 1), (20, 1), (21, 3),
                                     (9, 8), (8, 7)],
                   label="wren-cadence", window=8, sentinel=0x1A, cap_index=0, threshold_index=4)
    elif name == "partition":
        cfg.update(registry_divisions=["D1", "D2", "D3"], registry_licences=["LC2"],
                   label="vector-plume", window=10, sentinel=0x7F, cap_index=2, threshold_index=1)
    elif name == "sentinel_heavy":
        cfg.update(directives_per_match=6, control_directives_per_match=6, label="umbral-stack",
                   window=6, sentinel=0x1D, cap_index=5, threshold_index=5)
    elif name == "fresh_base":
        cfg.update(label="quill-lattice", window=10, sentinel=0x7F, cap_index=3, threshold_index=0,
                   season_matches=22, control_matches=7, baseline_matches=7, baseline_pool=16,
                   control_pool=19, season_pool=30, registered=26, season_skip=(18, 23),
                   squads_lo=4, squads_hi=6)
    else:
        raise ValueError("unknown mutation " + name)
    return cfg


def w_pool(cfg, size, skip=()):
    return [W_KEY_POOL[i] for i in range(size) if i not in skip]


def w_squads_for(cfg, rng, pool, index, count):
    picked = []
    step = 0
    while len(picked) < count:
        key = pool[(index * 3 + step) % len(pool)]
        if key not in picked:
            picked.append(key)
        step += 1
    return picked


def w_build_matches(cfg, rng, pool, count, first_id, transfers, roster_dict, directives_per_match):
    plans = []
    for index in range(count):
        size = rng.randrange(cfg["squads_lo"], cfg["squads_hi"] + 1)
        plans.append(w_squads_for(cfg, rng, pool, index, size))
    pending = []
    for source_index, target_index in transfers:
        source = W_KEY_POOL[source_index]
        target = W_KEY_POOL[target_index]
        placed = False
        for index, squads in enumerate(plans):
            if source in squads and not any(p[0] == index for p in pending if p[1] == source):
                pending.append((index, source, target))
                placed = True
                break
        if not placed:
            plans[0].append(source)
            pending.append((0, source, target))
    matches = []
    for index, squads in enumerate(plans):
        close = rng.randrange(cfg["close_lo"], cfg["close_hi"])
        events = []
        for key in squads:
            units = rng.randrange(cfg["units_lo"], cfg["units_hi"] + 1)
            for unit in range(1, units + 1):
                deploy = rng.randrange(0, max(2, close // 3))
                rate = rng.randrange(cfg["rate_lo"], cfg["rate_hi"] + 1)
                events.append({"kind": 1, "key": key, "unit": unit, "tick": deploy, "value": rate, "dirs": []})
                for _ in range(rng.randrange(cfg["out_lo"], cfg["out_hi"] + 1)):
                    events.append(
                        {
                            "kind": 2,
                            "key": key,
                            "unit": unit,
                            "tick": rng.randrange(deploy + 1, close),
                            "value": rng.randrange(cfg["val_lo"], cfg["val_hi"] + 1),
                            "dirs": [],
                        }
                    )
                if rng.random() < cfg["elim_rate"]:
                    events.append(
                        {
                            "kind": 3,
                            "key": key,
                            "unit": unit,
                            "tick": rng.randrange(deploy + 1, close),
                            "value": 0,
                            "dirs": [],
                        }
                    )
                if rng.random() < cfg["disband_rate"]:
                    events.append(
                        {
                            "kind": 4,
                            "key": key,
                            "unit": unit,
                            "tick": rng.randrange(deploy + 1, close),
                            "value": 0,
                            "dirs": [],
                        }
                    )
        if rng.random() < cfg["concede_rate"]:
            key = squads[rng.randrange(len(squads))]
            events.append(
                {
                    "kind": 5,
                    "key": key,
                    "unit": 0,
                    "tick": rng.randrange(max(1, close // 2), close),
                    "value": 0,
                    "dirs": [],
                }
            )
        for slot, source, target in pending:
            if slot != index:
                continue
            if roster_dict and target in roster_dict:
                events.append(
                    {
                        "kind": 6,
                        "key": source,
                        "unit": 0,
                        "tick": rng.randrange(1, close),
                        "value": roster_dict.index(target),
                        "dirs": [],
                    }
                )
        events.append({"kind": 8, "key": squads[0], "unit": 0, "tick": close, "value": 0, "dirs": []})
        events.sort(key=lambda e: (e["tick"], e["kind"], e["key"], e["unit"], e["value"]))
        if roster_dict and directives_per_match:
            span = min(16, len(roster_dict))
            outputs = [e for e in events if e["kind"] == 2]
            rng.shuffle(outputs)
            for event in outputs[:directives_per_match]:
                dirs = [(1, rng.randrange(span))]
                if rng.random() < 0.7:
                    dirs.append((2, rng.randrange(1, span)))
                event["dirs"] = dirs
        matches.append({"match_id": first_id + index, "events": events, "squads": squads})
    return matches


def w_wire_order(dirs):
    return [d for d in dirs if d[0] == 2] + [d for d in dirs if d[0] == 1]


def w_stride(nonce, count):
    from math import gcd

    base = 2 + ((nonce >> 8) % count)
    step = base
    while gcd(step, count) != 1:
        step += 1
    return step


def w_encode(cfg, capture_id, matches, flags, nonce, base_tick, declared_roster, roster_dict, rng):
    def on(bit):
        return bool((flags >> bit) & 1)

    label = cfg["label"]
    window = cfg["window"]
    sentinel = cfg["sentinel"]
    bias = (sum(label.encode("utf-8")) % 11) if on(7) else 0
    count = len(matches)
    salts = [rng.randrange(1, W_MASK32) for _ in range(count)]
    recent = []
    chain_prev = nonce & W_MASK32
    global_index = 0
    running = 0
    segments = []
    total_events = 0
    for li, match in enumerate(matches):
        salt = salts[li]
        effective = (salt ^ (running & W_MASK32)) & W_MASK32 if on(3) else salt
        payload = bytearray()
        delta = 0
        prev_tick = 0
        segment_sum = 0
        for ri, event in enumerate(match["events"]):
            key = event["key"]
            dirs = event["dirs"] if on(11) else []
            akind = 0
            keybytes = key.encode("ascii")
            if on(5) and len(recent) == window and key in recent and rng.random() < cfg["p_backref"]:
                akind = 1
                keybytes = bytes([recent.index(key)])
            elif on(10) and roster_dict and key in roster_dict and rng.random() < cfg["p_alias"]:
                akind = 2
                keybytes = bytes([roster_dict.index(key)])
            tick = event["tick"]
            if on(2):
                step = (tick - prev_tick) & W_MASK32
                stored_tick = (step - delta) & W_MASK32
                delta = step
            else:
                stored_tick = tick & W_MASK32
            prev_tick = tick
            value = event["value"] & W_MASK32
            stored_value = (value ^ chain_prev) & W_MASK32 if on(1) else value
            chain_prev = tick
            body = (
                bytes([event["kind"], akind])
                + keybytes
                + struct.pack("<H", event["unit"])
                + struct.pack("<I", stored_tick)
                + struct.pack("<I", stored_value)
            )
            escape = b""
            for op, nib in w_wire_order(dirs):
                escape += bytes([sentinel, (op << 4) | nib])
            blob = bytearray(escape + body)
            true_len = len(blob)
            if on(9):
                shift = (effective + global_index) % true_len
                blob = blob[shift:] + blob[:shift]
            if on(0):
                stream = hashlib.sha256(
                    ("%s:%d:%d:%d" % (label, match["match_id"], effective, ri)).encode("utf-8")
                ).digest()
                blob = bytearray(byte ^ stream[i] for i, byte in enumerate(blob))
            if on(8):
                shadow = true_len ^ (effective & 0xFF)
                visible = (true_len + 5 + (global_index % 7)) % 256
                if visible == true_len:
                    visible = (visible + 1) % 256
            else:
                shadow = 0
                visible = true_len
            payload += bytes([visible, shadow]) + bytes(blob)
            if key in recent:
                recent.remove(key)
            recent.append(key)
            while len(recent) > window:
                recent.pop(0)
            segment_sum = (segment_sum + value) & W_MASK32
            global_index += 1
        payload += bytes(rng.randrange(256) for _ in range(3 + (salt % 5)))
        running = (running + segment_sum) & W_MASK32
        segments.append(
            {
                "match_id": match["match_id"],
                "records": len(match["events"]),
                "salt": salt,
                "payload": bytes(payload),
            }
        )
        total_events += len(match["events"])

    if on(4):
        offset = nonce % count
        stride = w_stride(nonce, count)
        placement = [(offset + i * stride) % count for i in range(count)]
    else:
        placement = list(range(count))

    header = bytearray(36)
    header[0:4] = b"RJNL"
    header[4] = 2
    header[5] = 0
    header[6:8] = struct.pack("<H", flags)
    header[8:12] = struct.pack(">I" if on(6) else "<I", nonce)
    header[12:16] = struct.pack("<I", count + bias)
    header[16:20] = struct.pack("<I", total_events + bias)
    header[20:24] = struct.pack("<I", base_tick)
    header[24:28] = struct.pack("<I", capture_id)
    header[28:32] = struct.pack("<I", declared_roster + bias)
    header[32:36] = struct.pack("<I", sum(header[0:32]) & W_MASK32)

    out = bytearray(header)
    for slot in range(count):
        segment = segments[placement[slot]]
        out += b"SEG0"
        out += struct.pack("<I", slot)
        out += struct.pack("<I", segment["match_id"])
        out += struct.pack("<I", segment["records"] + bias)
        out += struct.pack("<I", segment["salt"])
        out += struct.pack("<I", len(segment["payload"]))
        out += struct.pack("<I", sum(segment["payload"]) & W_MASK32)
        out += segment["payload"]
    return bytes(out)


# ---------------------------------------------------------------- documents


def w_tables(cfg):
    division_rows = ["division,conference,ladder_weight"]
    for name, conference, weight in cfg["divisions"]:
        division_rows.append("%s,%s,%d" % (name, conference, weight))
    licence_rows = ["licence_class,segment,levy_rate"]
    for name, segment, levy in cfg["licences"]:
        licence_rows.append("%s,%s,%d" % (name, segment, levy))
    registry_rows = ["entrant_id,squad_key,division,licence_class,is_ranked"]
    divisions = cfg.get("registry_divisions") or [d[0] for d in cfg["divisions"]]
    licences = cfg.get("registry_licences") or [l[0] for l in cfg["licences"]]
    for index in range(cfg["registered"]):
        key = W_KEY_POOL[index]
        registry_rows.append(
            "E%03d,%s,%s,%s,%s"
            % (
                index + 1,
                key,
                divisions[index % len(divisions)],
                licences[(index * 3) % len(licences)],
                "yes" if index % 3 != 1 else "no",
            )
        )
    return "\n".join(division_rows) + "\n", "\n".join(licence_rows) + "\n", "\n".join(registry_rows) + "\n"


def w_backtick_list(values):
    return ", ".join("`%s`" % v for v in values)


def w_docs(cfg):
    docs = {}
    docs["ladder-brief.md"] = """# Ladder brief

The tactics league seals one binary journal for every capture it keeps. A capture holds a
run of match segments; a segment holds the ordered record stream of a single match. The
league settles a season from the sealed material alone.

Three captures accompany this brief. Their roles are told apart by the capture identifier
carried in the envelope, never by the name of the file that holds them.

Event kinds carried in a record body:

| kind | meaning | value field |
| --- | --- | --- |
| 1 | a unit is deployed | upkeep rate charged for each tick the unit stays on the field |
| 2 | a unit reports output | points reported |
| 3 | a unit is eliminated | unused |
| 4 | a unit is disbanded | unused |
| 5 | a squad concedes the match | unused |
| 6 | a squad hands its entry to another squad | position of the receiving squad in the roster dictionary |
| 8 | the match closes | unused |

A record carries the attribution key of the squad it belongs to, the unit ordinal inside
that squad, the tick at which the event happened and a value whose reading depends on the
kind above. Unit ordinals are scoped to the squad and to the match.

The season report is settled for four conferences and for two entrant segments. The
conference of a squad follows from its division, and the segment of a squad follows from
its licence class.
"""

    docs["journal-envelope.md"] = """# Journal envelope

Every capture opens with a thirty six byte envelope.

| offset | width | field |
| --- | --- | --- |
| 0 | 4 | the ASCII marker RJNL |
| 4 | 1 | envelope version |
| 5 | 1 | reserved, always zero |
| 6 | 2 | guard register, unsigned, little endian |
| 8 | 4 | capture nonce, unsigned |
| 12 | 4 | declared segment count, unsigned little endian |
| 16 | 4 | declared record count, unsigned little endian |
| 20 | 4 | season base tick, unsigned little endian |
| 24 | 4 | capture identifier, unsigned little endian |
| 28 | 4 | declared roster size, unsigned little endian |
| 32 | 4 | envelope checksum, unsigned little endian |

The envelope checksum is the sum of the first thirty two bytes of the envelope taken
modulo two to the power thirty two. Because it sums bytes rather than words it agrees with
the envelope whichever way round the multi byte fields were laid down.

The capture identifier ends in 1 for the reference capture that carries the base envelope,
in 2 for the control slice, and in 3 for the sealed season.

Each segment that follows the envelope opens with a twenty eight byte segment envelope:
the ASCII marker SEG0, the storage slot of the segment as an unsigned little endian word,
the match identifier as an unsigned little endian word, the declared record count of the
segment as an unsigned little endian word, the segment salt as an unsigned little endian
word, the payload width in bytes as an unsigned little endian word, and the payload
checksum as an unsigned little endian word. The payload checksum is the sum of the payload
bytes modulo two to the power thirty two.

The payload holds the declared number of record frames and then trailing filler. The width
of the filler is three bytes more than the segment salt taken modulo five, and its content
carries no meaning. Nothing marks where the records stop; only the declared count does.

A record frame opens with a visible width byte and a shadow width byte and then carries a
record blob of the true width. When the shadow guard is idle the visible byte holds the
true width and the shadow byte is zero.

A decoded record blob holds, in order, the event kind as one byte, the attribution mode as
one byte, the attribution field, the unit ordinal as an unsigned little endian half word,
the tick field as an unsigned little endian word and the value field as an unsigned little
endian word. Attribution mode 0 carries three ASCII bytes naming the squad. Attribution
mode 1 carries a single byte holding a back reference. Attribution mode 2 carries a single
byte holding a roster dictionary position.

Guards that touch the envelope:

The endian guard lays the capture nonce down most significant byte first while every other
envelope word stays least significant byte first.

The count bias guard stores every declared count of a capture, in the envelope and in each
segment envelope alike, offset upward by a constant. The constant is the sum of the byte
values of the engine tag taken modulo eleven.

The shadow guard makes the visible width byte a decoy. The true width of the blob is the
shadow byte combined by exclusive or with the low byte of the effective segment salt.
"""

    docs["replay-guards.md"] = """# Replay guards

The guard register in the envelope is a bit field. A set bit means the named guard was
active when the capture was sealed.

| bit | guard |
| --- | --- |
| 0 | keystream |
| 1 | chain |
| 2 | delta2 |
| 3 | driftkey |
| 4 | segperm |
| 5 | backref |
| 6 | endian |
| 7 | countbias |
| 8 | shadow |
| 9 | rotate |
| 10 | alias |
| 11 | escape |

Three quantities govern how the guards resolve. None of them is written down anywhere in
this material. Each is drawn from the roster published beside it here.

Engine tag roster: `orion-fold`, `wren-cadence`, `tessel-drift`, `halcyon-mark`, `vector-plume`, `umbral-stack`, `quill-lattice`, `nimbus-gable`

Window depth roster: `4`, `6`, `8`, `10`

Sentinel roster: `0x1A`, `0x1D`, `0x20`, `0x7F`

The keystream guard masks a record blob by exclusive or against the SHA-256 digest of the
engine tag, the match identifier, the effective segment salt and the ordinal of the record
inside its segment, written as decimal text and joined by single colons, truncated to the
width of the blob.

The rotate guard turns a record blob left. The turn is the sum of the effective segment
salt and the ordinal of the record inside the whole capture, taken modulo the true width of
the blob. The turn moves bytes without changing their sum, so the payload checksum agrees
either way.

The escape guard lets a record carry directive pairs ahead of its body. Every pair is the
sentinel byte followed by a directive byte whose high nibble is the operation and whose low
nibble is the operand. Operation 1 redirects the attribution of the record to the roster
dictionary entry at the operand position. Operation 2 advances the attribution of the
record by the operand, counted forward around the roster dictionary from the position the
record already holds. A record carries at most two pairs. No event kind ever collides with a
sentinel value.

The back reference guard replaces the attribution field with a position inside a rolling
list of recently seen squads. The list is as deep as the window depth, holds each squad at
most once, and is ordered from the least recently seen entry to the most recently seen one.
Positions count from the least recently seen end. The list is refreshed after every record
using the attribution key as written on the wire, before any escape directive has been
weighed, and a squad already in the list moves to the most recent end rather than being
added twice.

The alias guard replaces the attribution field with a position in the roster dictionary.
"""

    docs["stream-continuity.md"] = """# Stream continuity

Four guards make a capture readable only from its first record forward.

The segment permutation guard stores segments away from the order in which they happened.
The storage slot at position i holds the segment whose logical position is the capture
nonce plus i times a step, taken modulo the number of segments in the capture. The step is
the smallest integer that is at least two more than the nonce shifted right by eight bits
and reduced modulo the number of segments, and that shares no factor with the number of
segments. The slot word inside a segment envelope repeats the storage slot and says nothing
about logical position.

The delta guard stores the tick field as a second order difference. Within a segment the
first difference is the gap from the previous tick, the second difference is the gap from
the previous first difference, and both run from zero at the head of each segment. Ticks
inside a segment never move backwards.

The chain guard masks the value field by exclusive or against the tick of the record that
was decoded immediately before it. The chain runs across segment boundaries in logical
order for the whole capture, and the very first record of a capture is masked against the
capture nonce.

The drift key guard makes the effective segment salt differ from the salt written in the
segment envelope. The effective salt is the written salt combined by exclusive or with the
running sum, modulo two to the power thirty two, of the value fields of every record in
every logically earlier segment of the same capture.

All word arithmetic in this document is unsigned and modulo two to the power thirty two.
"""

    docs["roster-continuity.md"] = """# Roster continuity

The roster dictionary is the list of distinct attribution keys as written on the wire in
the reference capture, kept in the order in which each key is first met when that capture
is read in logical order. It is a property of the corpus rather than of any single capture,
and the sealed season and the control slice both resolve their dictionary positions against
it.

An entry hand over is recorded as an event of kind 6. Its value field holds the position of
the receiving squad in the roster dictionary, reduced modulo the size of the dictionary.
When a squad records more than one hand over, the one that is read last in logical order is
the one that stands.

Following the hand overs to their end gives the effective owner of every squad. A squad
that never hands its entry away owns itself. When a set of hand overs closes on itself so
that following them returns to where it began, every squad on that closed set settles on
the squad of that set whose key sorts first.

The settlement is reached by repeated sweeps. A sweep replaces the current owner of every
squad by the squad that the current owner hands to, and the settlement is reached on the
first sweep that changes nothing. The number of sweeps taken, counting the sweep that
changed nothing, is a reported figure.

A squad that has handed its entry away is no longer its own effective owner and stands
outside the pool from which the finals bracket is elected.
"""

    docs["upkeep-policy.md"] = """# Upkeep policy

A deployed unit charges its squad an upkeep rate for every tick it remains on the field.
The rate is the value field of the deployment record. The charge runs from the tick of the
deployment up to, and not counting, the tick at which the unit leaves the field.

An eliminated unit remains upkeep bearing for the remainder of the match. Elimination
withdraws the output of the unit but never its upkeep. Only a disband order withdraws
upkeep, and only from its own tick onward. A unit that is neither disbanded nor withdrawn
by the close of the match bears upkeep until the closing tick.

Upkeep raised in this way is the raw upkeep of the squad in that match.

Every squad carries one budget for the whole season. Raw upkeep is charged against that
budget match by match, taken in the logical order of the matches. Upkeep that still fits
inside the budget is charged upkeep. Upkeep that no longer fits is overflow upkeep. Once
the budget is exhausted every further tick of upkeep is overflow. Overflow upkeep is
reported but never levied.

Budget cap roster: `420`, `560`, `700`, `840`, `980`, `1120`

The season budget is one of the figures on that roster. Which one it is appears nowhere in
this material.
"""

    docs["scoring-policy.md"] = """# Scoring policy

Output reported by a unit counts toward the gross output of its squad in that match unless
it is withdrawn.

Output reported by a unit at or after the tick at which that unit was eliminated is
withdrawn. Output reported by a squad at or after the tick at which that squad conceded the
match is withdrawn, and that applies to every unit of the squad. Output reported before the
concession stands. Withdrawn output is reported as voided output and never counts toward
gross output.

The match score of a squad is its gross output multiplied by the ladder weight of its
division, less its charged upkeep in that match multiplied by the levy rate of its licence
class. Overflow upkeep is never levied. All of this arithmetic is exact integer
arithmetic; nothing is ever rounded.

A squad that appears in a capture without appearing in the entrant registry carries a
ladder weight of one and a levy rate of one.

The season score of a squad is the sum of its match scores. A season score may be negative.
"""

    docs["standings-policy.md"] = """# Standings policy

Standing points are awarded match by match. Every squad that recorded any event in a match
took part in it.

The squad with the highest match score in a match takes three standing points. When two or
more squads share the highest match score each of them takes one standing point. Every
other squad takes none.

A squad that conceded a match takes no standing points from that match whatever its match
score, and it is never counted when the highest match score of that match is looked for.

A squad that wins matches back to back earns streak credit. Counting only the matches the
squad took part in, and taking them in logical order, a run of consecutive outright wins
earns two further standing points for every win in the run after the first. A shared
highest score breaks a run.

The standing points of a squad for the season are its match awards plus its streak credit.

Promotion tiers follow from the season score against one threshold. A season score of three
times the threshold or more is champion. A season score of at least twice the threshold but
below three times is contender. A season score of at least the threshold but below twice is
qualifier. Anything lower is provisional.

Promotion threshold roster: `180`, `260`, `340`, `420`, `500`, `580`

The threshold in force is one of the figures on that roster and is written down nowhere.
"""

    docs["bracket-policy.md"] = """# Bracket policy

The finals bracket holds exactly three squads. Only a squad that is its own effective owner
and that took part in at least the eligibility floor of matches may stand.

Eligibility match floor: `2`

The league would like the bracket to meet all of the following, and settles for the bracket
that meets the largest number of them.

- at least one member sits in a division whose ladder weight reaches the prominence figure
- both entrant segments appear among the members
- every member holds at least the standing points floor
- no two members share a conference
- at least one member is a ranked entrant
- the ladder weights of the members sum to at least the combined ladder weight floor
- no member carries any overflow upkeep
- at least one member holds the champion tier
- the gross output of the members sums to at least the combined gross output floor
- no member has handed its entry to another squad

Ladder weight prominence: `4`

Standing points floor: `8`

Combined ladder weight floor: `7`

Combined gross output floor: `900`

When several brackets meet the same largest number, the one whose members carry the least
charged upkeep in total stands. When that is still shared, the bracket whose member keys
sort first stands.

The review docket is elected separately. Every match carries a review index equal to its
voided output plus the review weight for every record in it that carried an escape
directive.

Review weight: `25`

Docket size: `5`

Docket conference cap: `2`

The docket holds exactly the docket size in matches and takes the largest total review
index it can reach while never holding more than the docket conference cap of matches from
any one conference. A match belongs to the conference of the registered squad that reported
the most gross output in it, and the squad whose key sorts first settles a tie. When two
dockets reach the same total, the one whose match identifiers sort first stands.
"""

    docs["reconciliation-invariants.md"] = """# Reconciliation invariants

The settlement is only accepted once the following are stated and understood. A residual
that is not zero is a finding to be reported, never an error to be silenced.

The declared record count of a capture is compared against the number of records actually
recovered from it. The declared segment count is compared against the number of segments
recovered. The declared roster size counts only the squads of the capture that appear in the
entrant registry, so comparing it against the number of distinct squads recovered exposes
every squad that played without an entry.

A squad that appears in a capture but not in the entrant registry is unregistered. Its
output and its upkeep are settled and reported in the season totals, and it is left out of
every conference report and every segment report because it belongs to none. The difference
between the season gross output and the sum of the conference gross outputs is therefore
the attribution residual.

A squad that appears in the entrant registry but in no capture is an absent entrant. It is
listed and it contributes nothing.

Gross output plus voided output equals all the output reported in a capture. Charged upkeep
plus overflow upkeep equals raw upkeep. Ticks inside a segment never move backwards, and any
place where they do is counted.

The kind ledger counts the recovered records of each event kind under the names deploy,
output, eliminate, disband, concede, transfer and close.
"""

    return docs


def w_controls_doc(figures, names):
    lines = ["# Calibration controls", "", "The control slice is a sealed capture that is held out of the season", "settlement. The figures below were published with it. Exactly one reading of the", "material reproduces every one of them.", ""]
    for index in range(1, 12):
        lines.append("- C%d %s: %d" % (index, names[index], figures[index]))
    lines.append("")
    lines.append("A reading that reproduces the first six figures has pinned the guard quantities.")
    lines.append("A reading that then reproduces the next two has pinned the season budget. A")
    lines.append("reading that then reproduces the last three has pinned the promotion threshold.")
    lines.append("")
    return "\n".join(lines)


W_CONTROL_NAMES = {
    1: "matches opened",
    2: "records recovered",
    3: "distinct squads",
    4: "gross output",
    5: "voided output",
    6: "raw upkeep",
    7: "charged upkeep",
    8: "overflow upkeep",
    9: "champion entrants",
    10: "qualifier entrants",
    11: "provisional entrants",
}


def w_write_text(path, text):
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def w_prune_rosters(cfg):
    """Drop roster values a capture cannot possibly distinguish.

    A capture whose guards leave a parameter with no observable consequence
    would leave that parameter free, so such a capture publishes the single
    value it actually used and the calibration slice stays decisive.
    """
    flags = cfg["flags"]
    if not flags & ((1 << 0) | (1 << 7)):
        cfg["tag_roster"] = [cfg["label"]]
    if not flags & (1 << 5):
        cfg["window_roster"] = [cfg["window"]]
    if not flags & (1 << 11):
        cfg["sentinel_roster"] = [cfg["sentinel"]]
    return cfg


def w_corpus(dest, seed, mut="base", guards=0x0FFF):
    """Materialise one corpus under an explicit seed, mutation and guard mask."""
    cfg = w_default_config() if mut == "base" else w_mutation(mut)
    cfg["seed"] = seed
    cfg["flags"] = guards
    return w_write_corpus(cfg, dest)


def w_write_corpus(cfg, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    w_prune_rosters(cfg)
    rng = random.Random(cfg["seed"])

    baseline_pool = w_pool(cfg, cfg["baseline_pool"])
    baseline_matches = w_build_matches(cfg, rng, baseline_pool, cfg["baseline_matches"], 100, [], None, 0)
    baseline_keys = []
    for match in baseline_matches:
        for event in match["events"]:
            if event["key"] not in baseline_keys:
                baseline_keys.append(event["key"])
    roster_dict = list(baseline_keys)

    control_pool = w_pool(cfg, cfg["control_pool"])
    control_matches = w_build_matches(
        cfg, rng, control_pool, cfg["control_matches"], 300, cfg["control_transfers"], roster_dict,
        cfg["control_directives_per_match"],
    )
    season_pool = w_pool(cfg, cfg["season_pool"], cfg.get("season_skip", ()))
    season_matches = w_build_matches(
        cfg, rng, season_pool, cfg["season_matches"], 500, cfg["season_transfers"], roster_dict,
        cfg["directives_per_match"],
    )

    registered = set(W_KEY_POOL[: cfg["registered"]])

    def declared_roster_of(matches):
        keys = set()
        for match in matches:
            for event in match["events"]:
                keys.add(event["key"])
        return len({k for k in keys if k in registered})

    baseline_raw = w_encode(cfg, 41, baseline_matches, 0, 0x5A31C7E2, 1000, declared_roster_of(baseline_matches), None, rng)
    control_raw = w_encode(
        cfg, 62, control_matches, cfg["flags"], 0x37C1A94D, 2000, declared_roster_of(control_matches), roster_dict, rng
    )
    season_raw = w_encode(
        cfg, 83, season_matches, cfg["flags"], 0x6B2F51A8, 3000, declared_roster_of(season_matches), roster_dict, rng
    )

    with open(os.path.join(out_dir, "baseline-capture.rjnl"), "wb") as handle:
        handle.write(baseline_raw)
    with open(os.path.join(out_dir, "control-capture.rjnl"), "wb") as handle:
        handle.write(control_raw)
    with open(os.path.join(out_dir, "season-capture.rjnl"), "wb") as handle:
        handle.write(season_raw)

    divisions_csv, licences_csv, registry_csv = w_tables(cfg)
    w_write_text(os.path.join(out_dir, "division-topology.csv"), divisions_csv)
    w_write_text(os.path.join(out_dir, "licence-classes.csv"), licences_csv)
    w_write_text(os.path.join(out_dir, "entrant-registry.csv"), registry_csv)

    docs = w_docs(cfg)
    for name, text in docs.items():
        w_write_text(os.path.join(out_dir, name), text)

    policy = cfg["policy"]
    bracket_text = docs["bracket-policy.md"]
    for token in ("eligibility_floor", "weight_prominence", "standing_floor", "weight_floor",
                  "output_floor", "review_weight", "docket_size", "docket_cap"):
        head = W_TPL[token].split(":")[0]
        bracket_text = w_swap(bracket_text, W_TPL[token], "%s: `%d`" % (head, policy[token]))
    w_write_text(os.path.join(out_dir, "bracket-policy.md"), bracket_text)

    guards_text = docs["replay-guards.md"]
    guards_text = w_swap(guards_text, W_TPL["tags"], "Engine tag roster: " + w_backtick_list(cfg["tag_roster"]))
    guards_text = w_swap(guards_text, W_TPL["windows"], "Window depth roster: " + w_backtick_list(cfg["window_roster"]))
    guards_text = w_swap(
        guards_text,
        W_TPL["sentinels"],
        "Sentinel roster: " + w_backtick_list(["0x%02X" % v for v in cfg["sentinel_roster"]]),
    )
    w_write_text(os.path.join(out_dir, "replay-guards.md"), guards_text)

    upkeep_text = w_swap(
        docs["upkeep-policy.md"], W_TPL["caps"], "Budget cap roster: " + w_backtick_list(cfg["cap_roster"])
    )
    w_write_text(os.path.join(out_dir, "upkeep-policy.md"), upkeep_text)

    standings_text = w_swap(
        docs["standings-policy.md"],
        W_TPL["thresholds"],
        "Promotion threshold roster: " + w_backtick_list(cfg["threshold_roster"]),
    )
    w_write_text(os.path.join(out_dir, "standings-policy.md"), standings_text)

    w_write_text(os.path.join(out_dir, "calibration-controls.md"), w_controls_doc({i: 0 for i in range(1, 12)}, W_CONTROL_NAMES))
    w_write_text(os.path.join(out_dir, "baseline-events.jsonl"), "{}\n")
    w_write_text(os.path.join(out_dir, "example-conference-report.json"), "{}\n")

    inputs = s_load(out_dir)
    params = {"label": cfg["label"], "window": cfg["window"], "sentinel": cfg["sentinel"]}
    baseline_dec = s_decode(inputs["captures"][1], {"label": "", "window": 1, "sentinel": 0}, None)
    inputs["roster_dict"] = s_roster_dictionary(baseline_dec)
    control_dec = s_decode(inputs["captures"][2], params, inputs["roster_dict"])

    open_res = s_domain(control_dec, inputs, 1 << 40, 1)
    ceiling = max(row["raw_upkeep"] for row in open_res["aggregate"].values())
    cap_roster = w_spread(
        range(20, max(60, ceiling), 20),
        lambda value: tuple(s_figures(control_dec, s_domain(control_dec, inputs, value, 1), inputs)[6:8]),
    )
    cap = cap_roster[cfg["cap_index"]]
    capped = s_domain(control_dec, inputs, cap, 1)
    top = max([row["season_score"] for row in capped["aggregate"].values()] + [60])
    threshold_roster = w_spread(
        range(20, max(60, top), 20),
        lambda value: tuple(s_figures(control_dec, s_domain(control_dec, inputs, cap, value), inputs)[8:11]),
    )
    threshold = threshold_roster[cfg["threshold_index"]]
    cfg["cap"] = cap
    cfg["threshold"] = threshold
    w_write_text(
        os.path.join(out_dir, "upkeep-policy.md"),
        w_swap(docs["upkeep-policy.md"], W_TPL["caps"], "Budget cap roster: " + w_backtick_list(cap_roster)),
    )
    w_write_text(
        os.path.join(out_dir, "standings-policy.md"),
        w_swap(
            docs["standings-policy.md"],
            W_TPL["thresholds"],
            "Promotion threshold roster: " + w_backtick_list(threshold_roster),
        ),
    )
    inputs = s_load(out_dir)
    inputs["roster_dict"] = s_roster_dictionary(baseline_dec)
    control_res = s_domain(control_dec, inputs, cap, threshold)
    figures = s_figures(control_dec, control_res, inputs)
    controls = {i + 1: figures[i] for i in range(11)}
    w_write_text(os.path.join(out_dir, "calibration-controls.md"), w_controls_doc(controls, W_CONTROL_NAMES))

    transcript = []
    for record in baseline_dec["records"]:
        transcript.append(
            json.dumps(
                {
                    "logical_segment": record["mi"],
                    "match_id": record["match_id"],
                    "record_ordinal": record["seq"],
                    "kind": record["kind"],
                    "squad_key": record["key"],
                    "unit": record["unit"],
                    "tick": record["tick"],
                    "value": record["value"],
                },
                sort_keys=True,
            )
        )
    w_write_text(os.path.join(out_dir, "baseline-events.jsonl"), "\n".join(transcript) + "\n")

    inputs = s_load(out_dir)
    inputs["roster_dict"] = list(roster_dict)
    control_dec = s_decode(inputs["captures"][2], params, inputs["roster_dict"])
    control_res = s_domain(control_dec, inputs, cfg["cap"], cfg["threshold"])
    _summary, conference_reports, _segment_reports, _dq = s_reports(control_dec, control_res, inputs)
    example_conference = sorted(conference_reports)[0]
    with open(os.path.join(out_dir, "example-conference-report.json"), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(conference_reports[example_conference], indent=2, sort_keys=True))
        handle.write("\n")

    return {
        "roster_dict": roster_dict,
        "controls": controls,
        "params": params,
        "cap": cfg["cap"],
        "threshold": cfg["threshold"],
    }


# ---------------------------------------------------------------------------
# Verifier harness
# ---------------------------------------------------------------------------

import copy
import json as _json
import os
import shutil
import subprocess
import sys
import tempfile

APP_OUT = "/app/out"
AGENT = APP_OUT + "/derive_report.py"

GRADED = (
    "summary.json",
    "data_quality.json",
    "region_East.json",
    "region_North.json",
    "region_South.json",
    "region_West.json",
    "segment_commercial.json",
    "segment_public.json",
)

ARTIFACTS = ("derive_report.py", "policy_audit.json") + GRADED

AUDIT_KEYS = {
    "bracket_candidates": int,
    "bracket_pool": list,
    "bracket_satisfied_max": int,
    "bracket_verdicts": list,
    "calibration_comparison": list,
    "docket_conferences": list,
    "effective_owners": list,
    "finals_bracket": list,
    "guard_flags_live": list,
    "guard_parameter_candidates": int,
    "guard_parameter_verdicts": list,
    "guard_parameters": dict,
    "promotion_threshold": int,
    "promotion_threshold_candidates": int,
    "promotion_threshold_verdicts": list,
    "review_docket": list,
    "review_index_total": int,
    "roster_dictionary": list,
    "roster_dictionary_size": int,
    "settlement_passes": int,
    "transfer_edges": list,
    "upkeep_cap": int,
    "upkeep_cap_candidates": int,
    "upkeep_cap_verdicts": list,
}

ALL_GUARDS = 0x0FFF

COMPANION = {
    "driftkey": 1 << 0,
    "segperm": (1 << 3) | (1 << 0),
    "endian": (1 << 4) | (1 << 3) | (1 << 0),
}

MUTATIONS = (
    "tie_config",
    "cap_push",
    "empty_group",
    "boundary_shift",
    "add_entity",
    "counterfactual_flip",
    "scale",
    "contradict",
    "alias_chain",
    "nonapply",
    "cycle",
    "partition",
    "sentinel_heavy",
    "fresh_base",
)

HELD_OUT_SEEDS = {name: 6620131 + i for i, name in enumerate(MUTATIONS)}

SHIPPED_SEED = 20260417
SHIPPED_MUT = "base"

_CORPUS_CACHE = {}
_AGENT_CACHE = {}


def _stage(tag, seed, mut, guards):
    key = (tag, seed, mut, guards)
    if key not in _CORPUS_CACHE:
        dest = tempfile.mkdtemp(prefix="rjnl_" + tag + "_")
        w_corpus(dest, seed, mut, guards)
        _CORPUS_CACHE[key] = dest
    return _CORPUS_CACHE[key]


def _isolated(source, name):
    parent = tempfile.mkdtemp(prefix="rjnl_iso_")
    os.chmod(parent, 0o755)
    target = os.path.join(parent, name)
    shutil.copytree(source, target)
    for entry in sorted(os.listdir(target)):
        os.chmod(os.path.join(target, entry), 0o444)
    os.chmod(target, 0o555)
    return target


def _invoke(data_dir, out_dir):
    if not os.path.exists(AGENT):
        raise AssertionError("no derivation program at " + AGENT)
    os.makedirs(out_dir, exist_ok=True)
    argv = [sys.executable, AGENT, data_dir, out_dir]
    drop = shutil.which("setpriv") is not None and hasattr(os, "geteuid") and os.geteuid() == 0
    if drop:
        os.chmod(out_dir, 0o777)
        argv = [
            "setpriv",
            "--reuid=nobody",
            "--regid=nogroup",
            "--init-groups",
        ] + argv
    proc = subprocess.run(argv, capture_output=True, timeout=600)
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise AssertionError(
            "derivation program exited %d: %s" % (proc.returncode, tail[-1] if tail else "")
        )
    return proc


def _collect(out_dir, names):
    found = {}
    for name in names:
        path = os.path.join(out_dir, name)
        if not os.path.exists(path):
            raise AssertionError("missing report " + name)
        with open(path, "r", encoding="utf-8") as fh:
            body = fh.read()
        try:
            found[name] = _json.loads(body)
        except Exception as exc:
            raise AssertionError("report %s is not readable json: %s" % (name, exc))
    return found


def _expected(data_dir, names):
    scratch = tempfile.mkdtemp(prefix="rjnl_ref_")
    s_run(data_dir, scratch)
    return _collect(scratch, names)


def _delivered(tag, seed, mut, guards, names=GRADED):
    key = (tag, seed, mut, guards)
    if key not in _AGENT_CACHE:
        data_dir = _stage(tag, seed, mut, guards)
        reference = _expected(data_dir, names)
        isolated = _isolated(data_dir, "capture_set")
        out_dir = tempfile.mkdtemp(prefix="rjnl_out_")
        _invoke(isolated, out_dir)
        _AGENT_CACHE[key] = (_collect(out_dir, names), reference)
    delivered, reference = _AGENT_CACHE[key]
    return copy.deepcopy(delivered), copy.deepcopy(reference)


def _shipped():
    return _collect(APP_OUT, GRADED)


def _shipped_expected(names=GRADED):
    return _expected(_stage("shipped", SHIPPED_SEED, SHIPPED_MUT, ALL_GUARDS), names)


def _same(delivered, expected, label):
    for name in sorted(expected):
        got = delivered.get(name)
        want = expected[name]
        if got == want:
            continue
        if isinstance(want, dict) and isinstance(got, dict):
            for field in sorted(want):
                if got.get(field) != want[field]:
                    raise AssertionError(
                        "%s: %s field %r is %r and the resolved value is %r"
                        % (label, name, field, got.get(field), want[field])
                    )
            extra = sorted(set(got) - set(want))
            if extra:
                raise AssertionError("%s: %s carries unresolved keys %r" % (label, name, extra))
        raise AssertionError("%s: %s diverges from the resolved report" % (label, name))


def _match_on(tag, seed, mut, guards):
    delivered, expected = _delivered(tag, seed, mut, guards)
    _same(delivered, expected, tag)


def _guard_case(name):
    return (1 << S_GUARD_NAMES.index(name)) | COMPANION.get(name, 0)


def _reuses_supplied_extract():
    delivered, _ = _delivered("leak", 9970001, "scale", ALL_GUARDS)
    try:
        shipped = _shipped_expected()
    except Exception:
        return False
    return delivered == shipped


# --- base format ----------------------------------------------------------


def test_format_artifacts_present():
    """POSITIVE: every promised artifact exists under the output directory."""
    for name in ARTIFACTS:
        path = os.path.join(APP_OUT, name)
        assert os.path.exists(path), "missing artifact " + name
        assert os.path.getsize(path) > 0, "empty artifact " + name


def test_format_reports_parse():
    """POSITIVE: the graded reports are readable json objects with the agreed skeleton."""
    reports = _shipped()
    summary = reports["summary.json"]
    assert isinstance(summary, dict)
    for field in ("capture_id", "matches_opened", "events_decoded", "season_score_total"):
        assert field in summary, "summary is missing " + field
    quality = reports["data_quality.json"]
    for field in ("declared_event_residual", "upkeep_residual", "kind_ledger"):
        assert field in quality, "reconciliation is missing " + field
    for name in GRADED:
        if name.startswith("region_") or name.startswith("segment_"):
            group = reports[name]
            assert isinstance(group.get("entries"), list), name + " has no entry list"


def test_format_adjudication_record_schema():
    """POSITIVE: the adjudication record carries every required key at the agreed type."""
    path = os.path.join(APP_OUT, "policy_audit.json")
    assert os.path.exists(path), "missing adjudication record"
    with open(path, "r", encoding="utf-8") as fh:
        audit = _json.load(fh)
    assert isinstance(audit, dict), "adjudication record is not an object"
    for key, kind in sorted(AUDIT_KEYS.items()):
        assert key in audit, "adjudication record is missing " + key
        assert isinstance(audit[key], kind), "adjudication record key %s has the wrong type" % key
    assert len(audit["calibration_comparison"]) >= 11, "calibration comparison is short"
    assert len(audit["guard_parameter_verdicts"]) >= 2, "guard parameter verdicts are short"


# --- shipped instance -----------------------------------------------------


def test_instance_summary():
    """POSITIVE: the shipped season summary matches the resolved season summary."""
    _same(_shipped(), _shipped_expected(("summary.json",)), "shipped capture")


def test_instance_reconciliation():
    """POSITIVE: the shipped reconciliation matches the resolved reconciliation."""
    _same(_shipped(), _shipped_expected(("data_quality.json",)), "shipped capture")


def test_instance_region_east():
    """POSITIVE: the shipped East conference report matches the resolved report."""
    _same(_shipped(), _shipped_expected(("region_East.json",)), "shipped capture")


def test_instance_region_north():
    """POSITIVE: the shipped North conference report matches the resolved report."""
    _same(_shipped(), _shipped_expected(("region_North.json",)), "shipped capture")


def test_instance_region_south():
    """POSITIVE: the shipped South conference report matches the resolved report."""
    _same(_shipped(), _shipped_expected(("region_South.json",)), "shipped capture")


def test_instance_region_west():
    """POSITIVE: the shipped West conference report matches the resolved report."""
    _same(_shipped(), _shipped_expected(("region_West.json",)), "shipped capture")


def test_instance_segment_commercial():
    """POSITIVE: the shipped sponsored entrant report matches the resolved report."""
    _same(_shipped(), _shipped_expected(("segment_commercial.json",)), "shipped capture")


def test_instance_segment_public():
    """POSITIVE: the shipped open entrant report matches the resolved report."""
    _same(_shipped(), _shipped_expected(("segment_public.json",)), "shipped capture")


# --- guard inversion ------------------------------------------------------


def test_trap_keystream():
    """POSITIVE: the per record keystream guard is inverted on an unseen capture."""
    _match_on("trap_keystream", 7730011, "base", _guard_case("keystream"))


def test_trap_chain():
    """POSITIVE: the cross record value chain guard is inverted on an unseen capture."""
    _match_on("trap_chain", 7730012, "base", _guard_case("chain"))


def test_trap_delta2():
    """POSITIVE: the second order tick guard is inverted on an unseen capture."""
    _match_on("trap_delta2", 7730013, "base", _guard_case("delta2"))


def test_trap_driftkey():
    """POSITIVE: the drifting segment key guard is inverted on an unseen capture."""
    _match_on("trap_driftkey", 7730014, "base", _guard_case("driftkey"))


def test_trap_segperm():
    """POSITIVE: the envelope seeded segment permutation is inverted on an unseen capture."""
    _match_on("trap_segperm", 7730015, "base", _guard_case("segperm"))


def test_trap_backref():
    """POSITIVE: the sliding window back reference guard is inverted on an unseen capture."""
    _match_on("trap_backref", 7730016, "base", _guard_case("backref"))


def test_trap_endian():
    """POSITIVE: the mixed endianness envelope island is inverted on an unseen capture."""
    _match_on("trap_endian", 7730017, "base", _guard_case("endian"))


def test_trap_countbias():
    """POSITIVE: the biased declared count guard is inverted on an unseen capture."""
    _match_on("trap_countbias", 7730018, "base", _guard_case("countbias"))


def test_trap_lenshadow():
    """POSITIVE: the shadow record width guard is inverted on an unseen capture."""
    _match_on("trap_lenshadow", 7730019, "base", _guard_case("lenshadow"))


def test_trap_rotate():
    """POSITIVE: the checksum invisible payload rotation is inverted on an unseen capture."""
    _match_on("trap_rotate", 7730020, "base", _guard_case("rotate"))


def test_trap_alias():
    """POSITIVE: the cross capture roster alias guard is inverted on an unseen capture."""
    _match_on("trap_alias", 7730021, "base", _guard_case("alias"))


def test_trap_escape():
    """POSITIVE: the sparse sentinel directive guard is inverted on an unseen capture."""
    _match_on("trap_escape", 7730022, "base", _guard_case("escape"))


# --- generalisation -------------------------------------------------------


def test_generalisation_tie_config():
    """POSITIVE: a held out capture whose bracket tie break decides the finals resolves."""
    _match_on("gen_tie_config", HELD_OUT_SEEDS["tie_config"], "tie_config", ALL_GUARDS)


def test_generalisation_cap_push():
    """POSITIVE: a held out capture that pushes the upkeep budget cap resolves."""
    _match_on("gen_cap_push", HELD_OUT_SEEDS["cap_push"], "cap_push", ALL_GUARDS)


def test_generalisation_empty_group():
    """POSITIVE: a held out capture that empties one reporting group resolves."""
    _match_on("gen_empty_group", HELD_OUT_SEEDS["empty_group"], "empty_group", ALL_GUARDS)


def test_generalisation_boundary_shift():
    """POSITIVE: a held out capture sitting on the promotion tier boundary resolves."""
    _match_on("gen_boundary_shift", HELD_OUT_SEEDS["boundary_shift"], "boundary_shift", ALL_GUARDS)


def test_generalisation_add_entity():
    """POSITIVE: a held out capture with additional divisions and entrants resolves."""
    _match_on("gen_add_entity", HELD_OUT_SEEDS["add_entity"], "add_entity", ALL_GUARDS)


def test_generalisation_counterfactual_flip():
    """POSITIVE: a held out capture dominated by elimination and disband ordering resolves."""
    _match_on(
        "gen_counterfactual_flip",
        HELD_OUT_SEEDS["counterfactual_flip"],
        "counterfactual_flip",
        ALL_GUARDS,
    )


def test_generalisation_scale():
    """POSITIVE: a held out capture at larger scale resolves."""
    _match_on("gen_scale", HELD_OUT_SEEDS["scale"], "scale", ALL_GUARDS)


def test_generalisation_contradict():
    """POSITIVE: a held out capture with contradictory registry evidence resolves."""
    _match_on("gen_contradict", HELD_OUT_SEEDS["contradict"], "contradict", ALL_GUARDS)


def test_generalisation_alias_chain():
    """POSITIVE: a held out capture with a longer transfer chain resolves."""
    _match_on("gen_alias_chain", HELD_OUT_SEEDS["alias_chain"], "alias_chain", ALL_GUARDS)


def test_generalisation_nonapply():
    """POSITIVE: a held out capture where the transfer rule stays idle resolves."""
    _match_on("gen_nonapply", HELD_OUT_SEEDS["nonapply"], "nonapply", ALL_GUARDS)


def test_generalisation_cycle():
    """POSITIVE: a held out capture carrying a transfer cycle resolves."""
    _match_on("gen_cycle", HELD_OUT_SEEDS["cycle"], "cycle", ALL_GUARDS)


def test_generalisation_partition():
    """POSITIVE: a held out capture with a redrawn division map resolves."""
    _match_on("gen_partition", HELD_OUT_SEEDS["partition"], "partition", ALL_GUARDS)


def test_generalisation_sentinel_heavy():
    """POSITIVE: a held out capture dense in sentinel directives resolves."""
    _match_on("gen_sentinel_heavy", HELD_OUT_SEEDS["sentinel_heavy"], "sentinel_heavy", ALL_GUARDS)


def test_generalisation_fresh_base():
    """POSITIVE: a held out capture with a rebuilt baseline roster resolves."""
    _match_on("gen_fresh_base", HELD_OUT_SEEDS["fresh_base"], "fresh_base", ALL_GUARDS)


# --- guard interaction ----------------------------------------------------


def test_mixed_stateful_guards():
    """POSITIVE: the four stateful guards acting together are inverted."""
    _match_on("mix_stateful", 8830011, "base", 0x00F)


def test_mixed_layout_guards():
    """POSITIVE: the envelope and framing guards acting together are inverted."""
    _match_on("mix_layout", 8830012, "cap_push", 0x1D9)


def test_mixed_key_guards():
    """POSITIVE: the attribution guards acting together are inverted."""
    _match_on("mix_key", 8830013, "alias_chain", 0xE20)


def test_mixed_corpus_context_guards():
    """POSITIVE: the cross capture guards acting together are inverted."""
    _match_on("mix_corpus", 8830014, "scale", 0x699)


# --- negative detectors ---------------------------------------------------


def test_negative_weight_hardcoded_sample_answers():
    """NEGATIVE: fires when the shipped capture is reproduced and every fresh capture is missed."""
    try:
        _same(_shipped(), _shipped_expected(), "shipped capture")
    except Exception:
        raise AssertionError("shortcut absent: the shipped capture was not reproduced")
    misses = 0
    for index, mut in enumerate(("base", "tie_config", "scale", "cycle")):
        try:
            _match_on("probe_%d" % index, 9950000 + index, mut, ALL_GUARDS)
        except Exception:
            misses += 1
    overfitted = misses == 4 and not _reuses_supplied_extract()
    assert overfitted, "shortcut absent: fresh captures were resolved or the transcript was reused"


def test_negative_weight_filename_dependent_behaviour():
    """NEGATIVE: fires when identical capture bytes under two directory names diverge."""
    assert os.path.exists(AGENT), "shortcut absent: no program was delivered"
    source = _stage("naming", 9960001, "base", ALL_GUARDS)
    seen = []
    for label in ("alpha_capture", "zulu_capture"):
        out_dir = tempfile.mkdtemp(prefix="rjnl_name_")
        _invoke(_isolated(source, label), out_dir)
        seen.append(_collect(out_dir, GRADED))
    assert all(seen), "shortcut absent: no reports were produced"
    assert seen[0] != seen[1], "shortcut absent: both directory names agreed"


def test_negative_weight_reads_supplied_answer_data_at_runtime():
    """NEGATIVE: fires when a fresh capture yields the shipped capture figures."""
    assert os.path.exists(AGENT), "shortcut absent: no program was delivered"
    assert _reuses_supplied_extract(), "shortcut absent: the supplied transcript was not reused"


def test_negative_weight_nondeterministic_output():
    """NEGATIVE: fires when four runs over one capture disagree."""
    assert os.path.exists(AGENT), "shortcut absent: no program was delivered"
    data_dir = _isolated(_stage("stable", 9980001, "base", ALL_GUARDS), "capture_set")
    runs = []
    for _ in range(4):
        out_dir = tempfile.mkdtemp(prefix="rjnl_rep_")
        _invoke(data_dir, out_dir)
        runs.append(_collect(out_dir, GRADED))
    assert all(runs), "shortcut absent: no reports were produced"
    assert any(run != runs[0] for run in runs[1:]), "shortcut absent: the runs agreed"
