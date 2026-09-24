#!/usr/bin/env python3
"""Derive the installation dossier from an exhibition snapshot.

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


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        rows = []
        for row in csv.DictReader(fh):
            clean = {}
            for key, value in row.items():
                if key is None:
                    continue
                clean[key.strip()] = value.strip() if isinstance(value, str) else ""
            if any(v != "" for v in clean.values()):
                rows.append(clean)
        return rows


def read_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def to_int(text, default=0):
    text = (text or "").strip()
    if text == "":
        return default
    return int(text)


def to_bool(text):
    return to_int(text, 0) != 0


def round_half_up(num, den):
    """Round num/den half up. den must be non zero."""
    if den == 0:
        raise ZeroDivisionError("rounding by zero denominator")
    negative = False
    if den < 0:
        num, den = -num, -den
    if num < 0:
        negative = True
        num = -num
    quotient, remainder = divmod(num, den)
    if 2 * remainder >= den:
        quotient += 1
    return -quotient if negative else quotient


def ceil_div(num, den):
    return -((-num) // den)


def reduced_ratio(num, den):
    """Reduce num/den; a zero denominator yields the sentinel 0/1."""
    if den == 0 or num == 0:
        return 0, 1
    common = gcd(abs(num), abs(den))
    num //= common
    den //= common
    if den < 0:
        num, den = -num, -den
    return num, den


def write_jsonl(path, records):
    with open(path, "w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, sort_keys=True, separators=(",", ":")))
            fh.write("\n")


def write_json(path, value):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(value, sort_keys=True, indent=2))
        fh.write("\n")


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------
# orientation convention (stage 3)
# --------------------------------------------------------------------------

def orientation_box(index, clearance_w, clearance_h, clearance_d):
    """Return (footprint_w, footprint_d, vertical) for an orientation index."""
    if index == 0:
        return clearance_w, clearance_d, clearance_h
    if index == 1:
        return clearance_d, clearance_w, clearance_h
    if index == 2:
        return clearance_w, clearance_h, clearance_d
    return clearance_h, clearance_w, clearance_d


def admissible_orientations(tip_allowed, rotate_allowed):
    orientations = [0]
    if rotate_allowed:
        orientations.append(1)
    if tip_allowed:
        orientations.extend([2, 3])
    return orientations


# --------------------------------------------------------------------------
# cycle detection (stage 11)
# --------------------------------------------------------------------------

def find_cycle(nodes, adjacency):
    """Deterministic DFS cycle search; returns a list of (from, to) or None."""
    color = {node: 0 for node in nodes}
    for start in nodes:
        if color[start] != 0:
            continue
        color[start] = 1
        stack = [(start, iter(adjacency.get(start, ())))]
        while stack:
            node, iterator = stack[-1]
            advanced = False
            for nxt in iterator:
                state = color.get(nxt, 0)
                if state == 1:
                    path = [frm for frm, _ in stack]
                    ring = path[path.index(nxt):]
                    return [
                        (ring[i], ring[(i + 1) % len(ring)])
                        for i in range(len(ring))
                    ]
                if state == 0:
                    color[nxt] = 1
                    stack.append((nxt, iter(adjacency.get(nxt, ()))))
                    advanced = True
                    break
            if not advanced:
                color[node] = 2
                stack.pop()
    return None


# --------------------------------------------------------------------------
# main derivation
# --------------------------------------------------------------------------

def derive(data_dir, out_dir):
    config = read_json(os.path.join(data_dir, "horizon_config.json"), {})
    clearance_unit = config.get("clearance_unit", "MM")
    setup_div = int(config.get("setup_div", 700))
    chain_seed = config.get("chain_seed", "0" * 64)
    venue_exclusive = config.get("venue_end_convention", "exclusive") == "exclusive"
    conservation_exclusive = (
        config.get("conservation_end_convention", "inclusive") == "exclusive"
    )
    declared_factor = LENGTH_FACTOR[clearance_unit]

    master_rows = read_csv(os.path.join(data_dir, "object_master.csv"))
    dimension_rows = read_csv(os.path.join(data_dir, "object_dimensions.csv"))
    mass_rows = read_csv(os.path.join(data_dir, "object_mass.csv"))
    value_rows = read_csv(os.path.join(data_dir, "object_value.csv"))
    orientation_rows = read_csv(os.path.join(data_dir, "object_orientation.csv"))
    version_rows = read_csv(os.path.join(data_dir, "object_version_map.csv"))
    material_rows = read_csv(os.path.join(data_dir, "material_rules.csv"))
    handling_rows = read_csv(os.path.join(data_dir, "handling_class_rules.csv"))
    bay_rows = read_csv(os.path.join(data_dir, "gallery_bays.csv"))
    exception_rows = read_csv(os.path.join(data_dir, "bay_exceptions.csv"))
    crate_rows = read_csv(os.path.join(data_dir, "crate_catalog.csv"))
    reservation_rows = read_csv(os.path.join(data_dir, "crate_reservations.csv"))
    prior_rows = read_csv(os.path.join(data_dir, "prior_state.csv"))
    conservation_rows = read_csv(os.path.join(data_dir, "conservation_rules.csv"))
    venue_rows = read_csv(os.path.join(data_dir, "venue_windows.csv"))
    dependency_rows = read_csv(os.path.join(data_dir, "dependency_rules.csv"))
    crew_rows = read_csv(os.path.join(data_dir, "crew_roster.csv"))
    transport_rows = read_csv(os.path.join(data_dir, "transport_rules.csv"))
    lender_rows = read_csv(os.path.join(data_dir, "lender_master.csv"))
    value_class_rows = read_csv(os.path.join(data_dir, "value_class_rules.csv"))
    objective_order = read_json(
        os.path.join(data_dir, "objective_order.json"), {"order": [], "sense": []}
    )

    # -- reference tables ---------------------------------------------------
    materials = {row["material_code"]: row for row in material_rows}
    handling = {row["handling_class"]: row for row in handling_rows}
    conservation = {row["conservation_class"]: row for row in conservation_rows}
    crew_windows = {row["crew_class"]: row for row in crew_rows}
    venue_windows = {row["bay_id"]: row for row in venue_rows}
    lenders = {row["lender_id"]: row for row in lender_rows}

    # A higher conservation class label denotes a more sensitive object.
    sensitivity_rank = {
        name: index for index, name in enumerate(sorted(conservation))
    }

    blocked_classes = {}
    for row in exception_rows:
        blocked_classes.setdefault(row["bay_id"], set()).add(row["blocked_class"])

    bays = {}
    for row in bay_rows:
        bays[row["bay_id"]] = {
            "bay_id": row["bay_id"],
            "interior_w": to_int(row["interior_w"]) * declared_factor,
            "interior_h": to_int(row["interior_h"]) * declared_factor,
            "interior_d": to_int(row["interior_d"]) * declared_factor,
            "lux_level": to_int(row["lux_level"]),
            "bay_priority": to_int(row["bay_priority"]),
        }
    bay_ids_sorted = sorted(bays)
    bay_scan_order = sorted(
        bays.values(), key=lambda bay: (bay["bay_priority"], bay["bay_id"])
    )

    crates = {}
    for row in crate_rows:
        inner_w = to_int(row["inner_w"]) * declared_factor
        inner_h = to_int(row["inner_h"]) * declared_factor
        inner_d = to_int(row["inner_d"]) * declared_factor
        crates[row["crate_class"]] = {
            "crate_class": row["crate_class"],
            "dims": sorted([inner_w, inner_h, inner_d]),
            "volume": inner_w * inner_h * inner_d,
            "count": to_int(row["count"]),
            "transport_mode": row["transport_mode"],
        }

    prior_consumed = {}
    for row in prior_rows:
        prior_consumed[row["crate_class"]] = prior_consumed.get(
            row["crate_class"], 0
        ) + to_int(row["consumed_count"])

    reservations = {}
    for row in reservation_rows:
        key = (row["crate_class"], row["lender_id"])
        reservations[key] = reservations.get(key, 0) + to_int(row["reserved_count"])

    forbidden_modes = {}
    for row in transport_rows:
        forbidden_modes.setdefault(row["lender_id"], []).append(
            (row["forbidden_mode"], row["fallback_mode"])
        )

    value_classes = sorted(
        (
            {
                "value_class": row["value_class"],
                "min_value": to_int(row["min_value"]),
                "unschedulable_penalty": to_int(row["unschedulable_penalty"]),
                "contention_penalty": to_int(row["contention_penalty"]),
            }
            for row in value_class_rows
        ),
        key=lambda item: (-item["min_value"], item["value_class"]),
    )

    # -- stage 1 canonical identity and units -------------------------------
    order = []
    canonical = {}
    for row in master_rows:
        object_id = row["object_id"]
        if object_id in canonical:
            continue
        order.append(object_id)
        canonical[object_id] = row

    versions = {}
    for row in version_rows:
        object_id = row["object_id"]
        version = to_int(row["version"])
        if object_id not in versions or version > versions[object_id]:
            versions[object_id] = version

    def first_by_object(rows):
        table = {}
        for row in rows:
            table.setdefault(row["object_id"], row)
        return table

    dimensions = first_by_object(dimension_rows)
    masses = first_by_object(mass_rows)
    values = first_by_object(value_rows)
    orientation_flags = first_by_object(orientation_rows)

    objects = {}
    for object_id in order:
        base = canonical[object_id]
        dim = dimensions.get(object_id)
        if dim is None:
            width_um = height_um = depth_um = 0
        else:
            factor = LENGTH_FACTOR[dim["dim_unit"]]
            width_um = to_int(dim["width"]) * factor
            height_um = to_int(dim["height"]) * factor
            depth_um = to_int(dim["depth"]) * factor
        mass_row = masses.get(object_id)
        mass_g = (
            to_int(mass_row["mass"]) * MASS_FACTOR[mass_row["mass_unit"]]
            if mass_row
            else 0
        )
        value_row = values.get(object_id)
        declared_value = to_int(value_row["declared_value"]) if value_row else 0
        flags = orientation_flags.get(object_id)
        tip_allowed = to_bool(flags["tip_allowed"]) if flags else False
        rotate_allowed = to_bool(flags["rotate_allowed"]) if flags else False

        material = materials[base["material_code"]]
        handling_rule = handling[material["handling_class"]]

        margin_w = to_int(handling_rule["clearance_w"]) * declared_factor
        margin_d = to_int(handling_rule["clearance_d"]) * declared_factor
        margin_h = to_int(handling_rule["clearance_h"]) * declared_factor
        handle_margin = to_int(handling_rule["handle_margin"]) * declared_factor
        handling_dims = [
            width_um + handle_margin,
            height_um + handle_margin,
            depth_um + handle_margin,
        ]
        aspect_num, aspect_den = reduced_ratio(height_um, width_um)

        objects[object_id] = {
            "object_id": object_id,
            "lender_id": base["lender_id"],
            "material_code": base["material_code"],
            "version": versions.get(object_id, 0),
            "declared_value": declared_value,
            "declared_value_cents": declared_value * CENTS_PER_UNIT,
            "mass_g": mass_g,
            "width_um": width_um,
            "height_um": height_um,
            "depth_um": depth_um,
            "tip_allowed": tip_allowed,
            "rotate_allowed": rotate_allowed,
            "conservation_class": material["conservation_class"],
            "max_lux": to_int(material["max_lux"]),
            "handling_class": material["handling_class"],
            "crew_class": handling_rule["crew_class"],
            "setup_base": to_int(handling_rule["setup_base"]),
            "clearance_w": width_um + 2 * margin_w,
            "clearance_d": depth_um + 2 * margin_d,
            "clearance_h": height_um + margin_h,
            "aspect_num": aspect_num,
            "aspect_den": aspect_den,
            "orientations": admissible_orientations(tip_allowed, rotate_allowed),
            "handling_dims": handling_dims,
            "handling_volume": handling_dims[0] * handling_dims[1] * handling_dims[2],
        }

    # -- stage 4 bay candidates ---------------------------------------------
    bay_candidates = {}
    for object_id in order:
        state = objects[object_id]
        per_bay = {}
        for bay_id in bay_ids_sorted:
            bay = bays[bay_id]
            fitting = -1
            for index in state["orientations"]:
                foot_w, foot_d, vertical = orientation_box(
                    index,
                    state["clearance_w"],
                    state["clearance_h"],
                    state["clearance_d"],
                )
                if (
                    foot_w <= bay["interior_w"]
                    and foot_d < bay["interior_d"]
                    and vertical <= bay["interior_h"]
                ):
                    fitting = index
                    break
            per_bay[bay_id] = fitting
        bay_candidates[object_id] = per_bay

    # -- stage 5 selected placement -----------------------------------------
    selected = {}
    for object_id in order:
        state = objects[object_id]
        chosen = None
        for bay in bay_scan_order:
            index = bay_candidates[object_id][bay["bay_id"]]
            if index >= 0:
                chosen = (bay["bay_id"], index)
                break
        if chosen is None:
            code = "P_NO_ORIENTATION" if len(state["orientations"]) == 1 else "P_NO_FIT"
            selected[object_id] = {"bay_id": "", "orientation": -1, "code": code}
        else:
            selected[object_id] = {
                "bay_id": chosen[0],
                "orientation": chosen[1],
                "code": "P_OK",
            }

    # -- stage 6 conservation adjustment ------------------------------------
    def excluded_by_exception(state, bay):
        return state["conservation_class"] in blocked_classes.get(bay["bay_id"], ())

    def conservation_ok(state, bay):
        if bay["lux_level"] > state["max_lux"]:
            return False
        if excluded_by_exception(state, bay):
            return False
        rule = conservation.get(state["conservation_class"])
        if rule is not None and to_bool(rule["needs_dark"]) and bay["lux_level"] > 0:
            return False
        return True

    adjusted = {}
    for object_id in order:
        state = objects[object_id]
        geometric_bays = [
            bay
            for bay in bay_scan_order
            if bay_candidates[object_id][bay["bay_id"]] >= 0
        ]
        chosen = None
        for bay in geometric_bays:
            if conservation_ok(state, bay):
                chosen = (bay["bay_id"], bay_candidates[object_id][bay["bay_id"]])
                break
        if chosen is not None:
            adjusted[object_id] = {
                "bay_id": chosen[0],
                "orientation": chosen[1],
                "code": "P_OK",
            }
        elif not geometric_bays:
            adjusted[object_id] = {
                "bay_id": "",
                "orientation": -1,
                "code": selected[object_id]["code"],
            }
        else:
            code = "P_CONSERVATION_BLOCK"
            if len(geometric_bays) == 1 and excluded_by_exception(
                state, geometric_bays[0]
            ):
                code = "P_BAY_EXCEPTION"
            adjusted[object_id] = {"bay_id": "", "orientation": -1, "code": code}

    # -- stage 7 crate candidates -------------------------------------------
    crate_candidates = {}
    primary_crate = {}
    for object_id in order:
        state = objects[object_id]
        sorted_handling = sorted(state["handling_dims"])
        fitting = []
        for crate_class in sorted(crates):
            crate = crates[crate_class]
            if all(sorted_handling[axis] <= crate["dims"][axis] for axis in range(3)):
                fitting.append(
                    {
                        "crate_class": crate_class,
                        "residual": crate["volume"] - state["handling_volume"],
                    }
                )
        fitting.sort(key=lambda item: (item["residual"], item["crate_class"]))
        crate_candidates[object_id] = fitting
        primary_crate[object_id] = fitting[0]["crate_class"] if fitting else ""

    # -- stage 13 setup seconds (consumed by stage 12) ----------------------
    setup_seconds = {
        object_id: round_half_up(
            objects[object_id]["setup_base"] * objects[object_id]["mass_g"], setup_div
        )
        for object_id in order
    }

    # -- stage 10 dependency edges ------------------------------------------
    edges = {}

    def add_edge(from_id, to_id, kind, weight):
        key = (from_id, to_id)
        existing = edges.get(key)
        if existing is None or KIND_RANK[kind] > KIND_RANK[existing["kind"]]:
            edges[key] = {"kind": kind, "weight": weight}

    bay_members = {}
    for object_id in order:
        placement = adjusted[object_id]
        if placement["code"] == "P_OK":
            bay_members.setdefault(placement["bay_id"], []).append(object_id)
    for bay_id in bay_members:
        bay_members[bay_id].sort()

    for bay_id in sorted(bay_members):
        members = bay_members[bay_id]
        for left_id in members:
            for right_id in members:
                if left_id == right_id:
                    continue
                left = objects[left_id]
                right = objects[right_id]
                if (-left["handling_volume"], left_id) < (
                    -right["handling_volume"],
                    right_id,
                ):
                    add_edge(left_id, right_id, KIND_BAYSIZE, left["handling_volume"])
        for left_id in members:
            for right_id in members:
                if left_id == right_id:
                    continue
                left = objects[left_id]
                right = objects[right_id]
                if sensitivity_rank.get(
                    left["conservation_class"], -1
                ) > sensitivity_rank.get(right["conservation_class"], -1):
                    add_edge(
                        left_id, right_id, KIND_CONSERVATION, left["handling_volume"]
                    )

    for row in dependency_rows:
        from_id = row["from_object"]
        to_id = row["to_object"]
        if from_id not in objects or to_id not in objects:
            continue
        add_edge(from_id, to_id, KIND_RULE, to_int(row["edge_priority"]))

    dependency_edge_rows = [
        {"from": key[0], "to": key[1], "kind": value["kind"], "weight": value["weight"]}
        for key, value in sorted(edges.items())
    ]

    # -- stage 11 cycle resolution ------------------------------------------
    live_edges = dict(edges)
    removed_edges = []
    while True:
        adjacency = {}
        for (from_id, to_id) in live_edges:
            adjacency.setdefault(from_id, []).append(to_id)
        for key in adjacency:
            adjacency[key].sort()
        cycle = find_cycle(order, adjacency)
        if cycle is None:
            break
        victim = min(
            cycle,
            key=lambda pair: (
                KIND_RANK[live_edges[pair]["kind"]],
                live_edges[pair]["weight"],
                pair[0],
                pair[1],
            ),
        )
        data = live_edges.pop(victim)
        removed_edges.append(
            {
                "from": victim[0],
                "to": victim[1],
                "kind": data["kind"],
                "weight": data["weight"],
            }
        )

    # -- stage 12 install order ---------------------------------------------
    successors = {object_id: [] for object_id in order}
    indegree = {object_id: 0 for object_id in order}
    for (from_id, to_id) in live_edges:
        successors[from_id].append(to_id)
        indegree[to_id] += 1

    def ready_key(object_id):
        return (setup_seconds[object_id], object_id)

    install_sequence = []
    ready = sorted(
        (object_id for object_id in order if indegree[object_id] == 0), key=ready_key
    )
    while ready:
        chosen = ready.pop(0)
        install_sequence.append(chosen)
        freed = []
        for successor in sorted(successors[chosen]):
            indegree[successor] -= 1
            if indegree[successor] == 0:
                freed.append(successor)
        if freed:
            ready.extend(freed)
            ready.sort(key=ready_key)
    installed = set(install_sequence)
    leftover = [oid for oid in order if oid not in installed]
    install_sequence.extend(sorted(leftover, key=ready_key))
    install_position = {
        object_id: index + 1 for index, object_id in enumerate(install_sequence)
    }

    # -- stage 8 shared crate capacity --------------------------------------
    remaining_crates = {
        crate_class: crates[crate_class]["count"] - prior_consumed.get(crate_class, 0)
        for crate_class in crates
    }
    remaining_reservations = dict(reservations)
    assignment = {}
    for object_id in install_sequence:
        state = objects[object_id]
        primary = primary_crate[object_id]
        options = [
            candidate
            for candidate in crate_candidates[object_id]
            if remaining_crates.get(candidate["crate_class"], 0) > 0
        ]
        if not options:
            assignment[object_id] = {
                "crate_class": "",
                "code": "C_NO_CRATE",
                "primary_class": primary,
            }
            continue
        best = min(
            options,
            key=lambda candidate: (
                0
                if remaining_reservations.get(
                    (candidate["crate_class"], state["lender_id"]), 0
                )
                > 0
                else 1,
                candidate["residual"],
                candidate["crate_class"],
            ),
        )
        crate_class = best["crate_class"]
        remaining_crates[crate_class] -= 1
        reservation_key = (crate_class, state["lender_id"])
        if remaining_reservations.get(reservation_key, 0) > 0:
            remaining_reservations[reservation_key] -= 1
        assignment[object_id] = {
            "crate_class": crate_class,
            "code": "C_PRIMARY" if crate_class == primary else "C_DISPLACED",
            "primary_class": primary,
            "residual": best["residual"],
        }

    # -- stage 9 transport legs ---------------------------------------------
    transport_manifest = []
    for object_id in order:
        entry = assignment[object_id]
        if entry["code"] == "C_NO_CRATE":
            continue
        mode = crates[entry["crate_class"]]["transport_mode"]
        exception = False
        for forbidden, fallback in forbidden_modes.get(
            objects[object_id]["lender_id"], []
        ):
            if forbidden == mode:
                mode = fallback
                exception = True
                break
        transport_manifest.append(
            {
                "object_id": object_id,
                "crate_class": entry["crate_class"],
                "mode": mode,
                "exception": exception,
            }
        )

    # -- stage 13 crew and windows ------------------------------------------
    window_rows = []
    unschedulable_rows = []
    scheduled = {}
    for object_id in order:
        state = objects[object_id]
        placement = adjusted[object_id]
        required_days = ceil_div(setup_seconds[object_id], SECONDS_PER_DAY)
        bay_id = placement["bay_id"]
        placed = placement["code"] == "P_OK"
        if not placed:
            # Without a bay there is no venue access leg, so no window exists.
            low, high = 0, -1
        else:
            starts = []
            ends = []
            conservation_rule = conservation.get(state["conservation_class"])
            if conservation_rule is not None:
                starts.append(to_int(conservation_rule["setup_start"]))
                end = to_int(conservation_rule["setup_end"])
                ends.append(end - 1 if conservation_exclusive else end)
            venue_rule = venue_windows.get(bay_id)
            if venue_rule is not None:
                starts.append(to_int(venue_rule["access_start"]))
                end = to_int(venue_rule["access_end"])
                ends.append(end - 1 if venue_exclusive else end)
            crew_rule = crew_windows.get(state["crew_class"])
            if crew_rule is not None:
                starts.append(to_int(crew_rule["avail_start"]))
                ends.append(to_int(crew_rule["avail_end"]))
            low = max(starts) if starts else 0
            high = min(ends) if ends else -1
        if not placed:
            code = "S_NO_PLACEMENT"
        elif assignment[object_id]["code"] == "C_NO_CRATE":
            code = "S_NO_CRATE"
        elif high < low:
            code = "S_NO_WINDOW"
        elif required_days > high - low + 1:
            code = "S_SHORT_WINDOW"
        else:
            code = "S_OK"
        scheduled[object_id] = code == "S_OK"
        window_rows.append(
            {
                "object_id": object_id,
                "bay_id": bay_id,
                "window_start": low,
                "window_end": high,
                "required_days": required_days,
                "code": code,
            }
        )
        if code != "S_OK":
            unschedulable_rows.append({"object_id": object_id, "code": code})

    # -- stage 14 dossier ----------------------------------------------------
    def value_class_of(object_id):
        declared = objects[object_id]["declared_value"]
        for entry in value_classes:
            if declared >= entry["min_value"]:
                return entry
        return None

    unschedulable_penalty = 0
    for row in unschedulable_rows:
        entry = value_class_of(row["object_id"])
        if entry is not None:
            unschedulable_penalty += entry["unschedulable_penalty"]

    contention_penalty = 0
    contention_report = []
    for object_id in sorted(order):
        entry = assignment[object_id]
        if entry["code"] != "C_DISPLACED":
            continue
        contention_report.append(
            {
                "object_id": object_id,
                "wanted_class": entry["primary_class"],
                "assigned_class": entry["crate_class"],
            }
        )
        value_entry = value_class_of(object_id)
        if value_entry is not None:
            contention_penalty += value_entry["contention_penalty"]
    displacement_count = len(contention_report)

    insurance = {}
    for object_id in order:
        lender = lenders.get(objects[object_id]["lender_id"])
        denominator = to_int(lender["insurance_rate_den"]) if lender else 0
        if lender is None or denominator == 0:
            insurance[object_id] = 0
        else:
            insurance[object_id] = round_half_up(
                objects[object_id]["declared_value_cents"]
                * to_int(lender["insurance_rate_num"]),
                denominator,
            )

    total_residual = sum(assignment[oid].get("residual", 0) for oid in order)
    total_setup_seconds = sum(setup_seconds[oid] for oid in order)
    total_insurance_cents = sum(insurance[oid] for oid in order)
    placed_objects = sum(1 for oid in order if scheduled[oid])
    cycles_broken = len(removed_edges)
    objective = [
        placed_objects,
        unschedulable_penalty,
        contention_penalty,
        cycles_broken,
        total_residual,
    ]

    # -- emit ----------------------------------------------------------------
    os.makedirs(out_dir, exist_ok=True)

    write_jsonl(
        os.path.join(out_dir, "canonical_objects.jsonl"),
        [
            {
                "object_id": oid,
                "lender_id": objects[oid]["lender_id"],
                "material_code": objects[oid]["material_code"],
                "version": objects[oid]["version"],
                "declared_value_cents": objects[oid]["declared_value_cents"],
                "mass_g": objects[oid]["mass_g"],
            }
            for oid in order
        ],
    )

    write_jsonl(
        os.path.join(out_dir, "normalized_geometry.jsonl"),
        [
            {
                "object_id": oid,
                "clearance_w": objects[oid]["clearance_w"],
                "clearance_h": objects[oid]["clearance_h"],
                "clearance_d": objects[oid]["clearance_d"],
                "aspect_num": objects[oid]["aspect_num"],
                "aspect_den": objects[oid]["aspect_den"],
            }
            for oid in order
        ],
    )

    write_jsonl(
        os.path.join(out_dir, "admissibility_matrix.jsonl"),
        [
            {
                "object_id": oid,
                "orientations": objects[oid]["orientations"],
                "tip_allowed": objects[oid]["tip_allowed"],
                "rotate_allowed": objects[oid]["rotate_allowed"],
            }
            for oid in order
        ],
    )

    write_jsonl(
        os.path.join(out_dir, "bay_candidate_matrix.jsonl"),
        [
            {
                "object_id": oid,
                "bay_id": bay_id,
                "fits": bay_candidates[oid][bay_id] >= 0,
                "orientation": bay_candidates[oid][bay_id],
            }
            for oid in order
            for bay_id in bay_ids_sorted
        ],
    )

    write_jsonl(
        os.path.join(out_dir, "selected_placement.jsonl"),
        [
            {
                "object_id": oid,
                "bay_id": selected[oid]["bay_id"],
                "orientation": selected[oid]["orientation"],
                "code": selected[oid]["code"],
            }
            for oid in order
        ],
    )

    write_jsonl(
        os.path.join(out_dir, "rejected_placement.jsonl"),
        sorted(
            (
                {"object_id": oid, "code": adjusted[oid]["code"]}
                for oid in order
                if adjusted[oid]["code"] != "P_OK"
            ),
            key=lambda row: (row["object_id"], row["code"]),
        ),
    )

    write_jsonl(
        os.path.join(out_dir, "conservation_adjusted.jsonl"),
        [
            {
                "object_id": oid,
                "bay_id": adjusted[oid]["bay_id"],
                "orientation": adjusted[oid]["orientation"],
                "code": adjusted[oid]["code"],
            }
            for oid in order
        ],
    )

    write_jsonl(
        os.path.join(out_dir, "crate_candidate_matrix.jsonl"),
        [
            {
                "object_id": oid,
                "crate_class": candidate["crate_class"],
                "residual": candidate["residual"],
            }
            for oid in sorted(order)
            for candidate in crate_candidates[oid]
        ],
    )

    write_jsonl(
        os.path.join(out_dir, "crate_assignment.jsonl"),
        [
            {
                "object_id": oid,
                "crate_class": assignment[oid]["crate_class"],
                "code": assignment[oid]["code"],
                "primary_class": assignment[oid]["primary_class"],
            }
            for oid in sorted(order)
        ],
    )

    write_jsonl(
        os.path.join(out_dir, "crate_capacity_ledger.jsonl"),
        [
            {
                "crate_class": crate_class,
                "catalog_count": crates[crate_class]["count"],
                "prior_consumed": prior_consumed.get(crate_class, 0),
                "remaining": remaining_crates[crate_class],
            }
            for crate_class in sorted(crates)
        ],
    )

    write_jsonl(
        os.path.join(out_dir, "crate_contention_report.jsonl"), contention_report
    )
    write_jsonl(os.path.join(out_dir, "transport_leg_manifest.jsonl"), transport_manifest)
    write_jsonl(os.path.join(out_dir, "dependency_edges.jsonl"), dependency_edge_rows)
    write_jsonl(os.path.join(out_dir, "cycle_resolution_audit.jsonl"), removed_edges)

    write_jsonl(
        os.path.join(out_dir, "install_order.jsonl"),
        [
            {"position": index + 1, "object_id": oid}
            for index, oid in enumerate(install_sequence)
        ],
    )

    write_jsonl(
        os.path.join(out_dir, "crew_requirement_ledger.jsonl"),
        [
            {
                "object_id": oid,
                "crew_class": objects[oid]["crew_class"],
                "setup_seconds": setup_seconds[oid],
            }
            for oid in order
        ],
    )

    write_jsonl(os.path.join(out_dir, "install_window_table.jsonl"), window_rows)

    write_jsonl(
        os.path.join(out_dir, "unschedulable_report.jsonl"),
        sorted(unschedulable_rows, key=lambda row: (row["object_id"], row["code"])),
    )

    write_jsonl(
        os.path.join(out_dir, "gallery_occupancy_map.jsonl"),
        [
            {
                "bay_id": bay_id,
                "object_count": len(bay_members.get(bay_id, [])),
                "objects": sorted(bay_members.get(bay_id, [])),
            }
            for bay_id in bay_ids_sorted
        ],
    )

    write_jsonl(
        os.path.join(out_dir, "deterministic_audit.jsonl"),
        [
            {
                "object_id": oid,
                "placed_bay": adjusted[oid]["bay_id"],
                "crate_class": assignment[oid]["crate_class"],
                "displaced": assignment[oid]["code"] == "C_DISPLACED",
                "scheduled": scheduled[oid],
                "install_position": install_position[oid],
                "setup_seconds": setup_seconds[oid],
                "insurance_cents": insurance[oid],
            }
            for oid in order
        ],
    )

    write_json(
        os.path.join(out_dir, "penalty_summary.json"),
        {
            "unschedulable_penalty": unschedulable_penalty,
            "contention_penalty": contention_penalty,
            "cycles_broken": cycles_broken,
            "total_insurance_cents": total_insurance_cents,
        },
    )

    write_json(
        os.path.join(out_dir, "objective_tuple.json"),
        {"objective": objective, "order": objective_order.get("order", [])},
    )

    program_path = os.path.realpath(__file__)
    manifest = {}
    for name in JSONL_ARTIFACTS + ["penalty_summary.json", "objective_tuple.json"]:
        manifest[name] = sha256_file(os.path.join(out_dir, name))
    manifest[os.path.basename(program_path)] = sha256_file(program_path)
    write_json(os.path.join(out_dir, "integrity_manifest.json"), manifest)

    chain = chain_seed
    for name in sorted(manifest):
        chain = hashlib.sha256((chain + manifest[name]).encode("utf-8")).hexdigest()

    write_json(
        os.path.join(out_dir, "final_state.json"),
        {
            "chain": chain,
            "repair_converged": True,
            "cycle_edges_removed": cycles_broken,
            "final_cycle_count": 0,
            "displacement_count": displacement_count,
            "unschedulable_count": len(unschedulable_rows),
            "placed_objects": placed_objects,
            "total_setup_seconds": total_setup_seconds,
            "total_insurance_cents": total_insurance_cents,
            "objective": objective,
        },
    )


def main(argv):
    if len(argv) != 3:
        sys.stderr.write("usage: plan.py <DATA_DIR> <OUT_DIR>\n")
        return 2
    derive(argv[1], argv[2])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
