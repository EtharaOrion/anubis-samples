#!/usr/bin/env python3
"""Rebuild the travelling exhibition installation dossier from a snapshot.

Usage: python3 plan.py <DATA_DIR> <OUT_DIR>

Every quantity is an integer or an exact rational; no floating point value is
formed, compared or written anywhere in this program.
"""

import csv
import hashlib
import json
import os
import sys
from math import gcd

LEN_FACTOR = {"MM": 1000, "CM": 10000, "IN": 25400}
MASS_FACTOR = {"G": 1, "KG": 1000}
MM_TO_UM = 1000
VALUE_TO_CENTS = 100
SECONDS_PER_DAY = 3600
KIND_RANK = {"D_BAYSIZE": 0, "D_CONSERVATION": 1, "D_RULE": 2}


def round_half_up(num, den):
    """Round the rational num/den half up; den must be positive."""
    q, r = divmod(num, den)
    if 2 * r >= den:
        q += 1
    return q


def ceil_div(num, den):
    return -(-num // den)


def ratio(num, den):
    """Reduce num/den, using the sentinel zero over one for a zero denominator."""
    if den == 0:
        return 0, 1
    g = gcd(num, den)
    if g == 0:
        return 0, 1
    return num // g, den // g


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as handle:
        return [
            {k: (v.strip() if isinstance(v, str) else v) for k, v in row.items()}
            for row in csv.DictReader(handle)
        ]


def read_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def index_first(rows, key):
    """Index rows by a key column; the first occurrence of a key wins."""
    indexed = {}
    for row in rows:
        indexed.setdefault(row[key], row)
    return indexed


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
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def sensitivity(conservation_class):
    """Sort key where the larger value is the more sensitive conservation class."""
    digits = "".join(ch for ch in conservation_class if ch.isdigit())
    return (int(digits) if digits else 0, conservation_class)


class Snapshot:
    """The shipped flat files, read once and indexed."""

    def __init__(self, data_dir):
        self.config = read_json(os.path.join(data_dir, "horizon_config.json"))
        self.objective_order = read_json(os.path.join(data_dir, "objective_order.json"))

        self.order = []
        self.master = {}
        for row in read_csv(os.path.join(data_dir, "object_master.csv")):
            oid = row["object_id"]
            if oid in self.master:
                continue
            self.master[oid] = row
            self.order.append(oid)

        self.dims = index_first(read_csv(os.path.join(data_dir, "object_dimensions.csv")), "object_id")
        self.mass = index_first(read_csv(os.path.join(data_dir, "object_mass.csv")), "object_id")
        self.value = index_first(read_csv(os.path.join(data_dir, "object_value.csv")), "object_id")
        self.orient = index_first(read_csv(os.path.join(data_dir, "object_orientation.csv")), "object_id")

        self.version = {}
        for row in read_csv(os.path.join(data_dir, "object_version_map.csv")):
            oid, ver = row["object_id"], int(row["version"])
            if oid not in self.version or ver > self.version[oid]:
                self.version[oid] = ver

        self.material = index_first(read_csv(os.path.join(data_dir, "material_rules.csv")), "material_code")
        self.handling = index_first(read_csv(os.path.join(data_dir, "handling_class_rules.csv")), "handling_class")

        self.bays = index_first(read_csv(os.path.join(data_dir, "gallery_bays.csv")), "bay_id")
        self.bay_ids = sorted(self.bays)

        self.bay_blocked = {}
        for row in read_csv(os.path.join(data_dir, "bay_exceptions.csv")):
            self.bay_blocked.setdefault(row["bay_id"], set()).add(row["blocked_class"])

        self.crates = index_first(read_csv(os.path.join(data_dir, "crate_catalog.csv")), "crate_class")
        self.crate_ids = sorted(self.crates)

        self.reservations = {}
        for row in read_csv(os.path.join(data_dir, "crate_reservations.csv")):
            key = (row["crate_class"], row["lender_id"])
            self.reservations[key] = self.reservations.get(key, 0) + int(row["reserved_count"])

        self.prior = {}
        for row in read_csv(os.path.join(data_dir, "prior_state.csv")):
            cls = row["crate_class"]
            self.prior[cls] = self.prior.get(cls, 0) + int(row["consumed_count"])

        self.conservation = index_first(read_csv(os.path.join(data_dir, "conservation_rules.csv")), "conservation_class")
        self.venue = index_first(read_csv(os.path.join(data_dir, "venue_windows.csv")), "bay_id")
        self.crew = index_first(read_csv(os.path.join(data_dir, "crew_roster.csv")), "crew_class")
        self.dep_rules = read_csv(os.path.join(data_dir, "dependency_rules.csv"))

        self.transport = {}
        for row in read_csv(os.path.join(data_dir, "transport_rules.csv")):
            self.transport.setdefault(row["lender_id"], []).append(row)

        self.lenders = index_first(read_csv(os.path.join(data_dir, "lender_master.csv")), "lender_id")
        self.value_classes = read_csv(os.path.join(data_dir, "value_class_rules.csv"))

    def bay_interior(self, bay_id):
        bay = self.bays[bay_id]
        return (
            int(bay["interior_w"]) * MM_TO_UM,
            int(bay["interior_h"]) * MM_TO_UM,
            int(bay["interior_d"]) * MM_TO_UM,
        )

    def bay_scan_order(self):
        return sorted(self.bay_ids, key=lambda b: (int(self.bays[b]["bay_priority"]), b))

    def crate_interior(self, crate_class):
        crate = self.crates[crate_class]
        return (
            int(crate["inner_w"]) * MM_TO_UM,
            int(crate["inner_h"]) * MM_TO_UM,
            int(crate["inner_d"]) * MM_TO_UM,
        )


def stage1_canonical(snap):
    spine = {}
    clearance_factor = LEN_FACTOR[snap.config["clearance_unit"]]
    for oid in snap.order:
        master = snap.master[oid]
        dims = snap.dims[oid]
        factor = LEN_FACTOR[dims["dim_unit"]]
        mass_row = snap.mass[oid]
        material = snap.material[master["material_code"]]
        handling = snap.handling[material["handling_class"]]
        declared = int(snap.value[oid]["declared_value"])
        spine[oid] = {
            "object_id": oid,
            "lender_id": master["lender_id"],
            "material_code": master["material_code"],
            "version": snap.version.get(oid, 0),
            "declared_value": declared,
            "declared_value_cents": declared * VALUE_TO_CENTS,
            "mass_g": int(mass_row["mass"]) * MASS_FACTOR[mass_row["mass_unit"]],
            "width": int(dims["width"]) * factor,
            "height": int(dims["height"]) * factor,
            "depth": int(dims["depth"]) * factor,
            "conservation_class": material["conservation_class"],
            "max_lux": int(material["max_lux"]),
            "handling_class": material["handling_class"],
            "crew_class": handling["crew_class"],
            "setup_base": int(handling["setup_base"]),
            "clearance_margin_w": int(handling["clearance_w"]) * clearance_factor,
            "clearance_margin_d": int(handling["clearance_d"]) * clearance_factor,
            "clearance_margin_h": int(handling["clearance_h"]) * clearance_factor,
            "handle_margin": int(handling["handle_margin"]) * clearance_factor,
            "tip_allowed": int(snap.orient[oid]["tip_allowed"]),
            "rotate_allowed": int(snap.orient[oid]["rotate_allowed"]),
        }
    return spine


def stage2_geometry(spine):
    for state in spine.values():
        state["clearance_w"] = state["width"] + 2 * state["clearance_margin_w"]
        state["clearance_d"] = state["depth"] + 2 * state["clearance_margin_d"]
        state["clearance_h"] = state["height"] + state["clearance_margin_h"]
        state["aspect_num"], state["aspect_den"] = ratio(state["height"], state["width"])
        margin = state["handle_margin"]
        state["handling_w"] = state["width"] + margin
        state["handling_h"] = state["height"] + margin
        state["handling_d"] = state["depth"] + margin
        state["handling_volume"] = state["handling_w"] * state["handling_h"] * state["handling_d"]


def stage3_admissibility(spine):
    for state in spine.values():
        orientations = [0]
        if state["rotate_allowed"]:
            orientations.append(1)
        if state["tip_allowed"]:
            orientations.extend([2, 3])
        state["orientations"] = orientations


def oriented_extent(state, index):
    """(footprint width, footprint depth, vertical extent) of an orientation."""
    cw, ch, cd = state["clearance_w"], state["clearance_h"], state["clearance_d"]
    if index == 0:
        return cw, cd, ch
    if index == 1:
        return cd, cw, ch
    if index == 2:
        return cw, ch, cd
    return ch, cw, cd


def orientation_fits(state, index, interior):
    interior_w, interior_h, interior_d = interior
    footprint_w, footprint_d, vertical = oriented_extent(state, index)
    return footprint_w <= interior_w and footprint_d < interior_d and vertical <= interior_h


def stage4_bay_candidates(snap, spine):
    for state in spine.values():
        fits = {}
        for bay_id in snap.bay_ids:
            interior = snap.bay_interior(bay_id)
            chosen = -1
            for index in state["orientations"]:
                if orientation_fits(state, index, interior):
                    chosen = index
                    break
            fits[bay_id] = chosen
        state["bay_fit"] = fits


def stage5_selected(snap, spine):
    for state in spine.values():
        placement = ("", -1)
        for candidate in snap.bay_scan_order():
            if state["bay_fit"][candidate] >= 0:
                placement = (candidate, state["bay_fit"][candidate])
                break
        state["selected_bay"], state["selected_orientation"] = placement
        if placement[0]:
            state["selected_code"] = "P_OK"
        else:
            state["selected_code"] = "P_NO_ORIENTATION" if state["orientations"] == [0] else "P_NO_FIT"


def conservation_ok(snap, state, bay_id):
    bay = snap.bays[bay_id]
    lux = int(bay["lux_level"])
    if lux > state["max_lux"]:
        return False
    if state["conservation_class"] in snap.bay_blocked.get(bay_id, set()):
        return False
    if int(snap.conservation[state["conservation_class"]]["needs_dark"]) and lux > 0:
        return False
    return True


def stage6_conservation(snap, spine):
    for state in spine.values():
        placement = ("", -1)
        for candidate in snap.bay_scan_order():
            if state["bay_fit"][candidate] < 0 or not conservation_ok(snap, state, candidate):
                continue
            placement = (candidate, state["bay_fit"][candidate])
            break
        state["placed_bay"], state["placed_orientation"] = placement
        if placement[0]:
            state["placed_code"] = "P_OK"
            continue
        geometric = [b for b in snap.bay_ids if state["bay_fit"][b] >= 0]
        if not geometric:
            state["placed_code"] = state["selected_code"]
        elif len(geometric) == 1 and state["conservation_class"] in snap.bay_blocked.get(geometric[0], set()):
            state["placed_code"] = "P_BAY_EXCEPTION"
        else:
            state["placed_code"] = "P_CONSERVATION_BLOCK"


def stage7_crate_candidates(snap, spine):
    for state in spine.values():
        handling = sorted((state["handling_w"], state["handling_h"], state["handling_d"]))
        candidates = []
        for crate_class in snap.crate_ids:
            interior = sorted(snap.crate_interior(crate_class))
            if all(handling[axis] <= interior[axis] for axis in range(3)):
                volume = interior[0] * interior[1] * interior[2]
                candidates.append((crate_class, volume - state["handling_volume"]))
        candidates.sort(key=lambda item: (item[1], item[0]))
        state["crate_candidates"] = candidates
        state["primary_class"] = candidates[0][0] if candidates else ""


def stage13_setup(snap, spine):
    setup_div = int(snap.config["setup_div"])
    for state in spine.values():
        state["setup_seconds"] = round_half_up(state["setup_base"] * state["mass_g"], setup_div)
        state["required_days"] = ceil_div(state["setup_seconds"], SECONDS_PER_DAY)


def stage10_edges(snap, spine):
    edges = {}

    def add(from_id, to_id, kind, weight):
        if from_id == to_id:
            return
        key = (from_id, to_id)
        existing = edges.get(key)
        if existing is None or KIND_RANK[kind] > KIND_RANK[existing[0]]:
            edges[key] = (kind, weight)

    by_bay = {}
    for oid in snap.order:
        bay_id = spine[oid]["placed_bay"]
        if bay_id:
            by_bay.setdefault(bay_id, []).append(oid)

    for bay_id in sorted(by_bay):
        members = sorted(by_bay[bay_id])
        for i, first in enumerate(members):
            for second in members[i + 1:]:
                volume_first = spine[first]["handling_volume"]
                volume_second = spine[second]["handling_volume"]
                if volume_second > volume_first:
                    earlier, later = second, first
                else:
                    earlier, later = first, second
                add(earlier, later, "D_BAYSIZE", spine[earlier]["handling_volume"])

    for bay_id in sorted(by_bay):
        members = sorted(by_bay[bay_id])
        for first in members:
            for second in members:
                if first == second:
                    continue
                if sensitivity(spine[first]["conservation_class"]) > sensitivity(
                    spine[second]["conservation_class"]
                ):
                    add(first, second, "D_CONSERVATION", spine[first]["handling_volume"])

    for row in snap.dep_rules:
        from_id, to_id = row["from_object"], row["to_object"]
        if from_id in spine and to_id in spine:
            add(from_id, to_id, "D_RULE", int(row["edge_priority"]))

    return [
        {"from": key[0], "to": key[1], "kind": value[0], "weight": value[1]}
        for key, value in sorted(edges.items())
    ]


def find_cycle(nodes, adjacency):
    """Deterministic depth first search returning the nodes of one cycle."""
    white, grey, black = 0, 1, 2
    color = {node: white for node in nodes}
    for root in nodes:
        if color[root] != white:
            continue
        color[root] = grey
        path = [root]
        stack = [(root, 0)]
        while stack:
            node, index = stack[-1]
            neighbours = adjacency.get(node, [])
            if index < len(neighbours):
                stack[-1] = (node, index + 1)
                nxt = neighbours[index]
                if color.get(nxt, white) == grey:
                    return path[path.index(nxt):]
                if color.get(nxt, white) == white:
                    color[nxt] = grey
                    path.append(nxt)
                    stack.append((nxt, 0))
            else:
                color[node] = black
                stack.pop()
                path.pop()
    return None


def stage11_cycles(nodes, edges):
    live = {(edge["from"], edge["to"]): edge for edge in edges}
    removed = []
    while True:
        adjacency = {}
        for from_id, to_id in sorted(live):
            adjacency.setdefault(from_id, []).append(to_id)
        cycle = find_cycle(nodes, adjacency)
        if cycle is None:
            break
        pairs = [(cycle[i], cycle[(i + 1) % len(cycle)]) for i in range(len(cycle))]
        victim = min(
            pairs,
            key=lambda pair: (
                KIND_RANK[live[pair]["kind"]],
                live[pair]["weight"],
                pair[0],
                pair[1],
            ),
        )
        removed.append(dict(live.pop(victim)))
    return removed, live


def stage12_install_order(nodes, spine, live_edges):
    predecessors = {node: set() for node in nodes}
    for from_id, to_id in live_edges:
        predecessors[to_id].add(from_id)
    remaining = set(nodes)
    order = []
    while remaining:
        ready = [node for node in remaining if not (predecessors[node] & remaining)]
        ready.sort(key=lambda node: (spine[node]["setup_seconds"], node))
        chosen = ready[0]
        order.append(chosen)
        remaining.discard(chosen)
    return order


def stage8_crates(snap, spine, install_order):
    remaining = {}
    for crate_class in snap.crate_ids:
        remaining[crate_class] = int(snap.crates[crate_class]["count"]) - snap.prior.get(crate_class, 0)
    reservations = dict(snap.reservations)
    displaced = []
    for oid in install_order:
        state = spine[oid]
        options = []
        for crate_class, residual in state["crate_candidates"]:
            if remaining.get(crate_class, 0) <= 0:
                continue
            reserved = 1 if reservations.get((crate_class, state["lender_id"]), 0) > 0 else 0
            options.append((-reserved, residual, crate_class))
        if not options:
            state["crate_class"] = ""
            state["crate_code"] = "C_NO_CRATE"
            state["crate_residual"] = 0
            continue
        options.sort()
        chosen = options[0][2]
        remaining[chosen] -= 1
        key = (chosen, state["lender_id"])
        if reservations.get(key, 0) > 0:
            reservations[key] -= 1
        state["crate_class"] = chosen
        state["crate_residual"] = options[0][1]
        if chosen == state["primary_class"]:
            state["crate_code"] = "C_PRIMARY"
        else:
            state["crate_code"] = "C_DISPLACED"
            displaced.append(oid)
    return remaining, displaced


def stage9_transport(snap, spine):
    for state in spine.values():
        if not state["crate_class"]:
            state["mode"] = ""
            state["transport_exception"] = False
            continue
        mode = snap.crates[state["crate_class"]]["transport_mode"]
        exception = False
        for rule in snap.transport.get(state["lender_id"], []):
            if rule["forbidden_mode"] == mode:
                mode = rule["fallback_mode"]
                exception = True
                break
        state["mode"] = mode
        state["transport_exception"] = exception


def stage13_windows(snap, spine):
    venue_shift = 1 if snap.config["venue_end_convention"] == "exclusive" else 0
    conservation_shift = 1 if snap.config["conservation_end_convention"] == "exclusive" else 0
    for state in spine.values():
        cons = snap.conservation[state["conservation_class"]]
        crew = snap.crew[state["crew_class"]]
        starts = [int(cons["setup_start"]), int(crew["avail_start"])]
        ends = [int(cons["setup_end"]) - conservation_shift, int(crew["avail_end"])]
        bay_id = state["placed_bay"]
        if bay_id:
            venue = snap.venue[bay_id]
            starts.append(int(venue["access_start"]))
            ends.append(int(venue["access_end"]) - venue_shift)
        low, high = max(starts), min(ends)
        state["window_start"] = low
        state["window_end"] = high
        available = high - low + 1
        state["window_days"] = available
        if not bay_id:
            state["schedule_code"] = "S_NO_PLACEMENT"
        elif not state["crate_class"]:
            state["schedule_code"] = "S_NO_CRATE"
        elif high < low:
            state["schedule_code"] = "S_NO_WINDOW"
        elif state["required_days"] > available:
            state["schedule_code"] = "S_SHORT_WINDOW"
        else:
            state["schedule_code"] = "S_OK"
        state["scheduled"] = state["schedule_code"] == "S_OK"


def value_class_of(snap, state):
    best = None
    for row in snap.value_classes:
        if state["declared_value"] < int(row["min_value"]):
            continue
        key = (int(row["min_value"]), row["value_class"])
        if best is None or key > (int(best["min_value"]), best["value_class"]):
            best = row
    return best


def insurance_cents(snap, state):
    lender = snap.lenders[state["lender_id"]]
    num = int(lender["insurance_rate_num"])
    den = int(lender["insurance_rate_den"])
    if den == 0:
        return 0
    return round_half_up(state["declared_value_cents"] * num, den)


HASHED_ARTIFACTS = [
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
    "penalty_summary.json",
    "objective_tuple.json",
]


def build(data_dir, out_dir):
    snap = Snapshot(data_dir)
    spine = stage1_canonical(snap)
    stage2_geometry(spine)
    stage3_admissibility(spine)
    stage4_bay_candidates(snap, spine)
    stage5_selected(snap, spine)
    stage6_conservation(snap, spine)
    stage7_crate_candidates(snap, spine)
    stage13_setup(snap, spine)

    nodes = sorted(snap.order)
    edges = stage10_edges(snap, spine)
    audit, live_edges = stage11_cycles(nodes, edges)
    install_order = stage12_install_order(nodes, spine, live_edges)
    position = {oid: index + 1 for index, oid in enumerate(install_order)}

    remaining, displaced = stage8_crates(snap, spine, install_order)
    stage9_transport(snap, spine)
    stage13_windows(snap, spine)

    for oid, state in spine.items():
        state["install_position"] = position[oid]
        state["insurance_cents"] = insurance_cents(snap, state)
        state["displaced"] = state["crate_code"] == "C_DISPLACED"

    order = snap.order
    by_id = sorted(snap.order)
    os.makedirs(out_dir, exist_ok=True)

    write_lines(
        os.path.join(out_dir, "canonical_objects.jsonl"),
        [
            {
                "object_id": oid,
                "lender_id": spine[oid]["lender_id"],
                "material_code": spine[oid]["material_code"],
                "version": spine[oid]["version"],
                "declared_value_cents": spine[oid]["declared_value_cents"],
                "mass_g": spine[oid]["mass_g"],
            }
            for oid in order
        ],
    )

    write_lines(
        os.path.join(out_dir, "normalized_geometry.jsonl"),
        [
            {
                "object_id": oid,
                "clearance_w": spine[oid]["clearance_w"],
                "clearance_h": spine[oid]["clearance_h"],
                "clearance_d": spine[oid]["clearance_d"],
                "aspect_num": spine[oid]["aspect_num"],
                "aspect_den": spine[oid]["aspect_den"],
            }
            for oid in order
        ],
    )

    write_lines(
        os.path.join(out_dir, "admissibility_matrix.jsonl"),
        [
            {
                "object_id": oid,
                "orientations": spine[oid]["orientations"],
                "tip_allowed": spine[oid]["tip_allowed"],
                "rotate_allowed": spine[oid]["rotate_allowed"],
            }
            for oid in order
        ],
    )

    write_lines(
        os.path.join(out_dir, "bay_candidate_matrix.jsonl"),
        [
            {
                "object_id": oid,
                "bay_id": bay_id,
                "fits": spine[oid]["bay_fit"][bay_id] >= 0,
                "orientation": spine[oid]["bay_fit"][bay_id],
            }
            for oid in order
            for bay_id in snap.bay_ids
        ],
    )

    write_lines(
        os.path.join(out_dir, "selected_placement.jsonl"),
        [
            {
                "object_id": oid,
                "bay_id": spine[oid]["selected_bay"],
                "orientation": spine[oid]["selected_orientation"],
                "code": spine[oid]["selected_code"],
            }
            for oid in order
        ],
    )

    rejected = set()
    for oid in order:
        for code in (spine[oid]["selected_code"], spine[oid]["placed_code"]):
            if code != "P_OK":
                rejected.add((oid, code))
    write_lines(
        os.path.join(out_dir, "rejected_placement.jsonl"),
        [{"object_id": oid, "code": code} for oid, code in sorted(rejected)],
    )

    write_lines(
        os.path.join(out_dir, "conservation_adjusted.jsonl"),
        [
            {
                "object_id": oid,
                "bay_id": spine[oid]["placed_bay"],
                "orientation": spine[oid]["placed_orientation"],
                "code": spine[oid]["placed_code"],
            }
            for oid in order
        ],
    )

    write_lines(
        os.path.join(out_dir, "crate_candidate_matrix.jsonl"),
        [
            {"object_id": oid, "crate_class": crate_class, "residual": residual}
            for oid in by_id
            for crate_class, residual in spine[oid]["crate_candidates"]
        ],
    )

    write_lines(
        os.path.join(out_dir, "crate_assignment.jsonl"),
        [
            {
                "object_id": oid,
                "crate_class": spine[oid]["crate_class"],
                "code": spine[oid]["crate_code"],
                "primary_class": spine[oid]["primary_class"],
            }
            for oid in by_id
        ],
    )

    write_lines(
        os.path.join(out_dir, "crate_capacity_ledger.jsonl"),
        [
            {
                "crate_class": crate_class,
                "catalog_count": int(snap.crates[crate_class]["count"]),
                "prior_consumed": snap.prior.get(crate_class, 0),
                "remaining": remaining[crate_class],
            }
            for crate_class in snap.crate_ids
        ],
    )

    write_lines(
        os.path.join(out_dir, "crate_contention_report.jsonl"),
        [
            {
                "object_id": oid,
                "wanted_class": spine[oid]["primary_class"],
                "assigned_class": spine[oid]["crate_class"],
            }
            for oid in by_id
            if spine[oid]["displaced"]
        ],
    )

    write_lines(
        os.path.join(out_dir, "transport_leg_manifest.jsonl"),
        [
            {
                "object_id": oid,
                "crate_class": spine[oid]["crate_class"],
                "mode": spine[oid]["mode"],
                "exception": spine[oid]["transport_exception"],
            }
            for oid in order
            if spine[oid]["crate_class"]
        ],
    )

    write_lines(os.path.join(out_dir, "dependency_edges.jsonl"), edges)
    write_lines(os.path.join(out_dir, "cycle_resolution_audit.jsonl"), audit)

    write_lines(
        os.path.join(out_dir, "install_order.jsonl"),
        [{"position": index + 1, "object_id": oid} for index, oid in enumerate(install_order)],
    )

    write_lines(
        os.path.join(out_dir, "crew_requirement_ledger.jsonl"),
        [
            {
                "object_id": oid,
                "crew_class": spine[oid]["crew_class"],
                "setup_seconds": spine[oid]["setup_seconds"],
            }
            for oid in order
        ],
    )

    write_lines(
        os.path.join(out_dir, "install_window_table.jsonl"),
        [
            {
                "object_id": oid,
                "bay_id": spine[oid]["placed_bay"],
                "window_start": spine[oid]["window_start"],
                "window_end": spine[oid]["window_end"],
                "required_days": spine[oid]["required_days"],
                "code": spine[oid]["schedule_code"],
            }
            for oid in order
        ],
    )

    unschedulable = sorted(
        (oid, spine[oid]["schedule_code"]) for oid in order if not spine[oid]["scheduled"]
    )
    write_lines(
        os.path.join(out_dir, "unschedulable_report.jsonl"),
        [{"object_id": oid, "code": code} for oid, code in unschedulable],
    )

    occupancy = {bay_id: [] for bay_id in snap.bay_ids}
    for oid in by_id:
        bay_id = spine[oid]["placed_bay"]
        if bay_id:
            occupancy[bay_id].append(oid)
    write_lines(
        os.path.join(out_dir, "gallery_occupancy_map.jsonl"),
        [
            {"bay_id": bay_id, "object_count": len(occupancy[bay_id]), "objects": occupancy[bay_id]}
            for bay_id in snap.bay_ids
        ],
    )

    write_lines(
        os.path.join(out_dir, "deterministic_audit.jsonl"),
        [
            {
                "object_id": oid,
                "placed_bay": spine[oid]["placed_bay"],
                "crate_class": spine[oid]["crate_class"],
                "displaced": spine[oid]["displaced"],
                "scheduled": spine[oid]["scheduled"],
                "install_position": spine[oid]["install_position"],
                "setup_seconds": spine[oid]["setup_seconds"],
                "insurance_cents": spine[oid]["insurance_cents"],
            }
            for oid in order
        ],
    )

    unschedulable_penalty = 0
    contention_penalty = 0
    for oid in order:
        row = value_class_of(snap, spine[oid])
        if row is None:
            continue
        if not spine[oid]["scheduled"]:
            unschedulable_penalty += int(row["unschedulable_penalty"])
        if spine[oid]["displaced"]:
            contention_penalty += int(row["contention_penalty"])

    total_insurance = sum(spine[oid]["insurance_cents"] for oid in order)
    total_setup = sum(spine[oid]["setup_seconds"] for oid in order)
    total_residual = sum(spine[oid]["crate_residual"] for oid in order)
    placed_objects = sum(1 for oid in order if spine[oid]["scheduled"])
    cycles_broken = len(audit)

    write_object(
        os.path.join(out_dir, "penalty_summary.json"),
        {
            "unschedulable_penalty": unschedulable_penalty,
            "contention_penalty": contention_penalty,
            "cycles_broken": cycles_broken,
            "total_insurance_cents": total_insurance,
        },
    )

    objective = [
        placed_objects,
        unschedulable_penalty,
        contention_penalty,
        cycles_broken,
        total_residual,
    ]
    write_object(
        os.path.join(out_dir, "objective_tuple.json"),
        {"objective": objective, "order": list(snap.objective_order["order"])},
    )

    manifest = {name: sha256_file(os.path.join(out_dir, name)) for name in HASHED_ARTIFACTS}
    program = os.path.abspath(__file__)
    manifest[os.path.basename(program)] = sha256_file(program)
    write_object(os.path.join(out_dir, "integrity_manifest.json"), manifest)

    chain = snap.config["chain_seed"]
    for name in sorted(manifest):
        chain = hashlib.sha256((chain + manifest[name]).encode("utf-8")).hexdigest()

    write_object(
        os.path.join(out_dir, "final_state.json"),
        {
            "chain": chain,
            "repair_converged": True,
            "cycle_edges_removed": cycles_broken,
            "final_cycle_count": 0,
            "displacement_count": len(displaced),
            "unschedulable_count": len(unschedulable),
            "placed_objects": placed_objects,
            "total_setup_seconds": total_setup,
            "total_insurance_cents": total_insurance,
            "objective": objective,
        },
    )


def main(argv):
    if len(argv) != 3:
        sys.stderr.write("usage: plan.py <DATA_DIR> <OUT_DIR>\n")
        return 2
    build(argv[1], argv[2])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
