#!/usr/bin/env python3
"""Derive the installation dossier from a snapshot directory.

Usage: python3 plan.py <DATA_DIR> <OUT_DIR>

All arithmetic is integer or exact rational; no float is ever produced.
"""

import csv
import hashlib
import json
import os
import sys
from math import gcd

LENGTH_FACTORS = {"MM": 1000, "CM": 10000, "IN": 25400}
MASS_FACTORS = {"G": 1, "KG": 1000}
CENTS_PER_UNIT = 100
SECONDS_PER_DAY = 3600
KIND_RANK = {"D_BAYSIZE": 0, "D_CONSERVATION": 1, "D_RULE": 2}

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
# exact arithmetic helpers
# --------------------------------------------------------------------------

def round_half_up(num, den):
    """num/den rounded half up; a zero denominator yields the sentinel zero."""
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
    return -((-num) // den)


def reduce_ratio(num, den):
    if den == 0:
        return 0, 1
    common = gcd(abs(num), abs(den))
    if common == 0:
        return 0, 1
    return num // common, den // common


# --------------------------------------------------------------------------
# input
# --------------------------------------------------------------------------

def read_csv(data_dir, name):
    with open(os.path.join(data_dir, name), newline="", encoding="utf-8") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def read_json(data_dir, name):
    with open(os.path.join(data_dir, name), encoding="utf-8") as handle:
        return json.load(handle)


def load_snapshot(data_dir):
    return {
        "config": read_json(data_dir, "horizon_config.json"),
        "objective_order": read_json(data_dir, "objective_order.json"),
        "master": read_csv(data_dir, "object_master.csv"),
        "dimensions": read_csv(data_dir, "object_dimensions.csv"),
        "mass": read_csv(data_dir, "object_mass.csv"),
        "value": read_csv(data_dir, "object_value.csv"),
        "orientation": read_csv(data_dir, "object_orientation.csv"),
        "versions": read_csv(data_dir, "object_version_map.csv"),
        "materials": read_csv(data_dir, "material_rules.csv"),
        "handling": read_csv(data_dir, "handling_class_rules.csv"),
        "bays": read_csv(data_dir, "gallery_bays.csv"),
        "bay_exceptions": read_csv(data_dir, "bay_exceptions.csv"),
        "crates": read_csv(data_dir, "crate_catalog.csv"),
        "reservations": read_csv(data_dir, "crate_reservations.csv"),
        "prior_state": read_csv(data_dir, "prior_state.csv"),
        "conservation": read_csv(data_dir, "conservation_rules.csv"),
        "venue": read_csv(data_dir, "venue_windows.csv"),
        "dependencies": read_csv(data_dir, "dependency_rules.csv"),
        "crew": read_csv(data_dir, "crew_roster.csv"),
        "transport": read_csv(data_dir, "transport_rules.csv"),
        "lenders": read_csv(data_dir, "lender_master.csv"),
        "value_classes": read_csv(data_dir, "value_class_rules.csv"),
    }


# --------------------------------------------------------------------------
# stage 1 canonical identity and units
# --------------------------------------------------------------------------

def build_objects(snap):
    """First master row per object id wins; later stale rows are discarded."""
    objects = []
    seen = set()
    for row in snap["master"]:
        oid = row["object_id"]
        if oid in seen:
            continue
        seen.add(oid)
        objects.append({
            "object_id": oid,
            "lender_id": row["lender_id"],
            "material_code": row["material_code"],
        })

    versions = {}
    for row in snap["versions"]:
        oid = row["object_id"]
        version = int(row["version"])
        if oid not in versions or version > versions[oid]:
            versions[oid] = version

    dims = {r["object_id"]: r for r in snap["dimensions"]}
    mass = {r["object_id"]: r for r in snap["mass"]}
    value = {r["object_id"]: r for r in snap["value"]}
    orient = {r["object_id"]: r for r in snap["orientation"]}
    materials = {r["material_code"]: r for r in snap["materials"]}
    handling = {r["handling_class"]: r for r in snap["handling"]}

    for obj in objects:
        oid = obj["object_id"]
        obj["version"] = versions[oid]
        obj["declared_value"] = int(value[oid]["declared_value"])
        obj["declared_value_cents"] = obj["declared_value"] * CENTS_PER_UNIT
        obj["mass_g"] = int(mass[oid]["mass"]) * MASS_FACTORS[mass[oid]["mass_unit"]]

        dim = dims[oid]
        factor = LENGTH_FACTORS[dim["dim_unit"]]
        obj["width_um"] = int(dim["width"]) * factor
        obj["height_um"] = int(dim["height"]) * factor
        obj["depth_um"] = int(dim["depth"]) * factor

        obj["tip_allowed"] = int(orient[oid]["tip_allowed"]) == 1
        obj["rotate_allowed"] = int(orient[oid]["rotate_allowed"]) == 1

        material = materials[obj["material_code"]]
        obj["conservation_class"] = material["conservation_class"]
        obj["max_lux"] = int(material["max_lux"])
        obj["handling_class"] = material["handling_class"]
        obj["handling_rule"] = handling[material["handling_class"]]
    return objects


# --------------------------------------------------------------------------
# stage 2 normalized geometry
# --------------------------------------------------------------------------

def normalize_geometry(objects, unit_factor):
    for obj in objects:
        rule = obj["handling_rule"]
        obj["clearance_w"] = obj["width_um"] + 2 * int(rule["clearance_w"]) * unit_factor
        obj["clearance_d"] = obj["depth_um"] + 2 * int(rule["clearance_d"]) * unit_factor
        obj["clearance_h"] = obj["height_um"] + int(rule["clearance_h"]) * unit_factor
        obj["aspect_num"], obj["aspect_den"] = reduce_ratio(obj["height_um"], obj["width_um"])


# --------------------------------------------------------------------------
# stage 3 orientation admissibility
# --------------------------------------------------------------------------

def orientation_extent(obj, index):
    """(footprint width, footprint depth, vertical extent) of an orientation."""
    cw, ch, cd = obj["clearance_w"], obj["clearance_h"], obj["clearance_d"]
    if index == 0:
        return cw, cd, ch
    if index == 1:
        return cd, cw, ch
    if index == 2:
        return cw, ch, cd
    return ch, cw, cd


def admissible_orientations(objects):
    for obj in objects:
        orientations = [0]
        if obj["rotate_allowed"]:
            orientations.append(1)
        if obj["tip_allowed"]:
            orientations.extend([2, 3])
        obj["orientations"] = orientations


# --------------------------------------------------------------------------
# stage 4 bay candidates
# --------------------------------------------------------------------------

def build_bays(snap, unit_factor):
    bays = []
    for row in snap["bays"]:
        bays.append({
            "bay_id": row["bay_id"],
            "interior_w": int(row["interior_w"]) * unit_factor,
            "interior_h": int(row["interior_h"]) * unit_factor,
            "interior_d": int(row["interior_d"]) * unit_factor,
            "lux_level": int(row["lux_level"]),
            "bay_priority": int(row["bay_priority"]),
        })
    return bays


def fitting_orientation(obj, bay):
    """Smallest admissible orientation index that fits, else -1.

    Width inclusive, depth strictly exclusive, vertical inclusive.
    """
    for index in obj["orientations"]:
        fw, fd, fv = orientation_extent(obj, index)
        if fw <= bay["interior_w"] and fd < bay["interior_d"] and fv <= bay["interior_h"]:
            return index
    return -1


def bay_candidates(objects, bays):
    return {
        obj["object_id"]: {bay["bay_id"]: fitting_orientation(obj, bay) for bay in bays}
        for obj in objects
    }


# --------------------------------------------------------------------------
# stages 5 and 6 placement
# --------------------------------------------------------------------------

def scan_order(bays):
    return sorted(bays, key=lambda b: (b["bay_priority"], b["bay_id"]))


def reject_code(obj):
    return "P_NO_ORIENTATION" if obj["orientations"] == [0] else "P_NO_FIT"


def select_placement(objects, bays, matrix):
    ordered = scan_order(bays)
    for obj in objects:
        obj["sel_bay"], obj["sel_orientation"], obj["sel_code"] = "", -1, reject_code(obj)
        for bay in ordered:
            index = matrix[obj["object_id"]][bay["bay_id"]]
            if index >= 0:
                obj["sel_bay"] = bay["bay_id"]
                obj["sel_orientation"] = index
                obj["sel_code"] = "P_OK"
                break


def conservation_exclusion(obj, bay, blocked, needs_dark):
    """Why conservation removes this bay for this object, or None if it passes."""
    if obj["conservation_class"] in blocked.get(bay["bay_id"], ()):
        return "exception"
    if bay["lux_level"] > obj["max_lux"]:
        return "lux"
    if needs_dark.get(obj["conservation_class"], False) and bay["lux_level"] > 0:
        return "dark"
    return None


def conservation_adjust(objects, bays, matrix, snap):
    ordered = scan_order(bays)
    blocked = {}
    for row in snap["bay_exceptions"]:
        blocked.setdefault(row["bay_id"], set()).add(row["blocked_class"])
    needs_dark = {
        row["conservation_class"]: int(row["needs_dark"]) == 1
        for row in snap["conservation"]
    }

    for obj in objects:
        geometric = [b for b in ordered if matrix[obj["object_id"]][b["bay_id"]] >= 0]
        obj["cons_bay"], obj["cons_orientation"] = "", -1
        reasons = []
        for bay in geometric:
            reason = conservation_exclusion(obj, bay, blocked, needs_dark)
            if reason is None:
                obj["cons_bay"] = bay["bay_id"]
                obj["cons_orientation"] = matrix[obj["object_id"]][bay["bay_id"]]
                obj["cons_code"] = "P_OK"
                break
            reasons.append(reason)
        else:
            if not geometric:
                obj["cons_code"] = reject_code(obj)
            elif len(geometric) == 1 and reasons == ["exception"]:
                obj["cons_code"] = "P_BAY_EXCEPTION"
            else:
                obj["cons_code"] = "P_CONSERVATION_BLOCK"


# --------------------------------------------------------------------------
# stage 7 crate candidates
# --------------------------------------------------------------------------

def build_crates(snap, unit_factor):
    crates = []
    for row in snap["crates"]:
        inner = (
            int(row["inner_w"]) * unit_factor,
            int(row["inner_h"]) * unit_factor,
            int(row["inner_d"]) * unit_factor,
        )
        crates.append({
            "crate_class": row["crate_class"],
            "sorted_inner": sorted(inner),
            "volume": inner[0] * inner[1] * inner[2],
            "count": int(row["count"]),
            "transport_mode": row["transport_mode"],
        })
    return crates


def crate_candidates(objects, crates, unit_factor):
    candidates = {}
    for obj in objects:
        margin = int(obj["handling_rule"]["handle_margin"]) * unit_factor
        dims = (
            obj["width_um"] + margin,
            obj["height_um"] + margin,
            obj["depth_um"] + margin,
        )
        obj["handling_volume"] = dims[0] * dims[1] * dims[2]
        sorted_dims = sorted(dims)
        fitting = [
            {"crate_class": c["crate_class"], "residual": c["volume"] - obj["handling_volume"]}
            for c in crates
            if all(a <= b for a, b in zip(sorted_dims, c["sorted_inner"]))
        ]
        fitting.sort(key=lambda c: (c["residual"], c["crate_class"]))
        candidates[obj["object_id"]] = fitting
        obj["primary_class"] = fitting[0]["crate_class"] if fitting else ""
    return candidates


# --------------------------------------------------------------------------
# stage 10 dependency edges
# --------------------------------------------------------------------------

def sensitivity_rank(snap):
    """Higher conservation class id ranks as more sensitive."""
    classes = sorted({row["conservation_class"] for row in snap["conservation"]})
    return {name: index for index, name in enumerate(classes)}


def build_dependency_edges(objects, snap):
    by_id = {obj["object_id"]: obj for obj in objects}
    ranks = sensitivity_rank(snap)
    edges = {}

    def offer(src, dst, kind, weight):
        current = edges.get((src, dst))
        if current is None or KIND_RANK[kind] > KIND_RANK[current["kind"]]:
            edges[(src, dst)] = {"from": src, "to": dst, "kind": kind, "weight": weight}

    by_bay = {}
    for obj in objects:
        if obj["cons_code"] == "P_OK":
            by_bay.setdefault(obj["cons_bay"], []).append(obj)

    for bay_objects in by_bay.values():
        ordered = sorted(bay_objects, key=lambda o: o["object_id"])
        for i, first in enumerate(ordered):
            for second in ordered[i + 1:]:
                if first["handling_volume"] >= second["handling_volume"]:
                    earlier, later = first, second
                else:
                    earlier, later = second, first
                offer(earlier["object_id"], later["object_id"],
                      "D_BAYSIZE", earlier["handling_volume"])
        for first in ordered:
            for second in ordered:
                if first is second:
                    continue
                if ranks[first["conservation_class"]] > ranks[second["conservation_class"]]:
                    offer(first["object_id"], second["object_id"],
                          "D_CONSERVATION", first["handling_volume"])

    for row in snap["dependencies"]:
        src, dst = row["from_object"], row["to_object"]
        if src in by_id and dst in by_id:
            offer(src, dst, "D_RULE", int(row["edge_priority"]))

    return [edges[key] for key in sorted(edges)]


# --------------------------------------------------------------------------
# stage 11 cycle resolution
# --------------------------------------------------------------------------

def find_cycle(nodes, edges):
    """First cycle under a DFS with nodes and adjacency in ascending order."""
    adjacency = {node: [] for node in nodes}
    for edge in edges:
        adjacency[edge["from"]].append(edge)
    for node in adjacency:
        adjacency[node].sort(key=lambda e: (e["to"], KIND_RANK[e["kind"]], e["weight"]))

    white, grey, black = 0, 1, 2
    color = {node: white for node in nodes}
    for start in sorted(nodes):
        if color[start] != white:
            continue
        color[start] = grey
        stack = [[start, 0]]
        path = []
        while stack:
            node, index = stack[-1]
            if index < len(adjacency[node]):
                stack[-1][1] += 1
                edge = adjacency[node][index]
                nxt = edge["to"]
                if color[nxt] == grey:
                    for i, walked in enumerate(path):
                        if walked["from"] == nxt:
                            return path[i:] + [edge]
                    return [edge]
                if color[nxt] == white:
                    color[nxt] = grey
                    path.append(edge)
                    stack.append([nxt, 0])
            else:
                stack.pop()
                color[node] = black
                if stack:
                    path.pop()
    return None


def edge_priority_key(edge):
    return (KIND_RANK[edge["kind"]], edge["weight"], edge["from"], edge["to"])


def resolve_cycles(nodes, edges):
    remaining = list(edges)
    removed = []
    while True:
        cycle = find_cycle(nodes, remaining)
        if cycle is None:
            break
        victim = min(cycle, key=edge_priority_key)
        remaining = [e for e in remaining
                     if not (e["from"] == victim["from"] and e["to"] == victim["to"])]
        removed.append(victim)
    return remaining, removed, find_cycle(nodes, remaining) is None


# --------------------------------------------------------------------------
# stage 13 crew and setup, consumed by stage 12
# --------------------------------------------------------------------------

def compute_setup(objects, setup_div):
    for obj in objects:
        obj["crew_class"] = obj["handling_rule"]["crew_class"]
        obj["setup_seconds"] = round_half_up(
            int(obj["handling_rule"]["setup_base"]) * obj["mass_g"], setup_div)


# --------------------------------------------------------------------------
# stage 12 install order
# --------------------------------------------------------------------------

def install_order(objects, edges):
    by_id = {obj["object_id"]: obj for obj in objects}
    predecessors = {oid: set() for oid in by_id}
    for edge in edges:
        predecessors[edge["to"]].add(edge["from"])

    order = []
    done = set()
    while len(order) < len(by_id):
        ready = [oid for oid in by_id
                 if oid not in done and not (predecessors[oid] - done)]
        if not ready:
            break
        chosen = min(ready, key=lambda oid: (by_id[oid]["setup_seconds"], oid))
        order.append(chosen)
        done.add(chosen)
    return order


# --------------------------------------------------------------------------
# stage 8 shared crate capacity
# --------------------------------------------------------------------------

def assign_crates(objects, snap, candidates, order):
    by_id = {obj["object_id"]: obj for obj in objects}
    catalog = {row["crate_class"]: row for row in snap["crates"]}
    prior = {row["crate_class"]: int(row["consumed_count"]) for row in snap["prior_state"]}
    remaining = {name: int(row["count"]) - prior.get(name, 0)
                 for name, row in catalog.items()}
    reservations = {
        (row["crate_class"], row["lender_id"]): int(row["reserved_count"])
        for row in snap["reservations"]
    }

    displaced = []
    for oid in order:
        obj = by_id[oid]
        options = [c for c in candidates[oid] if remaining.get(c["crate_class"], 0) > 0]
        if not options:
            obj["crate_class"] = ""
            obj["crate_code"] = "C_NO_CRATE"
            obj["residual"] = 0
            continue

        def rank(candidate, lender=obj["lender_id"]):
            reserved = reservations.get((candidate["crate_class"], lender), 0) > 0
            return (0 if reserved else 1, candidate["residual"], candidate["crate_class"])

        chosen = min(options, key=rank)
        name = chosen["crate_class"]
        remaining[name] -= 1
        if reservations.get((name, obj["lender_id"]), 0) > 0:
            reservations[(name, obj["lender_id"])] -= 1
        obj["crate_class"] = name
        obj["residual"] = chosen["residual"]
        if name == obj["primary_class"]:
            obj["crate_code"] = "C_PRIMARY"
        else:
            obj["crate_code"] = "C_DISPLACED"
            displaced.append(oid)

    ledger = [
        {
            "crate_class": name,
            "catalog_count": int(catalog[name]["count"]),
            "prior_consumed": prior.get(name, 0),
            "remaining": remaining[name],
        }
        for name in sorted(catalog)
    ]
    return ledger, displaced


# --------------------------------------------------------------------------
# stage 9 transport legs
# --------------------------------------------------------------------------

def transport_legs(objects, snap):
    catalog = {row["crate_class"]: row for row in snap["crates"]}
    rules = {row["lender_id"]: row for row in snap["transport"]}
    legs = []
    for obj in objects:
        if not obj["crate_class"]:
            continue
        mode = catalog[obj["crate_class"]]["transport_mode"]
        exception = False
        rule = rules.get(obj["lender_id"])
        if rule is not None and rule["forbidden_mode"] == mode:
            mode = rule["fallback_mode"]
            exception = True
        legs.append({
            "object_id": obj["object_id"],
            "crate_class": obj["crate_class"],
            "mode": mode,
            "exception": exception,
        })
    return legs


# --------------------------------------------------------------------------
# stage 13 windows
# --------------------------------------------------------------------------

def compute_windows(objects, snap, config):
    conservation = {row["conservation_class"]: row for row in snap["conservation"]}
    venue = {row["bay_id"]: row for row in snap["venue"]}
    crew = {row["crew_class"]: row for row in snap["crew"]}
    venue_drop = 1 if config["venue_end_convention"] == "exclusive" else 0
    conservation_drop = 1 if config["conservation_end_convention"] == "exclusive" else 0

    for obj in objects:
        obj["required_days"] = ceil_div(obj["setup_seconds"], SECONDS_PER_DAY)
        if obj["cons_code"] != "P_OK":
            obj["window_start"] = -1
            obj["window_end"] = -1
            obj["schedule_code"] = "S_NO_PLACEMENT"
            continue

        cons = conservation[obj["conservation_class"]]
        access = venue[obj["cons_bay"]]
        roster = crew[obj["crew_class"]]
        low = max(int(cons["setup_start"]), int(access["access_start"]),
                  int(roster["avail_start"]))
        high = min(int(cons["setup_end"]) - conservation_drop,
                   int(access["access_end"]) - venue_drop,
                   int(roster["avail_end"]))
        obj["window_start"] = low
        obj["window_end"] = high

        if not obj["crate_class"]:
            obj["schedule_code"] = "S_NO_CRATE"
        elif high < low:
            obj["schedule_code"] = "S_NO_WINDOW"
        elif obj["required_days"] > high - low + 1:
            obj["schedule_code"] = "S_SHORT_WINDOW"
        else:
            obj["schedule_code"] = "S_OK"


# --------------------------------------------------------------------------
# stage 14 dossier figures
# --------------------------------------------------------------------------

def value_class_table(snap):
    table = [
        {
            "value_class": row["value_class"],
            "min_value": int(row["min_value"]),
            "unschedulable_penalty": int(row["unschedulable_penalty"]),
            "contention_penalty": int(row["contention_penalty"]),
        }
        for row in snap["value_classes"]
    ]
    table.sort(key=lambda r: (-r["min_value"], r["value_class"]))
    return table


def value_class_of(obj, table):
    for row in table:
        if obj["declared_value"] >= row["min_value"]:
            return row
    return None


def compute_insurance(objects, snap):
    lenders = {row["lender_id"]: row for row in snap["lenders"]}
    for obj in objects:
        lender = lenders[obj["lender_id"]]
        obj["insurance_cents"] = round_half_up(
            obj["declared_value_cents"] * int(lender["insurance_rate_num"]),
            int(lender["insurance_rate_den"]),
        )


# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------

def write_lines(out_dir, name, rows):
    with open(os.path.join(out_dir, name), "w", encoding="utf-8", newline="") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")))
            handle.write("\n")


def write_object(out_dir, name, value):
    with open(os.path.join(out_dir, name), "w", encoding="utf-8", newline="") as handle:
        handle.write(json.dumps(value, sort_keys=True, indent=2))
        handle.write("\n")


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------

def run(data_dir, out_dir):
    snap = load_snapshot(data_dir)
    config = snap["config"]
    unit_factor = LENGTH_FACTORS[config["clearance_unit"]]
    setup_div = int(config["setup_div"])

    objects = build_objects(snap)
    by_id = {obj["object_id"]: obj for obj in objects}
    normalize_geometry(objects, unit_factor)
    admissible_orientations(objects)

    bays = build_bays(snap, unit_factor)
    bays_by_id = sorted(bays, key=lambda b: b["bay_id"])
    matrix = bay_candidates(objects, bays)

    select_placement(objects, bays, matrix)
    conservation_adjust(objects, bays, matrix, snap)

    crates = build_crates(snap, unit_factor)
    candidates = crate_candidates(objects, crates, unit_factor)

    edges = build_dependency_edges(objects, snap)
    node_ids = [obj["object_id"] for obj in objects]
    kept_edges, removed_edges, converged = resolve_cycles(node_ids, edges)

    compute_setup(objects, setup_div)
    order = install_order(objects, kept_edges)
    positions = {oid: index + 1 for index, oid in enumerate(order)}

    ledger, displaced = assign_crates(objects, snap, candidates, order)
    legs = transport_legs(objects, snap)
    compute_windows(objects, snap, config)
    compute_insurance(objects, snap)
    vclasses = value_class_table(snap)

    write_lines(out_dir, "canonical_objects.jsonl", [
        {
            "object_id": o["object_id"],
            "lender_id": o["lender_id"],
            "material_code": o["material_code"],
            "version": o["version"],
            "declared_value_cents": o["declared_value_cents"],
            "mass_g": o["mass_g"],
        }
        for o in objects
    ])

    write_lines(out_dir, "normalized_geometry.jsonl", [
        {
            "object_id": o["object_id"],
            "clearance_w": o["clearance_w"],
            "clearance_h": o["clearance_h"],
            "clearance_d": o["clearance_d"],
            "aspect_num": o["aspect_num"],
            "aspect_den": o["aspect_den"],
        }
        for o in objects
    ])

    write_lines(out_dir, "admissibility_matrix.jsonl", [
        {
            "object_id": o["object_id"],
            "orientations": o["orientations"],
            "tip_allowed": o["tip_allowed"],
            "rotate_allowed": o["rotate_allowed"],
        }
        for o in objects
    ])

    write_lines(out_dir, "bay_candidate_matrix.jsonl", [
        {
            "object_id": o["object_id"],
            "bay_id": bay["bay_id"],
            "fits": matrix[o["object_id"]][bay["bay_id"]] >= 0,
            "orientation": matrix[o["object_id"]][bay["bay_id"]],
        }
        for o in objects for bay in bays_by_id
    ])

    write_lines(out_dir, "selected_placement.jsonl", [
        {
            "object_id": o["object_id"],
            "bay_id": o["sel_bay"],
            "orientation": o["sel_orientation"],
            "code": o["sel_code"],
        }
        for o in objects
    ])

    write_lines(out_dir, "rejected_placement.jsonl", sorted(
        [{"object_id": o["object_id"], "code": o["sel_code"]}
         for o in objects if o["sel_code"] != "P_OK"],
        key=lambda r: (r["object_id"], r["code"]),
    ))

    write_lines(out_dir, "conservation_adjusted.jsonl", [
        {
            "object_id": o["object_id"],
            "bay_id": o["cons_bay"],
            "orientation": o["cons_orientation"],
            "code": o["cons_code"],
        }
        for o in objects
    ])

    write_lines(out_dir, "crate_candidate_matrix.jsonl", [
        {"object_id": oid, "crate_class": c["crate_class"], "residual": c["residual"]}
        for oid in sorted(candidates) for c in candidates[oid]
    ])

    write_lines(out_dir, "crate_assignment.jsonl", [
        {
            "object_id": oid,
            "crate_class": by_id[oid]["crate_class"],
            "code": by_id[oid]["crate_code"],
            "primary_class": by_id[oid]["primary_class"],
        }
        for oid in sorted(by_id)
    ])

    write_lines(out_dir, "crate_capacity_ledger.jsonl", ledger)

    write_lines(out_dir, "crate_contention_report.jsonl", [
        {
            "object_id": oid,
            "wanted_class": by_id[oid]["primary_class"],
            "assigned_class": by_id[oid]["crate_class"],
        }
        for oid in sorted(displaced)
    ])

    write_lines(out_dir, "transport_leg_manifest.jsonl", legs)

    write_lines(out_dir, "dependency_edges.jsonl", [
        {"from": e["from"], "to": e["to"], "kind": e["kind"], "weight": e["weight"]}
        for e in sorted(edges, key=lambda e: (e["from"], e["to"]))
    ])

    write_lines(out_dir, "cycle_resolution_audit.jsonl", [
        {"from": e["from"], "to": e["to"], "kind": e["kind"], "weight": e["weight"]}
        for e in removed_edges
    ])

    write_lines(out_dir, "install_order.jsonl", [
        {"position": positions[oid], "object_id": oid} for oid in order
    ])

    write_lines(out_dir, "crew_requirement_ledger.jsonl", [
        {
            "object_id": o["object_id"],
            "crew_class": o["crew_class"],
            "setup_seconds": o["setup_seconds"],
        }
        for o in objects
    ])

    write_lines(out_dir, "install_window_table.jsonl", [
        {
            "object_id": o["object_id"],
            "bay_id": o["cons_bay"],
            "window_start": o["window_start"],
            "window_end": o["window_end"],
            "required_days": o["required_days"],
            "code": o["schedule_code"],
        }
        for o in objects
    ])

    unschedulable = [o for o in objects if o["schedule_code"] != "S_OK"]
    write_lines(out_dir, "unschedulable_report.jsonl", sorted(
        [{"object_id": o["object_id"], "code": o["schedule_code"]} for o in unschedulable],
        key=lambda r: (r["object_id"], r["code"]),
    ))

    write_lines(out_dir, "gallery_occupancy_map.jsonl", [
        {
            "bay_id": bay["bay_id"],
            "object_count": len(members),
            "objects": members,
        }
        for bay, members in (
            (bay, sorted(o["object_id"] for o in objects
                         if o["cons_code"] == "P_OK" and o["cons_bay"] == bay["bay_id"]))
            for bay in bays_by_id
        )
    ])

    displaced_set = set(displaced)
    write_lines(out_dir, "deterministic_audit.jsonl", [
        {
            "object_id": o["object_id"],
            "placed_bay": o["cons_bay"],
            "crate_class": o["crate_class"],
            "displaced": o["object_id"] in displaced_set,
            "scheduled": o["schedule_code"] == "S_OK",
            "install_position": positions[o["object_id"]],
            "setup_seconds": o["setup_seconds"],
            "insurance_cents": o["insurance_cents"],
        }
        for o in objects
    ])

    unschedulable_penalty = 0
    for obj in unschedulable:
        row = value_class_of(obj, vclasses)
        if row is not None:
            unschedulable_penalty += row["unschedulable_penalty"]
    contention_penalty = 0
    for oid in displaced:
        row = value_class_of(by_id[oid], vclasses)
        if row is not None:
            contention_penalty += row["contention_penalty"]

    total_insurance = sum(o["insurance_cents"] for o in objects)
    total_setup = sum(o["setup_seconds"] for o in objects)
    total_residual = sum(o["residual"] for o in objects if o["crate_class"])
    placed_objects = sum(1 for o in objects if o["schedule_code"] == "S_OK")
    cycles_broken = len(removed_edges)

    write_object(out_dir, "penalty_summary.json", {
        "unschedulable_penalty": unschedulable_penalty,
        "contention_penalty": contention_penalty,
        "cycles_broken": cycles_broken,
        "total_insurance_cents": total_insurance,
    })

    objective = [placed_objects, unschedulable_penalty, contention_penalty,
                 cycles_broken, total_residual]
    write_object(out_dir, "objective_tuple.json", {
        "objective": objective,
        "order": snap["objective_order"]["order"],
    })

    program_path = os.path.abspath(__file__)
    manifest = {name: sha256_file(os.path.join(out_dir, name)) for name in JSONL_ARTIFACTS}
    for name in ("penalty_summary.json", "objective_tuple.json"):
        manifest[name] = sha256_file(os.path.join(out_dir, name))
    manifest[os.path.basename(program_path)] = sha256_file(program_path)
    write_object(out_dir, "integrity_manifest.json", manifest)

    chain = config["chain_seed"]
    for name in sorted(manifest):
        chain = hashlib.sha256((chain + manifest[name]).encode("utf-8")).hexdigest()

    write_object(out_dir, "final_state.json", {
        "chain": chain,
        "repair_converged": converged,
        "cycle_edges_removed": cycles_broken,
        "final_cycle_count": 0 if converged else 1,
        "displacement_count": len(displaced),
        "unschedulable_count": len(unschedulable),
        "placed_objects": placed_objects,
        "total_setup_seconds": total_setup,
        "total_insurance_cents": total_insurance,
        "objective": objective,
    })


def main(argv):
    if len(argv) != 3:
        sys.stderr.write("usage: plan.py <DATA_DIR> <OUT_DIR>\n")
        return 2
    os.makedirs(argv[2], exist_ok=True)
    run(argv[1], argv[2])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
