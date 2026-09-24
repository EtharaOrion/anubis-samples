#!/usr/bin/env python3
"""Rebuild the travelling exhibition installation dossier from a snapshot.

Usage: python3 plan.py <DATA_DIR> <OUT_DIR>

All arithmetic is integer or exact rational; no float is ever formed.
"""

import csv
import hashlib
import json
import os
import sys
from math import gcd

LENGTH_FACTOR = {"MM": 1000, "CM": 10000, "IN": 25400}
MASS_FACTOR = {"G": 1, "KG": 1000}
CENTS_PER_UNIT = 100
SECONDS_PER_DAY = 3600

KIND_RULE = "D_RULE"
KIND_CONSERVATION = "D_CONSERVATION"
KIND_BAYSIZE = "D_BAYSIZE"
KIND_RANK = {KIND_BAYSIZE: 0, KIND_CONSERVATION: 1, KIND_RULE: 2}


def read_csv(data_dir, name):
    path = os.path.join(data_dir, name)
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        rows = []
        for row in csv.DictReader(handle):
            rows.append({k.strip(): (v.strip() if isinstance(v, str) else v)
                         for k, v in row.items() if k is not None})
        return rows


def read_json(data_dir, name):
    with open(os.path.join(data_dir, name), "r", encoding="utf-8") as handle:
        return json.load(handle)


def round_half_up(num, den):
    """Round num/den half up. A zero denominator is the sentinel 0/1."""
    if den == 0:
        return 0
    if den < 0:
        num, den = -num, -den
    quotient, remainder = divmod(num, den)
    if 2 * remainder >= den:
        quotient += 1
    return quotient


def ceil_div(num, den):
    if den == 0:
        return 0
    quotient, remainder = divmod(num, den)
    return quotient + 1 if remainder else quotient


def reduce_ratio(num, den):
    if den == 0:
        return 0, 1
    divisor = gcd(abs(num), abs(den))
    if divisor == 0:
        return 0, 1
    return num // divisor, den // divisor


def write_lines(path, rows):
    with open(path, "w", encoding="utf-8", newline="") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")))
            handle.write("\n")


def write_object(path, obj):
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(json.dumps(obj, sort_keys=True, indent=2))
        handle.write("\n")


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        digest.update(handle.read())
    return digest.hexdigest()


def orientation_boxes(cw, ch, cd):
    """Footprint width, footprint depth and vertical extent per orientation index."""
    return {
        0: (cw, cd, ch),
        1: (cd, cw, ch),
        2: (cw, ch, cd),
        3: (ch, cw, cd),
    }


def find_cycle(nodes, edges):
    """Deterministic DFS cycle search. Returns the cycle as a list of (from, to)."""
    successors = {node: [] for node in nodes}
    for (src, dst) in edges:
        successors[src].append(dst)
    for node in successors:
        successors[node].sort()

    color = {node: 0 for node in nodes}
    parent = {}
    for start in sorted(nodes):
        if color[start] != 0:
            continue
        color[start] = 1
        stack = [(start, iter(successors[start]))]
        while stack:
            node, walker = stack[-1]
            advanced = False
            for nxt in walker:
                if color[nxt] == 0:
                    color[nxt] = 1
                    parent[nxt] = node
                    stack.append((nxt, iter(successors[nxt])))
                    advanced = True
                    break
                if color[nxt] == 1:
                    cycle = [(node, nxt)]
                    cur = node
                    while cur != nxt:
                        prev = parent[cur]
                        cycle.append((prev, cur))
                        cur = prev
                    return cycle
            if not advanced:
                color[node] = 2
                stack.pop()
    return None


