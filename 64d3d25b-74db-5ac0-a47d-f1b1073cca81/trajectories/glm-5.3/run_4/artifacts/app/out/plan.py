#!/usr/bin/env python3
"""Derive the travelling exhibition installation dossier from a snapshot.

Usage: python3 plan.py <DATA_DIR> <OUT_DIR>

Implements the staged derivation described by rules.md / schema.md / constants.md
/ reason_codes.md in the data directory, using integer and exact rational
arithmetic only (fractions.Fraction), never floating point.
"""

import csv
import hashlib
import json
import os
import sys
from fractions import Fraction
from math import gcd

LEN_FACTORS = {"MM": 1000, "CM": 10000, "IN": 25400}
MASS_FACTORS = {"G": 1, "KG": 1000}
CENTS_PER_UNIT = 100
SECONDS_PER_DAY = 3600

# removal precedence, low value is removed first
KIND_RANK = {"D_BAYSIZE": 0, "D_CONSERVATION": 1, "D_RULE": 2}
# kind precedence when two edges share from and to, high value wins
KIND_PRECEDENCE = {"D_RULE": 2, "D_CONSERVATION": 1, "D_BAYSIZE": 0}

JSONL_ARTIFACTS = [
    "canonical_objects.jsonl",
    "normalized_geometry.jsonl",
    "admissibility_matrix.jsonl",
    "bay_candidate_matrix.jsonl",
    "selected_placement.jsonl",
    "rejected_placement.jsonl",
    "conservation_adjusted.jsonl",
    "crate_candidate_matrix.jsonl",
    "crate_assignment.jsonl",
    "crate_capacity_ledger.jsonl",
    "crate_contention_report.jsonl",
    "transport_leg_manifest.jsonl",
    "dependency_edges.jsonl",
    "cycle_resolution_audit.jsonl",
    "install_order.jsonl",
    "crew_requirement_ledger.jsonl",
    "install_window_table.jsonl",
    "unschedulable_report.jsonl",
    "gallery_occupancy_map.jsonl",
    "deterministic_audit.jsonl",
]

ZERO_SEED = "0" * 64


# ---------------------------------------------------------------- helpers


def read_csv(data_dir, name):
    path = os.path.join(data_dir, name)
    rows = []
    with open(path, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            clean = {}
            for key, value in row.items():
                if key is None:
                    continue
                key = key.strip()
                clean[key] = "" if value is None else value.strip()
            rows.append(clean)
    return rows


def read_json(data_dir, name):
    with open(os.path.join(data_dir, name), encoding="utf-8") as handle:
        return json.load(handle)


def round_half_up(num, den):
    """Round the exact rational num/den (den > 0) half away toward +infinity."""
    if den == 0:
        return 0
    quotient, remainder = divmod(num, den)
    if 2 * remainder >= den:
        quotient += 1
    return quotient


def to_int(text):
    value = Fraction(text)
    if value.denominator == 1:
        return value.numerator
    return round_half_up(value.numerator, value.denominator)


def reduced_pair(num, den):
    """Reduce num/den to lowest terms with a positive denominator."""
    if den == 0:
        return 0, 1
    if num == 0:
        return 0, 1
    common = gcd(abs(num), abs(den))
    num //= common
    den //= common
    if den < 0:
        num, den = -num, -den
    return num, den


def scaled(text, factor):
    """Scale an exact declared quantity by an integer factor, rounding half up."""
    value = Fraction(text) * factor
    if value.denominator == 1:
        return value.numerator
    return round_half_up(value.numerator, value.denominator)


def write_jsonl(out_dir, name, records):
    lines = []
    for record in records:
        lines.append(json.dumps(record, sort_keys=True, separators=(",", ":")))
    write_bytes(out_dir, name, ("\n".join(lines) + "\n").encode("utf-8") if lines else b"")


def write_json_value(out_dir, name, value):
    text = json.dumps(value, sort_keys=True, indent=2)
    write_bytes(out_dir, name, (text + "\n").encode("utf-8"))


def write_bytes(out_dir, name, payload):
    with open(os.path.join(out_dir, name), "wb") as handle:
        handle.write(payload)


def find_cycle(nodes, adjacency):
    """Deterministic depth first search returning one cycle as a node list."""
    state = dict.fromkeys(nodes, 0)
    parent = {}
    for start in sorted(nodes):
        if state[start]:
            continue
        state[start] = 1
        stack = [(start, iter(sorted(adjacency[start])))]
        while stack:
            node, walker = stack[-1]
            descended = False
            for nxt in walker:
                if state[nxt] == 0:
                    state[nxt] = 1
                    parent[nxt] = node
                    stack.append((nxt, iter(sorted(adjacency[nxt]))))
                    descended = True
                    break
                if state[nxt] == 1:
                    cycle = [node]
                    cursor = node
                    while cursor != nxt:
                        cursor = parent[cursor]
                        cycle.append(cursor)
                    cycle.reverse()
                    return cycle
            if not descended:
                state[node] = 2
                stack.pop()
    return None


def count_cycles(nodes, adjacency):
    """Number of strongly connected components that contain a cycle."""
    index = {}
    low = {}
    on_stack = {}
    stack = []
    counter = [0]
    components = [0]

    for root in sorted(nodes):
        if root in index:
            continue
        index[root] = low[root] = counter[0]
        counter[0] += 1
        stack.append(root)
        on_stack[root] = True
        work = [(root, iter(sorted(adjacency[root])))]
        while work:
            node, walker = work[-1]
            advanced = False
            for nxt in walker:
                if nxt not in index:
                    index[nxt] = low[nxt] = counter[0]
                    counter[0] += 1
                    stack.append(nxt)
                    on_stack[nxt] = True
                    work.append((nxt, iter(sorted(adjacency[nxt]))))
                    advanced = True
                    break
                if on_stack.get(nxt):
                    low[node] = min(low[node], index[nxt])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                size = 0
                while True:
                    member = stack.pop()
                    on_stack[member] = False
                    size += 1
                    if member == node:
                        break
                if size > 1:
                    components[0] += 1
                else:
                    if node in adjacency.get(node, []):
                        components[0] += 1
    return components[0]


# ---------------------------------------------------------------- pipeline


def derive(data_dir, out_dir):
    horizon = read_json(data_dir, "horizon_config.json")
    setup_div = int(horizon.get("setup_div") or 700)
    chain_seed = horizon.get("chain_seed") or ZERO_SEED
    # the handling class margins are declared in the clearance unit, millimetres
    # unless the horizon says otherwise
    margin_factor = LEN_FACTORS.get(horizon.get("clearance_unit") or "MM", 1000)
    venue_end_exclusive = (horizon.get("venue_end_convention") or "exclusive") == "exclusive"
    conservation_end_inclusive = (horizon.get("conservation_end_convention") or "inclusive") == "inclusive"

    # ---- stage 1 canonical identity and units
    master_rows = read_csv(data_dir, "object_master.csv")
    order = []
    master = {}
    for row in master_rows:
        oid = row["object_id"]
        if oid not in master:
            master[oid] = row
            order.append(oid)

    dim_rows = {}
    for row in read_csv(data_dir, "object_dimensions.csv"):
        oid = row["object_id"]
        if oid not in dim_rows:
            dim_rows[oid] = row
    mass_rows = {}
    for row in read_csv(data_dir, "object_mass.csv"):
        oid = row["object_id"]
        if oid not in mass_rows:
            mass_rows[oid] = row
    value_rows = {}
    for row in read_csv(data_dir, "object_value.csv"):
        oid = row["object_id"]
        if oid not in value_rows:
            value_rows[oid] = row
    version_rows = {}
    for row in read_csv(data_dir, "object_version_map.csv"):
        oid = row["object_id"]
        version = to_int(row["version"])
        if oid not in version_rows or version > version_rows[oid]:
            version_rows[oid] = version
    orient_rows = {}
    for row in read_csv(data_dir, "object_orientation.csv"):
        oid = row["object_id"]
        if oid not in orient_rows:
            orient_rows[oid] = row

    materials = {row["material_code"]: row for row in read_csv(data_dir, "material_rules.csv")}
    handling = {row["handling_class"]: row for row in read_csv(data_dir, "handling_class_rules.csv")}

    objects = {}
    canonical_records = []
    for oid in order:
        row = master[oid]
        lender = row["lender_id"]
        material = row["material_code"]
        dim = dim_rows.get(oid)
        if dim is None:
            dim = {"width": "0", "height": "0", "depth": "0", "dim_unit": "MM"}
        factor = LEN_FACTORS[dim["dim_unit"]]
        width = scaled(dim["width"], factor)
        height = scaled(dim["height"], factor)
        depth = scaled(dim["depth"], factor)
        mass_row = mass_rows.get(oid) or {"mass": "0", "mass_unit": "G"}
        mass_g = scaled(mass_row["mass"], MASS_FACTORS[mass_row["mass_unit"]])
        value_row = value_rows.get(oid) or {"declared_value": "0"}
        value_cents = scaled(value_row["declared_value"], CENTS_PER_UNIT)
        material_rule = materials[material]
        hclass = material_rule["handling_class"]
        hrule = handling[hclass]
        objects[oid] = {
            "object_id": oid,
            "lender_id": lender,
            "material_code": material,
            "version": version_rows.get(oid, 0),
            "declared_value_cents": value_cents,
            "mass_g": mass_g,
            "conservation_class": material_rule["conservation_class"],
            "max_lux": to_int(material_rule["max_lux"]),
            "handling_class": hclass,
            "crew_class": hrule["crew_class"],
            "setup_base": to_int(hrule["setup_base"]),
            "clearance_w_mm": to_int(hrule["clearance_w"]),
            "clearance_d_mm": to_int(hrule["clearance_d"]),
            "clearance_h_mm": to_int(hrule["clearance_h"]),
            "handle_margin_um": to_int(hrule["handle_margin"]) * margin_factor,
            "width": width,
            "height": height,
            "depth": depth,
            "tip_allowed": to_int(orient_rows[oid]["tip_allowed"]) if oid in orient_rows else 0,
            "rotate_allowed": to_int(orient_rows[oid]["rotate_allowed"]) if oid in orient_rows else 0,
        }
        canonical_records.append(
            {
                "object_id": oid,
                "lender_id": lender,
                "material_code": material,
                "version": objects[oid]["version"],
                "declared_value_cents": value_cents,
                "mass_g": mass_g,
            }
        )

    # ---- stage 2 normalized geometry
    geometry_records = []
    for oid in order:
        obj = objects[oid]
        clearance_w = obj["width"] + 2 * obj["clearance_w_mm"] * margin_factor
        clearance_d = obj["depth"] + 2 * obj["clearance_d_mm"] * margin_factor
        clearance_h = obj["height"] + obj["clearance_h_mm"] * margin_factor
        obj["clearance_w"] = clearance_w
        obj["clearance_d"] = clearance_d
        obj["clearance_h"] = clearance_h
        aspect_num, aspect_den = reduced_pair(obj["height"], obj["width"])
        obj["aspect_num"] = aspect_num
        obj["aspect_den"] = aspect_den
        handle = obj["handle_margin_um"]
        obj["handling_w"] = obj["width"] + 2 * handle
        obj["handling_h"] = obj["height"] + 2 * handle
        obj["handling_d"] = obj["depth"] + 2 * handle
        obj["handling_volume"] = obj["handling_w"] * obj["handling_h"] * obj["handling_d"]
        geometry_records.append(
            {
                "object_id": oid,
                "clearance_w": clearance_w,
                "clearance_h": clearance_h,
                "clearance_d": clearance_d,
                "aspect_num": aspect_num,
                "aspect_den": aspect_den,
            }
        )

    # ---- stage 3 orientation admissibility
    admissibility_records = []
    for oid in order:
        obj = objects[oid]
        orientations = [0]
        if obj["rotate_allowed"]:
            orientations.append(1)
        if obj["tip_allowed"]:
            orientations.extend([2, 3])
        obj["orientations"] = orientations
        admissibility_records.append(
            {
                "object_id": oid,
                "orientations": orientations,
                "tip_allowed": obj["tip_allowed"],
                "rotate_allowed": obj["rotate_allowed"],
            }
        )

    def oriented(obj, index):
        cw, cd, ch = obj["clearance_w"], obj["clearance_d"], obj["clearance_h"]
        if index == 0:
            return cw, cd, ch
        if index == 1:
            return cd, cw, ch
        if index == 2:
            return cw, ch, cd
        return ch, cw, cd

    # ---- stage 4 bay candidates
    bays = {}
    for row in read_csv(data_dir, "gallery_bays.csv"):
        bays[row["bay_id"]] = {
            "bay_id": row["bay_id"],
            "interior_w": to_int(row["interior_w"]) * 1000,
            "interior_h": to_int(row["interior_h"]) * 1000,
            "interior_d": to_int(row["interior_d"]) * 1000,
            "lux_level": to_int(row["lux_level"]),
            "bay_priority": to_int(row["bay_priority"]),
        }
    bay_ids_sorted = sorted(bays)
    bay_scan_order = sorted(bays, key=lambda b: (bays[b]["bay_priority"], b))

    bay_candidate_records = []
    for oid in order:
        obj = objects[oid]
        obj["bay_fit"] = {}
        for bay_id in bay_ids_sorted:
            bay = bays[bay_id]
            fitting = None
            for index in obj["orientations"]:
                fw, fd, fh = oriented(obj, index)
                if fw <= bay["interior_w"] and fd < bay["interior_d"] and fh <= bay["interior_h"]:
                    fitting = index
                    break
            obj["bay_fit"][bay_id] = fitting
            bay_candidate_records.append(
                {
                    "object_id": oid,
                    "bay_id": bay_id,
                    "fits": fitting is not None,
                    "orientation": -1 if fitting is None else fitting,
                }
            )

    # ---- stage 5 selected placement
    selected_records = []
    rejected_records = []
    for oid in order:
        obj = objects[oid]
        chosen = None
        for bay_id in bay_scan_order:
            if obj["bay_fit"][bay_id] is not None:
                chosen = (bay_id, obj["bay_fit"][bay_id])
                break
        if chosen is None:
            obj["stage5"] = None
            code = "P_NO_ORIENTATION" if obj["orientations"] == [0] else "P_NO_FIT"
            obj["reject_code"] = code
            rejected_records.append({"object_id": oid, "code": code})
        else:
            obj["stage5"] = chosen
            selected_records.append(
                {
                    "object_id": oid,
                    "bay_id": chosen[0],
                    "orientation": chosen[1],
                    "code": "P_OK",
                }
            )

    # ---- stage 6 conservation adjustment
    blocked = {
        (row["bay_id"], row["blocked_class"]) for row in read_csv(data_dir, "bay_exceptions.csv")
    }
    conservation_rules = {
        row["conservation_class"]: row for row in read_csv(data_dir, "conservation_rules.csv")
    }
    # sensitivity ranks the conservation classes from the conservation rules table,
    # so that a more sensitive class is installed before a less sensitive one
    sensitivity = {}
    for rank, cclass in enumerate(sorted(conservation_rules), 1):
        sensitivity[cclass] = rank
    conservation_records = []
    for oid in order:
        obj = objects[oid]
        if obj["stage5"] is None:
            obj["placed_bay"] = None
            obj["placed_orientation"] = -1
            conservation_records.append(
                {
                    "object_id": oid,
                    "bay_id": "",
                    "orientation": -1,
                    "code": obj["reject_code"],
                }
            )
            continue
        cclass = obj["conservation_class"]
        needs_dark = to_int(conservation_rules[cclass]["needs_dark"]) != 0
        geometric = [b for b in bay_scan_order if obj["bay_fit"][b] is not None]
        excluded_by_exception = []
        chosen = None
        for bay_id in geometric:
            bay = bays[bay_id]
            if (bay_id, cclass) in blocked:
                excluded_by_exception.append(bay_id)
                continue
            if bay["lux_level"] > obj["max_lux"]:
                continue
            if needs_dark and bay["lux_level"] > 0:
                continue
            chosen = (bay_id, obj["bay_fit"][bay_id])
            break
        if chosen is None:
            obj["placed_bay"] = None
            obj["placed_orientation"] = -1
            if len(geometric) == 1 and excluded_by_exception:
                code = "P_BAY_EXCEPTION"
            else:
                code = "P_CONSERVATION_BLOCK"
            obj["reject_code"] = code
            rejected_records.append({"object_id": oid, "code": code})
            conservation_records.append(
                {"object_id": oid, "bay_id": "", "orientation": -1, "code": code}
            )
        else:
            obj["placed_bay"] = chosen[0]
            obj["placed_orientation"] = chosen[1]
            obj["reject_code"] = None
            conservation_records.append(
                {
                    "object_id": oid,
                    "bay_id": chosen[0],
                    "orientation": chosen[1],
                    "code": "P_OK",
                }
            )
    rejected_records.sort(key=lambda r: (r["object_id"], r["code"]))

    # ---- stage 7 crate candidates
    crates = {}
    for row in read_csv(data_dir, "crate_catalog.csv"):
        crates[row["crate_class"]] = {
            "crate_class": row["crate_class"],
            "inner_w": to_int(row["inner_w"]) * 1000,
            "inner_h": to_int(row["inner_h"]) * 1000,
            "inner_d": to_int(row["inner_d"]) * 1000,
            "count": to_int(row["count"]),
            "transport_mode": row["transport_mode"],
        }
    crate_ids_sorted = sorted(crates)
    crate_candidate_records = []
    for oid in sorted(objects):
        obj = objects[oid]
        obj["crate_fit"] = {}
        obj["crate_residual"] = {}
        handling_sorted = sorted([obj["handling_w"], obj["handling_h"], obj["handling_d"]])
        entries = []
        for crate_id in crate_ids_sorted:
            crate = crates[crate_id]
            inner_sorted = sorted([crate["inner_w"], crate["inner_h"], crate["inner_d"]])
            if all(h <= i for h, i in zip(handling_sorted, inner_sorted)):
                residual = (
                    crate["inner_w"] * crate["inner_h"] * crate["inner_d"]
                ) - obj["handling_volume"]
                obj["crate_fit"][crate_id] = True
                obj["crate_residual"][crate_id] = residual
                entries.append((residual, crate_id))
        entries.sort()
        obj["crate_entries"] = entries
        obj["primary_crate"] = entries[0][1] if entries else None
        for residual, crate_id in entries:
            crate_candidate_records.append(
                {"object_id": oid, "crate_class": crate_id, "residual": residual}
            )

    # ---- setup seconds (needed by the install order)
    for oid in order:
        obj = objects[oid]
        obj["setup_seconds"] = round_half_up(obj["setup_base"] * obj["mass_g"], setup_div)
        obj["required_days"] = -(-obj["setup_seconds"] // SECONDS_PER_DAY)

    # ---- stage 10 dependency edges
    edges = {}

    def add_edge(source, target, kind, weight):
        existing = edges.get((source, target))
        if existing is not None and KIND_PRECEDENCE[existing["kind"]] >= KIND_PRECEDENCE[kind]:
            return
        edges[(source, target)] = {"from": source, "to": target, "kind": kind, "weight": weight}

    for row in read_csv(data_dir, "dependency_rules.csv"):
        source, target = row["from_object"], row["to_object"]
        if source in objects and target in objects:
            add_edge(source, target, "D_RULE", to_int(row["edge_priority"]))

    by_bay = {}
    for oid in order:
        bay = objects[oid]["placed_bay"]
        if bay:
            by_bay.setdefault(bay, []).append(oid)
    for bay_id in sorted(by_bay):
        members = sorted(by_bay[bay_id])
        for source in members:
            for target in members:
                if source == target:
                    continue
                src, dst = objects[source], objects[target]
                if src["handling_volume"] > dst["handling_volume"]:
                    add_edge(source, target, "D_BAYSIZE", src["handling_volume"])
                src_rank = sensitivity.get(src["conservation_class"], 0)
                dst_rank = sensitivity.get(dst["conservation_class"], 0)
                if src_rank > dst_rank:
                    add_edge(source, target, "D_CONSERVATION", src["handling_volume"])

    # ---- stage 11 cycle resolution
    adjacency = {oid: set() for oid in order}
    for (source, target) in edges:
        adjacency[source].add(target)
    cycle_records = []
    guard = 0
    converged = True
    while True:
        cycle = find_cycle(order, adjacency)
        if cycle is None:
            break
        guard += 1
        if guard > len(edges) + len(order) + 10:
            converged = False
            break
        cycle_edges = []
        for offset in range(len(cycle)):
            source = cycle[offset]
            target = cycle[(offset + 1) % len(cycle)]
            record = edges.get((source, target))
            if record is not None:
                cycle_edges.append(record)
        if not cycle_edges:
            converged = False
            break
        victim = min(
            cycle_edges,
            key=lambda r: (KIND_RANK[r["kind"]], r["weight"], r["from"], r["to"]),
        )
        cycle_records.append(victim)
        adjacency[victim["from"]].discard(victim["to"])
    final_cycle_count = count_cycles(order, adjacency)
    dependency_records = [
        edges[key]
        for key in sorted(edges, key=lambda e: (e[0], e[1]))
        if key[1] in adjacency[key[0]]
    ]

    # ---- stage 12 install order
    remaining_preds = {oid: 0 for oid in order}
    successors = {oid: [] for oid in order}
    for record in dependency_records:
        remaining_preds[record["to"]] += 1
        successors[record["from"]].append(record["to"])
    ready = sorted(
        [oid for oid in order if remaining_preds[oid] == 0],
        key=lambda oid: (objects[oid]["setup_seconds"], oid),
    )
    install_position = {}
    position = 1
    while ready:
        oid = ready.pop(0)
        install_position[oid] = position
        position += 1
        for target in sorted(successors[oid]):
            remaining_preds[target] -= 1
            if remaining_preds[target] == 0:
                ready.append(target)
        ready.sort(key=lambda o: (objects[o]["setup_seconds"], o))
    for oid in order:
        if oid not in install_position:
            install_position[oid] = position
            position += 1
    install_sequence = [oid for oid, _ in sorted(install_position.items(), key=lambda kv: kv[1])]
    install_records = [
        {"position": index, "object_id": oid} for index, oid in enumerate(install_sequence, 1)
    ]

    # ---- stage 8 shared crate capacity
    prior = {
        row["crate_class"]: to_int(row["consumed_count"]) for row in read_csv(data_dir, "prior_state.csv")
    }
    reservations = {}
    for row in read_csv(data_dir, "crate_reservations.csv"):
        key = (row["crate_class"], row["lender_id"])
        reservations[key] = reservations.get(key, 0) + to_int(row["reserved_count"])

    available = {}
    for crate_id in crate_ids_sorted:
        available[crate_id] = crates[crate_id]["count"] - prior.get(crate_id, 0)

    assignment = {}
    for oid in install_sequence:
        obj = objects[oid]
        if obj["placed_bay"] is None:
            continue
        options = [c for c in obj["crate_fit"] if available[c] > 0]
        if not options:
            continue
        lender = obj["lender_id"]
        chosen = min(
            options,
            key=lambda c: (
                0 if reservations.get((c, lender), 0) > 0 else 1,
                obj["crate_residual"][c],
                c,
            ),
        )
        available[chosen] -= 1
        if reservations.get((chosen, lender), 0) > 0:
            reservations[(chosen, lender)] -= 1
        assignment[oid] = chosen

    crate_assignment_records = []
    contention_records = []
    displacement_count = 0
    total_residual = 0
    for oid in sorted(objects):
        obj = objects[oid]
        primary = obj["primary_crate"]
        assigned = assignment.get(oid)
        if assigned is None:
            code = "C_NO_CRATE"
            displaced = False
        elif assigned == primary:
            code = "C_PRIMARY"
            displaced = False
            total_residual += obj["crate_residual"][assigned]
        else:
            code = "C_DISPLACED"
            displaced = True
            displacement_count += 1
            total_residual += obj["crate_residual"][assigned]
            contention_records.append(
                {"object_id": oid, "wanted_class": primary, "assigned_class": assigned}
            )
        obj["crate_class"] = "" if assigned is None else assigned
        obj["displaced"] = displaced
        obj["crate_code"] = code
        crate_assignment_records.append(
            {
                "object_id": oid,
                "crate_class": obj["crate_class"],
                "code": code,
                "primary_class": "" if primary is None else primary,
            }
        )

    ledger_records = [
        {
            "crate_class": crate_id,
            "catalog_count": crates[crate_id]["count"],
            "prior_consumed": prior.get(crate_id, 0),
            "remaining": available[crate_id],
        }
        for crate_id in crate_ids_sorted
    ]

    # ---- stage 9 transport legs
    transport_rules = {
        row["lender_id"]: row for row in read_csv(data_dir, "transport_rules.csv")
    }
    transport_records = []
    for oid in order:
        obj = objects[oid]
        crate_id = assignment.get(oid)
        if crate_id is None:
            continue
        mode = crates[crate_id]["transport_mode"]
        rule = transport_rules.get(obj["lender_id"])
        exception = False
        if rule is not None and rule["forbidden_mode"] == mode:
            mode = rule["fallback_mode"]
            exception = True
        transport_records.append(
            {
                "object_id": oid,
                "crate_class": crate_id,
                "mode": mode,
                "exception": exception,
            }
        )

    # ---- stage 13 windows
    venue = {row["bay_id"]: row for row in read_csv(data_dir, "venue_windows.csv")}
    crew = {row["crew_class"]: row for row in read_csv(data_dir, "crew_roster.csv")}
    crew_records = []
    window_records = []
    unschedulable_records = []
    scheduled_count = 0
    total_setup_seconds = 0
    for oid in order:
        obj = objects[oid]
        crew_records.append(
            {
                "object_id": oid,
                "crew_class": obj["crew_class"],
                "setup_seconds": obj["setup_seconds"],
            }
        )
        total_setup_seconds += obj["setup_seconds"]
        bay_id = obj["placed_bay"]
        window_start = -1
        window_end = -1
        if bay_id is None:
            code = "S_NO_PLACEMENT"
        else:
            crule = conservation_rules[obj["conservation_class"]]
            vrule = venue[bay_id]
            krule = crew[obj["crew_class"]]
            window_start = max(
                to_int(crule["setup_start"]),
                to_int(vrule["access_start"]),
                to_int(krule["avail_start"]),
            )
            venue_end = to_int(vrule["access_end"]) - (1 if venue_end_exclusive else 0)
            cons_end = to_int(crule["setup_end"]) + (0 if conservation_end_inclusive else 1)
            window_end = min(
                cons_end,
                venue_end,
                to_int(krule["avail_end"]),
            )
            days_available = window_end - window_start + 1
            if assignment.get(oid) is None:
                code = "S_NO_CRATE"
            elif days_available <= 0:
                code = "S_NO_WINDOW"
            elif obj["required_days"] > days_available:
                code = "S_SHORT_WINDOW"
            else:
                code = "S_OK"
        obj["scheduled"] = code == "S_OK"
        if code == "S_OK":
            scheduled_count += 1
        else:
            unschedulable_records.append({"object_id": oid, "code": code})
        window_records.append(
            {
                "object_id": oid,
                "bay_id": "" if bay_id is None else bay_id,
                "window_start": window_start,
                "window_end": window_end,
                "required_days": obj["required_days"],
                "code": code,
            }
        )
    unschedulable_records.sort(key=lambda r: (r["object_id"], r["code"]))

    # ---- stage 14 dossier
    occupancy_records = []
    for bay_id in bay_ids_sorted:
        members = sorted(oid for oid in order if objects[oid]["placed_bay"] == bay_id)
        occupancy_records.append(
            {"bay_id": bay_id, "object_count": len(members), "objects": members}
        )

    lenders = {row["lender_id"]: row for row in read_csv(data_dir, "lender_master.csv")}
    value_classes = [
        {
            "value_class": row["value_class"],
            "min_value": to_int(row["min_value"]),
            "min_value_scaled": scaled(row["min_value"], CENTS_PER_UNIT),
            "unschedulable_penalty": to_int(row["unschedulable_penalty"]),
            "contention_penalty": to_int(row["contention_penalty"]),
        }
        for row in read_csv(data_dir, "value_class_rules.csv")
    ]
    value_classes.sort(key=lambda c: (-c["min_value"], c["value_class"]))

    def value_class_of(oid):
        cents = objects[oid]["declared_value_cents"]
        for entry in value_classes:
            if cents >= entry["min_value_scaled"]:
                return entry
        return value_classes[-1] if value_classes else None

    unschedulable_penalty = 0
    contention_penalty = 0
    total_insurance_cents = 0
    audit_records = []
    for oid in order:
        obj = objects[oid]
        entry = value_class_of(oid)
        if not obj["scheduled"] and entry is not None:
            unschedulable_penalty += entry["unschedulable_penalty"]
        if obj["displaced"] and entry is not None:
            contention_penalty += entry["contention_penalty"]
        lender_rule = lenders.get(obj["lender_id"])
        if lender_rule is None:
            insurance = 0
        else:
            insurance = round_half_up(
                obj["declared_value_cents"] * to_int(lender_rule["insurance_rate_num"]),
                to_int(lender_rule["insurance_rate_den"]),
            )
        obj["insurance_cents"] = insurance
        total_insurance_cents += insurance
        audit_records.append(
            {
                "object_id": oid,
                "placed_bay": "" if obj["placed_bay"] is None else obj["placed_bay"],
                "crate_class": obj["crate_class"],
                "displaced": obj["displaced"],
                "scheduled": obj["scheduled"],
                "install_position": install_position[oid],
                "setup_seconds": obj["setup_seconds"],
                "insurance_cents": insurance,
            }
        )

    cycles_broken = len(cycle_records)

    write_jsonl(out_dir, "canonical_objects.jsonl", canonical_records)
    write_jsonl(out_dir, "normalized_geometry.jsonl", geometry_records)
    write_jsonl(out_dir, "admissibility_matrix.jsonl", admissibility_records)
    write_jsonl(out_dir, "bay_candidate_matrix.jsonl", bay_candidate_records)
    write_jsonl(out_dir, "selected_placement.jsonl", selected_records)
    write_jsonl(out_dir, "rejected_placement.jsonl", rejected_records)
    write_jsonl(out_dir, "conservation_adjusted.jsonl", conservation_records)
    write_jsonl(out_dir, "crate_candidate_matrix.jsonl", crate_candidate_records)
    write_jsonl(out_dir, "crate_assignment.jsonl", crate_assignment_records)
    write_jsonl(out_dir, "crate_capacity_ledger.jsonl", ledger_records)
    write_jsonl(out_dir, "crate_contention_report.jsonl", contention_records)
    write_jsonl(out_dir, "transport_leg_manifest.jsonl", transport_records)
    write_jsonl(out_dir, "dependency_edges.jsonl", dependency_records)
    write_jsonl(out_dir, "cycle_resolution_audit.jsonl", cycle_records)
    write_jsonl(out_dir, "install_order.jsonl", install_records)
    write_jsonl(out_dir, "crew_requirement_ledger.jsonl", crew_records)
    write_jsonl(out_dir, "install_window_table.jsonl", window_records)
    write_jsonl(out_dir, "unschedulable_report.jsonl", unschedulable_records)
    write_jsonl(out_dir, "gallery_occupancy_map.jsonl", occupancy_records)
    write_jsonl(out_dir, "deterministic_audit.jsonl", audit_records)

    penalty_summary = {
        "unschedulable_penalty": unschedulable_penalty,
        "contention_penalty": contention_penalty,
        "cycles_broken": cycles_broken,
        "total_insurance_cents": total_insurance_cents,
    }
    write_json_value(out_dir, "penalty_summary.json", penalty_summary)

    component_values = {
        "placed_objects": scheduled_count,
        "unschedulable_penalty": unschedulable_penalty,
        "contention_penalty": contention_penalty,
        "cycles_broken": cycles_broken,
        "total_residual": total_residual,
    }
    objective_order = read_json(data_dir, "objective_order.json")
    names = objective_order.get("order") or list(component_values)
    objective = [component_values[name] for name in names]
    write_json_value(
        out_dir,
        "objective_tuple.json",
        {"objective": objective, "order": list(names)},
    )

    program_source = os.path.abspath(__file__)
    with open(program_source, "rb") as handle:
        program_bytes = handle.read()
    program_name = os.path.basename(program_source)
    if program_name != "plan.py":
        # the output directory must hold the deliverables under their fixed names
        program_name = "plan.py"
    write_bytes(out_dir, program_name, program_bytes)

    manifest = {}
    hashed_names = JSONL_ARTIFACTS + [
        "penalty_summary.json",
        "objective_tuple.json",
        program_name,
    ]
    for name in hashed_names:
        with open(os.path.join(out_dir, name), "rb") as handle:
            manifest[name] = hashlib.sha256(handle.read()).hexdigest()
    write_json_value(out_dir, "integrity_manifest.json", manifest)

    chain = chain_seed
    for name in sorted(manifest):
        chain = hashlib.sha256((chain + manifest[name]).encode("utf-8")).hexdigest()

    final_state = {
        "chain": chain,
        "repair_converged": converged and final_cycle_count == 0,
        "cycle_edges_removed": cycles_broken,
        "final_cycle_count": final_cycle_count,
        "displacement_count": displacement_count,
        "unschedulable_count": len(unschedulable_records),
        "placed_objects": scheduled_count,
        "total_setup_seconds": total_setup_seconds,
        "total_insurance_cents": total_insurance_cents,
        "objective": objective,
    }
    write_json_value(out_dir, "final_state.json", final_state)


def main(argv):
    if len(argv) != 3:
        sys.stderr.write("usage: plan.py <DATA_DIR> <OUT_DIR>\n")
        return 2
    data_dir = os.path.abspath(argv[1])
    out_dir = os.path.abspath(argv[2])
    os.makedirs(out_dir, exist_ok=True)
    derive(data_dir, out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