def build(data_dir, out_dir):
    config = read_json(data_dir, "horizon_config.json")
    objective_order = read_json(data_dir, "objective_order.json")
    clearance_factor = LENGTH_FACTOR[config["clearance_unit"]]
    setup_div = int(config["setup_div"])
    chain_seed = config["chain_seed"]
    venue_end_shift = -1 if config["venue_end_convention"] == "exclusive" else 0
    conservation_end_shift = -1 if config["conservation_end_convention"] == "exclusive" else 0

    # ---------------------------------------------------------------- inputs
    master_rows = read_csv(data_dir, "object_master.csv")
    order_ids = []
    master = {}
    for row in master_rows:
        oid = row["object_id"]
        if oid in master:
            continue
        master[oid] = {"lender_id": row["lender_id"], "material_code": row["material_code"]}
        order_ids.append(oid)

    dims = {r["object_id"]: r for r in read_csv(data_dir, "object_dimensions.csv")}
    masses = {r["object_id"]: r for r in read_csv(data_dir, "object_mass.csv")}
    values = {r["object_id"]: r for r in read_csv(data_dir, "object_value.csv")}
    orients = {r["object_id"]: r for r in read_csv(data_dir, "object_orientation.csv")}

    versions = {}
    for row in read_csv(data_dir, "object_version_map.csv"):
        oid = row["object_id"]
        version = int(row["version"])
        if oid not in versions or version > versions[oid]:
            versions[oid] = version

    materials = {r["material_code"]: r for r in read_csv(data_dir, "material_rules.csv")}
    handling = {r["handling_class"]: r for r in read_csv(data_dir, "handling_class_rules.csv")}
    bays = {r["bay_id"]: r for r in read_csv(data_dir, "gallery_bays.csv")}
    bay_ids = sorted(bays)

    bay_blocked = {}
    for row in read_csv(data_dir, "bay_exceptions.csv"):
        bay_blocked.setdefault(row["bay_id"], set()).add(row["blocked_class"])

    crates = {r["crate_class"]: r for r in read_csv(data_dir, "crate_catalog.csv")}
    crate_ids = sorted(crates)

    reservations = {}
    for row in read_csv(data_dir, "crate_reservations.csv"):
        key = (row["crate_class"], row["lender_id"])
        reservations[key] = reservations.get(key, 0) + int(row["reserved_count"])

    prior_consumed = {}
    for row in read_csv(data_dir, "prior_state.csv"):
        cls = row["crate_class"]
        prior_consumed[cls] = prior_consumed.get(cls, 0) + int(row["consumed_count"])

    conservation = {r["conservation_class"]: r for r in read_csv(data_dir, "conservation_rules.csv")}
    venue = {r["bay_id"]: r for r in read_csv(data_dir, "venue_windows.csv")}
    crew = {r["crew_class"]: r for r in read_csv(data_dir, "crew_roster.csv")}
    dependency_rules = read_csv(data_dir, "dependency_rules.csv")
    transport_rules = {r["lender_id"]: r for r in read_csv(data_dir, "transport_rules.csv")}
    lenders = {r["lender_id"]: r for r in read_csv(data_dir, "lender_master.csv")}
    value_classes = read_csv(data_dir, "value_class_rules.csv")

    sensitivity = {cls: rank for rank, cls in enumerate(sorted(conservation))}

    # ------------------------------------------- stage 1 canonical identity
    canonical = {}
    for oid in order_ids:
        dim = dims[oid]
        mass_row = masses[oid]
        state = {
            "object_id": oid,
            "lender_id": master[oid]["lender_id"],
            "material_code": master[oid]["material_code"],
            "version": versions[oid],
            "declared_value": int(values[oid]["declared_value"]),
            "declared_value_cents": int(values[oid]["declared_value"]) * CENTS_PER_UNIT,
            "mass_g": int(mass_row["mass"]) * MASS_FACTOR[mass_row["mass_unit"]],
        }
        factor = LENGTH_FACTOR[dim["dim_unit"]]
        state["width_um"] = int(dim["width"]) * factor
        state["height_um"] = int(dim["height"]) * factor
        state["depth_um"] = int(dim["depth"]) * factor
        material = materials[state["material_code"]]
        state["conservation_class"] = material["conservation_class"]
        state["max_lux"] = int(material["max_lux"])
        state["handling_class"] = material["handling_class"]
        canonical[oid] = state

    canonical_rows = [{
        "object_id": oid,
        "lender_id": canonical[oid]["lender_id"],
        "material_code": canonical[oid]["material_code"],
        "version": canonical[oid]["version"],
        "declared_value_cents": canonical[oid]["declared_value_cents"],
        "mass_g": canonical[oid]["mass_g"],
    } for oid in order_ids]

    # ------------------------------------------- stage 2 normalized geometry
    geometry_rows = []
    for oid in order_ids:
        state = canonical[oid]
        rules = handling[state["handling_class"]]
        margin_w = int(rules["clearance_w"]) * clearance_factor
        margin_d = int(rules["clearance_d"]) * clearance_factor
        margin_h = int(rules["clearance_h"]) * clearance_factor
        state["clearance_w"] = state["width_um"] + 2 * margin_w
        state["clearance_d"] = state["depth_um"] + 2 * margin_d
        state["clearance_h"] = state["height_um"] + margin_h
        num, den = reduce_ratio(state["height_um"], state["width_um"])
        state["aspect_num"], state["aspect_den"] = num, den
        handle_margin = int(rules["handle_margin"]) * clearance_factor
        state["handling_w"] = state["width_um"] + handle_margin
        state["handling_h"] = state["height_um"] + handle_margin
        state["handling_d"] = state["depth_um"] + handle_margin
        state["handling_volume"] = state["handling_w"] * state["handling_h"] * state["handling_d"]
        geometry_rows.append({
            "object_id": oid,
            "clearance_w": state["clearance_w"],
            "clearance_h": state["clearance_h"],
            "clearance_d": state["clearance_d"],
            "aspect_num": num,
            "aspect_den": den,
        })

    # --------------------------------------- stage 3 orientation admissibility
    admissibility_rows = []
    for oid in order_ids:
        state = canonical[oid]
        tip = int(orients[oid]["tip_allowed"]) != 0
        rotate = int(orients[oid]["rotate_allowed"]) != 0
        options = [0]
        if rotate:
            options.append(1)
        if tip:
            options.extend([2, 3])
        state["tip_allowed"] = tip
        state["rotate_allowed"] = rotate
        state["orientations"] = options
        admissibility_rows.append({
            "object_id": oid,
            "orientations": list(options),
            "tip_allowed": tip,
            "rotate_allowed": rotate,
        })

    # ------------------------------------------------ stage 4 bay candidates
    bay_interior = {}
    for bid in bay_ids:
        row = bays[bid]
        bay_interior[bid] = (
            int(row["interior_w"]) * clearance_factor,
            int(row["interior_h"]) * clearance_factor,
            int(row["interior_d"]) * clearance_factor,
        )

    def fitting_orientation(oid, bid):
        state = canonical[oid]
        iw, ih, idp = bay_interior[bid]
        boxes = orientation_boxes(state["clearance_w"], state["clearance_h"], state["clearance_d"])
        for index in state["orientations"]:
            fw, fd, fv = boxes[index]
            if fw <= iw and fd < idp and fv <= ih:
                return index
        return -1

    bay_candidate_rows = []
    candidate_fit = {}
    for oid in order_ids:
        for bid in bay_ids:
            index = fitting_orientation(oid, bid)
            candidate_fit[(oid, bid)] = index
            bay_candidate_rows.append({
                "object_id": oid,
                "bay_id": bid,
                "fits": index >= 0,
                "orientation": index,
            })

    scan_order = sorted(bay_ids, key=lambda b: (int(bays[b]["bay_priority"]), b))

    # ---------------------------------------------- stage 5 selected placement
    selected_rows = []
    rejected_rows = []
    for oid in order_ids:
        state = canonical[oid]
        chosen_bay, chosen_index = "", -1
        for bid in scan_order:
            index = candidate_fit[(oid, bid)]
            if index >= 0:
                chosen_bay, chosen_index = bid, index
                break
        if chosen_bay:
            code = "P_OK"
        else:
            code = "P_NO_ORIENTATION" if state["orientations"] == [0] else "P_NO_FIT"
            rejected_rows.append({"object_id": oid, "code": code})
        state["selected_bay"] = chosen_bay
        state["selected_orientation"] = chosen_index
        state["selected_code"] = code
        selected_rows.append({
            "object_id": oid,
            "bay_id": chosen_bay,
            "orientation": chosen_index,
            "code": code,
        })
    rejected_rows.sort(key=lambda r: (r["object_id"], r["code"]))

    # ------------------------------------------ stage 6 conservation adjusted
    def conservation_ok(oid, bid):
        state = canonical[oid]
        lux = int(bays[bid]["lux_level"])
        if lux > state["max_lux"]:
            return False
        if state["conservation_class"] in bay_blocked.get(bid, set()):
            return False
        if int(conservation[state["conservation_class"]]["needs_dark"]) != 0 and lux > 0:
            return False
        return True

    conservation_rows = []
    for oid in order_ids:
        state = canonical[oid]
        geometric = [bid for bid in scan_order if candidate_fit[(oid, bid)] >= 0]
        chosen_bay, chosen_index = "", -1
        for bid in geometric:
            if conservation_ok(oid, bid):
                chosen_bay, chosen_index = bid, candidate_fit[(oid, bid)]
                break
        if chosen_bay:
            code = "P_OK"
        elif not geometric:
            code = "P_NO_ORIENTATION" if state["orientations"] == [0] else "P_NO_FIT"
        elif (len(geometric) == 1
              and state["conservation_class"] in bay_blocked.get(geometric[0], set())):
            code = "P_BAY_EXCEPTION"
        else:
            code = "P_CONSERVATION_BLOCK"
        state["bay"] = chosen_bay
        state["orientation"] = chosen_index
        state["placement_code"] = code
        conservation_rows.append({
            "object_id": oid,
            "bay_id": chosen_bay,
            "orientation": chosen_index,
            "code": code,
        })

    # ---------------------------------------------- stage 7 crate candidates
    crate_interior = {}
    for cid in crate_ids:
        row = crates[cid]
        crate_interior[cid] = (
            int(row["inner_w"]) * clearance_factor,
            int(row["inner_h"]) * clearance_factor,
            int(row["inner_d"]) * clearance_factor,
        )

    crate_candidate_rows = []
    for oid in sorted(order_ids):
        state = canonical[oid]
        handling_sorted = sorted((state["handling_w"], state["handling_h"], state["handling_d"]))
        fits = []
        for cid in crate_ids:
            inner_sorted = sorted(crate_interior[cid])
            if all(h <= i for h, i in zip(handling_sorted, inner_sorted)):
                interior_volume = 1
                for side in crate_interior[cid]:
                    interior_volume *= side
                fits.append((interior_volume - state["handling_volume"], cid))
        fits.sort(key=lambda item: (item[0], item[1]))
        state["crate_candidates"] = fits
        state["primary_class"] = fits[0][1] if fits else ""
        for residual, cid in fits:
            crate_candidate_rows.append({
                "object_id": oid,
                "crate_class": cid,
                "residual": residual,
            })

    # -------------------------------- stage 13a crew class and setup seconds
    crew_rows = []
    for oid in order_ids:
        state = canonical[oid]
        rules = handling[state["handling_class"]]
        state["crew_class"] = rules["crew_class"]
        state["setup_seconds"] = round_half_up(int(rules["setup_base"]) * state["mass_g"], setup_div)
        crew_rows.append({
            "object_id": oid,
            "crew_class": state["crew_class"],
            "setup_seconds": state["setup_seconds"],
        })

    # ---------------------------------------------- stage 10 dependency edges
    edges = {}

    def offer(src, dst, kind, weight):
        key = (src, dst)
        current = edges.get(key)
        if current is None or (KIND_RANK[kind], weight) > (KIND_RANK[current[0]], current[1]):
            edges[key] = (kind, weight)

    for row in dependency_rules:
        src, dst = row["from_object"], row["to_object"]
        if src in canonical and dst in canonical:
            offer(src, dst, KIND_RULE, int(row["edge_priority"]))

    by_bay = {}
    for oid in order_ids:
        bid = canonical[oid]["bay"]
        if bid:
            by_bay.setdefault(bid, []).append(oid)

    for bid in sorted(by_bay):
        members = sorted(by_bay[bid])
        for a in members:
            for b in members:
                if a == b:
                    continue
                va = canonical[a]["handling_volume"]
                vb = canonical[b]["handling_volume"]
                if va > vb or (va == vb and a < b):
                    offer(a, b, KIND_BAYSIZE, va)
        for a in members:
            for b in members:
                if a == b:
                    continue
                sa = sensitivity[canonical[a]["conservation_class"]]
                sb = sensitivity[canonical[b]["conservation_class"]]
                if sa > sb:
                    offer(a, b, KIND_CONSERVATION, canonical[a]["handling_volume"])

    dependency_rows = [{
        "from": src,
        "to": dst,
        "kind": kind,
        "weight": weight,
    } for (src, dst), (kind, weight) in sorted(edges.items())]

    # ------------------------------------------------ stage 11 cycle removal
    live = dict(edges)
    removed = []
    nodes = list(order_ids)
    while True:
        cycle = find_cycle(nodes, live)
        if cycle is None:
            break
        victim = min(cycle, key=lambda e: (KIND_RANK[live[e][0]], live[e][1], e[0], e[1]))
        kind, weight = live.pop(victim)
        removed.append({
            "from": victim[0],
            "to": victim[1],
            "kind": kind,
            "weight": weight,
        })
    repair_converged = find_cycle(nodes, live) is None
    final_cycle_count = 0 if repair_converged else 1

    # -------------------------------------------------- stage 12 install order
    indegree = {oid: 0 for oid in nodes}
    outgoing = {oid: [] for oid in nodes}
    for (src, dst) in live:
        indegree[dst] += 1
        outgoing[src].append(dst)
    ready = sorted(oid for oid in nodes if indegree[oid] == 0)
    install_sequence = []
    while ready:
        pick = min(ready, key=lambda oid: (canonical[oid]["setup_seconds"], oid))
        ready.remove(pick)
        install_sequence.append(pick)
        for dst in sorted(outgoing[pick]):
            indegree[dst] -= 1
            if indegree[dst] == 0:
                ready.append(dst)
        ready.sort()

    install_rows = []
    for position, oid in enumerate(install_sequence, start=1):
        canonical[oid]["install_position"] = position
        install_rows.append({"position": position, "object_id": oid})

    # ------------------------------------------ stage 8 shared crate capacity
    remaining = {cid: int(crates[cid]["count"]) - prior_consumed.get(cid, 0) for cid in crate_ids}
    reserved_left = dict(reservations)

    for oid in install_sequence:
        state = canonical[oid]
        lender = state["lender_id"]
        options = []
        for residual, cid in state["crate_candidates"]:
            if remaining[cid] <= 0:
                continue
            has_reservation = reserved_left.get((cid, lender), 0) > 0
            options.append((0 if has_reservation else 1, residual, cid))
        if options:
            options.sort()
            _, residual, cid = options[0]
            remaining[cid] -= 1
            if reserved_left.get((cid, lender), 0) > 0:
                reserved_left[(cid, lender)] -= 1
            state["crate_class"] = cid
            state["crate_residual"] = residual
            state["crate_code"] = "C_PRIMARY" if cid == state["primary_class"] else "C_DISPLACED"
        else:
            state["crate_class"] = ""
            state["crate_residual"] = 0
            state["crate_code"] = "C_NO_CRATE"

    crate_assignment_rows = []
    contention_rows = []
    for oid in sorted(order_ids):
        state = canonical[oid]
        crate_assignment_rows.append({
            "object_id": oid,
            "crate_class": state["crate_class"],
            "code": state["crate_code"],
            "primary_class": state["primary_class"],
        })
        if state["crate_code"] == "C_DISPLACED":
            contention_rows.append({
                "object_id": oid,
                "wanted_class": state["primary_class"],
                "assigned_class": state["crate_class"],
            })

    ledger_rows = [{
        "crate_class": cid,
        "catalog_count": int(crates[cid]["count"]),
        "prior_consumed": prior_consumed.get(cid, 0),
        "remaining": remaining[cid],
    } for cid in crate_ids]

    # ---------------------------------------------------- stage 9 transport
    transport_rows = []
    for oid in order_ids:
        state = canonical[oid]
        cid = state["crate_class"]
        if not cid:
            continue
        mode = crates[cid]["transport_mode"]
        exception = False
        rule = transport_rules.get(state["lender_id"])
        if rule is not None and rule["forbidden_mode"] == mode:
            mode = rule["fallback_mode"]
            exception = True
        transport_rows.append({
            "object_id": oid,
            "crate_class": cid,
            "mode": mode,
            "exception": exception,
        })

    # ------------------------------------------------- stage 13b windows
    window_rows = []
    unschedulable_rows = []
    for oid in order_ids:
        state = canonical[oid]
        required_days = ceil_div(state["setup_seconds"], SECONDS_PER_DAY)
        bid = state["bay"]
        if not bid:
            code = "S_NO_PLACEMENT"
            low, high = 0, -1
        else:
            cons = conservation[state["conservation_class"]]
            venue_row = venue[bid]
            crew_row = crew[state["crew_class"]]
            low = max(int(cons["setup_start"]), int(venue_row["access_start"]),
                      int(crew_row["avail_start"]))
            high = min(int(cons["setup_end"]) + conservation_end_shift,
                       int(venue_row["access_end"]) + venue_end_shift,
                       int(crew_row["avail_end"]))
            if not state["crate_class"]:
                code = "S_NO_CRATE"
            elif high < low:
                code = "S_NO_WINDOW"
            elif required_days > high - low + 1:
                code = "S_SHORT_WINDOW"
            else:
                code = "S_OK"
        state["window_start"] = low
        state["window_end"] = high
        state["required_days"] = required_days
        state["schedule_code"] = code
        state["scheduled"] = code == "S_OK"
        window_rows.append({
            "object_id": oid,
            "bay_id": bid,
            "window_start": low,
            "window_end": high,
            "required_days": required_days,
            "code": code,
        })
        if code != "S_OK":
            unschedulable_rows.append({"object_id": oid, "code": code})
    unschedulable_rows.sort(key=lambda r: (r["object_id"], r["code"]))

    # ------------------------------------------------------ stage 14 dossier
    occupancy_rows = []
    for bid in bay_ids:
        members = sorted(by_bay.get(bid, []))
        occupancy_rows.append({
            "bay_id": bid,
            "object_count": len(members),
            "objects": members,
        })

    ranked_classes = sorted(value_classes, key=lambda r: -int(r["min_value"]))

    def value_class_of(oid):
        declared = canonical[oid]["declared_value"]
        for row in ranked_classes:
            if declared >= int(row["min_value"]):
                return row
        return None

    unschedulable_penalty = 0
    contention_penalty = 0
    total_insurance = 0
    total_setup = 0
    total_residual = 0
    for oid in order_ids:
        state = canonical[oid]
        vclass = value_class_of(oid)
        if not state["scheduled"] and vclass is not None:
            unschedulable_penalty += int(vclass["unschedulable_penalty"])
        if state["crate_code"] == "C_DISPLACED" and vclass is not None:
            contention_penalty += int(vclass["contention_penalty"])
        lender = lenders[state["lender_id"]]
        state["insurance_cents"] = round_half_up(
            state["declared_value_cents"] * int(lender["insurance_rate_num"]),
            int(lender["insurance_rate_den"]))
        total_insurance += state["insurance_cents"]
        total_setup += state["setup_seconds"]
        if state["crate_class"]:
            total_residual += state["crate_residual"]

    audit_rows = []
    for oid in order_ids:
        state = canonical[oid]
        audit_rows.append({
            "object_id": oid,
            "placed_bay": state["bay"],
            "crate_class": state["crate_class"],
            "displaced": state["crate_code"] == "C_DISPLACED",
            "scheduled": state["scheduled"],
            "install_position": state["install_position"],
            "setup_seconds": state["setup_seconds"],
            "insurance_cents": state["insurance_cents"],
        })

    placed_objects = sum(1 for oid in order_ids if canonical[oid]["scheduled"])
    displacement_count = len(contention_rows)
    cycles_broken = len(removed)
    objective = [placed_objects, unschedulable_penalty, contention_penalty,
                 cycles_broken, total_residual]

    # ----------------------------------------------------------- emission
    os.makedirs(out_dir, exist_ok=True)
    line_artifacts = [
        ("canonical_objects.jsonl", canonical_rows),
        ("normalized_geometry.jsonl", geometry_rows),
        ("admissibility_matrix.jsonl", admissibility_rows),
        ("bay_candidate_matrix.jsonl", bay_candidate_rows),
        ("selected_placement.jsonl", selected_rows),
        ("rejected_placement.jsonl", rejected_rows),
        ("conservation_adjusted.jsonl", conservation_rows),
        ("crate_candidate_matrix.jsonl", crate_candidate_rows),
        ("crate_assignment.jsonl", crate_assignment_rows),
        ("crate_capacity_ledger.jsonl", ledger_rows),
        ("crate_contention_report.jsonl", contention_rows),
        ("transport_leg_manifest.jsonl", transport_rows),
        ("dependency_edges.jsonl", dependency_rows),
        ("cycle_resolution_audit.jsonl", removed),
        ("install_order.jsonl", install_rows),
        ("crew_requirement_ledger.jsonl", crew_rows),
        ("install_window_table.jsonl", window_rows),
        ("unschedulable_report.jsonl", unschedulable_rows),
        ("gallery_occupancy_map.jsonl", occupancy_rows),
        ("deterministic_audit.jsonl", audit_rows),
    ]
    for name, rows in line_artifacts:
        write_lines(os.path.join(out_dir, name), rows)

    write_object(os.path.join(out_dir, "penalty_summary.json"), {
        "unschedulable_penalty": unschedulable_penalty,
        "contention_penalty": contention_penalty,
        "cycles_broken": cycles_broken,
        "total_insurance_cents": total_insurance,
    })
    write_object(os.path.join(out_dir, "objective_tuple.json"), {
        "objective": objective,
        "order": list(objective_order["order"]),
    })

    program_path = os.path.abspath(__file__)
    manifest = {name: sha256_file(os.path.join(out_dir, name)) for name, _ in line_artifacts}
    for name in ("penalty_summary.json", "objective_tuple.json"):
        manifest[name] = sha256_file(os.path.join(out_dir, name))
    manifest[os.path.basename(program_path)] = sha256_file(program_path)
    write_object(os.path.join(out_dir, "integrity_manifest.json"), manifest)

    chain = chain_seed
    for name in sorted(manifest):
        chain = hashlib.sha256((chain + manifest[name]).encode("utf-8")).hexdigest()

    write_object(os.path.join(out_dir, "final_state.json"), {
        "chain": chain,
        "repair_converged": repair_converged,
        "cycle_edges_removed": cycles_broken,
        "final_cycle_count": final_cycle_count,
        "displacement_count": displacement_count,
        "unschedulable_count": len(unschedulable_rows),
        "placed_objects": placed_objects,
        "total_setup_seconds": total_setup,
        "total_insurance_cents": total_insurance,
        "objective": objective,
    })


def main(argv):
    if len(argv) != 3:
        sys.stderr.write("usage: plan.py <DATA_DIR> <OUT_DIR>\n")
        return 2
    build(argv[1], argv[2])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
