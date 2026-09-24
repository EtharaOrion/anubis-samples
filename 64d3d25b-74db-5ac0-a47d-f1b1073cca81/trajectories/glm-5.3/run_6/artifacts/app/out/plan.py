#!/usr/bin/env python3
"""Derive the installation dossier from an administrative snapshot.

Usage: plan.py <DATA_DIR> <OUT_DIR>

All arithmetic is integer or exact rational; no float is ever computed,
compared or written. Every rational a stage forms is rounded half up to that
stage's quantum and the rounded integer is what later stages consume.
"""

import csv
import hashlib
import json
import os
import re
import sys
from fractions import Fraction

MM = 1000
CM = 10000
IN = 25400
SECONDS_PER_DAY = 3600

P_OK = "P_OK"
P_NO_FIT = "P_NO_FIT"
P_NO_ORIENTATION = "P_NO_ORIENTATION"
P_CONSERVATION_BLOCK = "P_CONSERVATION_BLOCK"

C_PRIMARY = "C_PRIMARY"
C_DISPLACED = "C_DISPLACED"
C_NO_CRATE = "C_NO_CRATE"

D_RULE = "D_RULE"
D_CONSERVATION = "D_CONSERVATION"
D_BAYSIZE = "D_BAYSIZE"

S_OK = "S_OK"
S_NO_PLACEMENT = "S_NO_PLACEMENT"
S_NO_CRATE = "S_NO_CRATE"
S_NO_WINDOW = "S_NO_WINDOW"
S_SHORT_WINDOW = "S_SHORT_WINDOW"

KIND_RANK = {D_BAYSIZE: 0, D_CONSERVATION: 1, D_RULE: 2}
KIND_PRIORITY = {D_RULE: 2, D_CONSERVATION: 1, D_BAYSIZE: 0}


class BadSnapshot(Exception):
    pass


# ---------------------------------------------------------------- input


def read_csv_rows(path):
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def keyed(data_dir, name, key_field, value_fn):
    """Read a csv as a map keyed by one field; the first occurrence wins."""
    out = {}
    for row in read_csv_rows(os.path.join(data_dir, name)):
        key = row[key_field].strip()
        if key in out:
            continue
        out[key] = value_fn(row)
    return out


def as_int(text, where):
    stripped = str(text).strip()
    if not stripped:
        raise BadSnapshot("empty integer in " + where)
    return int(stripped)


def as_flag(text):
    return str(text).strip() not in ("", "0", "false", "FALSE", "False", "no")


def round_half_up(numer, denom):
    if denom == 0:
        raise BadSnapshot("zero denominator in a rounded quantity")
    if numer >= 0:
        quot, rem = divmod(numer, denom)
        if 2 * rem >= denom:
            quot += 1
        return quot
    quot, rem = divmod(-numer, denom)
    if 2 * rem >= denom:
        quot += 1
    return -quot


def reduced_pair(numer, denom):
    """Reduce a ratio; a zero denominator yields the 0/1 sentinel."""
    if denom == 0:
        return 0, 1
    frac = Fraction(numer, denom)
    return frac.numerator, frac.denominator


def natural_key(text):
    """Order identifiers with embedded numbers numerically (L2 before L10).

    Token positions alternate between text and digit runs, so tokens at the
    same position are always the same type and never fail to compare.
    """
    return tuple(
        int(part) if part.isdigit() else part for part in re.split(r"(\d+)", text)
    )


def load_objects(data_dir):
    master = []
    seen = set()
    for row in read_csv_rows(os.path.join(data_dir, "object_master.csv")):
        oid = row["object_id"].strip()
        if oid in seen:
            continue
        seen.add(oid)
        master.append((oid, row["lender_id"].strip(), row["material_code"].strip()))

    version = {}
    for row in read_csv_rows(os.path.join(data_dir, "object_version_map.csv")):
        oid = row["object_id"].strip()
        ver = as_int(row["version"], "object_version_map")
        if oid not in version or ver > version[oid]:
            version[oid] = ver

    dims = keyed(
        data_dir,
        "object_dimensions.csv",
        "object_id",
        lambda r: (
            as_int(r["width"], "width"),
            as_int(r["height"], "height"),
            as_int(r["depth"], "depth"),
            r["dim_unit"].strip(),
        ),
    )
    masses = keyed(
        data_dir,
        "object_mass.csv",
        "object_id",
        lambda r: (as_int(r["mass"], "mass"), r["mass_unit"].strip()),
    )
    values = keyed(
        data_dir,
        "object_value.csv",
        "object_id",
        lambda r: as_int(r["declared_value"], "declared_value"),
    )
    orientations = keyed(
        data_dir,
        "object_orientation.csv",
        "object_id",
        lambda r: (as_flag(r["tip_allowed"]), as_flag(r["rotate_allowed"])),
    )

    length_factors = {"MM": MM, "CM": CM, "IN": IN}
    mass_factors = {"G": 1, "KG": 1000}

    objects = []
    for oid, lender, material in master:
        width, height, depth, dim_unit = dims[oid]
        mass_raw, mass_unit = masses[oid]
        if dim_unit not in length_factors:
            raise BadSnapshot("unknown length unit " + dim_unit)
        if mass_unit not in mass_factors:
            raise BadSnapshot("unknown mass unit " + mass_unit)
        factor = length_factors[dim_unit]
        objects.append(
            {
                "object_id": oid,
                "lender_id": lender,
                "material_code": material,
                "version": version.get(oid, 0),
                "width_um": width * factor,
                "height_um": height * factor,
                "depth_um": depth * factor,
                "declared_value_cents": values[oid] * 100,
                "mass_g": mass_raw * mass_factors[mass_unit],
                "tip_allowed": orientations[oid][0],
                "rotate_allowed": orientations[oid][1],
            }
        )
    return objects


def load_rules(data_dir):
    materials = keyed(
        data_dir,
        "material_rules.csv",
        "material_code",
        lambda r: {
            "conservation_class": r["conservation_class"].strip(),
            "max_lux": as_int(r["max_lux"], "max_lux"),
            "handling_class": r["handling_class"].strip(),
        },
    )
    handling = keyed(
        data_dir,
        "handling_class_rules.csv",
        "handling_class",
        lambda r: {
            "clearance_w": as_int(r["clearance_w"], "clearance_w") * MM,
            "clearance_d": as_int(r["clearance_d"], "clearance_d") * MM,
            "clearance_h": as_int(r["clearance_h"], "clearance_h") * MM,
            "handle_margin": as_int(r["handle_margin"], "handle_margin") * MM,
            "crew_class": r["crew_class"].strip(),
            "setup_base": as_int(r["setup_base"], "setup_base"),
        },
    )
    bays = keyed(
        data_dir,
        "gallery_bays.csv",
        "bay_id",
        lambda r: {
            "bay_id": r["bay_id"].strip(),
            "interior_w": as_int(r["interior_w"], "interior_w") * MM,
            "interior_h": as_int(r["interior_h"], "interior_h") * MM,
            "interior_d": as_int(r["interior_d"], "interior_d") * MM,
            "lux_level": as_int(r["lux_level"], "lux_level"),
            "bay_priority": as_int(r["bay_priority"], "bay_priority"),
        },
    )
    exceptions = {}
    for row in read_csv_rows(os.path.join(data_dir, "bay_exceptions.csv")):
        exceptions.setdefault(row["bay_id"].strip(), []).append(
            row["blocked_class"].strip()
        )
    crates = keyed(
        data_dir,
        "crate_catalog.csv",
        "crate_class",
        lambda r: {
            "crate_class": r["crate_class"].strip(),
            "sorted_dims": sorted(
                [
                    as_int(r["inner_w"], "inner_w") * MM,
                    as_int(r["inner_h"], "inner_h") * MM,
                    as_int(r["inner_d"], "inner_d") * MM,
                ]
            ),
            "volume": as_int(r["inner_w"], "inner_w")
            * as_int(r["inner_h"], "inner_h")
            * as_int(r["inner_d"], "inner_d")
            * MM ** 3,
            "count": as_int(r["count"], "count"),
            "transport_mode": r["transport_mode"].strip(),
        },
    )
    reservations = {}
    for row in read_csv_rows(os.path.join(data_dir, "crate_reservations.csv")):
        key = (row["crate_class"].strip(), row["lender_id"].strip())
        reservations[key] = reservations.get(key, 0) + as_int(
            row["reserved_count"], "reserved_count"
        )
    prior = {}
    for row in read_csv_rows(os.path.join(data_dir, "prior_state.csv")):
        crate_class = row["crate_class"].strip()
        prior[crate_class] = prior.get(crate_class, 0) + as_int(
            row["consumed_count"], "consumed_count"
        )
    conservation = keyed(
        data_dir,
        "conservation_rules.csv",
        "conservation_class",
        lambda r: {
            "setup_start": as_int(r["setup_start"], "setup_start"),
            "setup_end": as_int(r["setup_end"], "setup_end"),
            "needs_dark": as_flag(r["needs_dark"]),
        },
    )
    venue_windows = keyed(
        data_dir,
        "venue_windows.csv",
        "bay_id",
        lambda r: (
            as_int(r["access_start"], "access_start"),
            as_int(r["access_end"], "access_end"),
        ),
    )
    crew = keyed(
        data_dir,
        "crew_roster.csv",
        "crew_class",
        lambda r: (
            as_int(r["avail_start"], "avail_start"),
            as_int(r["avail_end"], "avail_end"),
        ),
    )
    transport = keyed(
        data_dir,
        "transport_rules.csv",
        "lender_id",
        lambda r: (r["forbidden_mode"].strip(), r["fallback_mode"].strip()),
    )
    lenders = keyed(
        data_dir,
        "lender_master.csv",
        "lender_id",
        lambda r: {
            "num": as_int(r["insurance_rate_num"], "insurance_rate_num"),
            "den": as_int(r["insurance_rate_den"], "insurance_rate_den"),
        },
    )
    value_classes = []
    for row in read_csv_rows(os.path.join(data_dir, "value_class_rules.csv")):
        value_classes.append(
            {
                "value_class": row["value_class"].strip(),
                "min_value": as_int(row["min_value"], "min_value") * 100,
                "unschedulable_penalty": as_int(
                    row["unschedulable_penalty"], "unschedulable_penalty"
                ),
                "contention_penalty": as_int(
                    row["contention_penalty"], "contention_penalty"
                ),
            }
        )
    value_classes.sort(
        key=lambda c: (-c["min_value"], natural_key(c["value_class"]))
    )
    dep_rules = []
    for row in read_csv_rows(os.path.join(data_dir, "dependency_rules.csv")):
        dep_rules.append(
            {
                "from": row["from_object"].strip(),
                "to": row["to_object"].strip(),
                "weight": as_int(row["edge_priority"], "edge_priority"),
            }
        )
    with open(os.path.join(data_dir, "horizon_config.json"), encoding="utf-8") as fh:
        horizon = json.load(fh)
    with open(os.path.join(data_dir, "objective_order.json"), encoding="utf-8") as fh:
        objective_cfg = json.load(fh)
    return {
        "materials": materials,
        "handling": handling,
        "bays": bays,
        "exceptions": exceptions,
        "crates": crates,
        "reservations": reservations,
        "prior": prior,
        "conservation": conservation,
        "venue_windows": venue_windows,
        "crew": crew,
        "transport": transport,
        "lenders": lenders,
        "value_classes": value_classes,
        "dep_rules": dep_rules,
        "horizon": horizon,
        "objective_cfg": objective_cfg,
    }


# ---------------------------------------------------------------- stages 2-7


def orientation_footprint(obj, index):
    """Footprint width, footprint depth, vertical extent of an orientation."""
    if index == 0:
        return obj["clearance_w"], obj["clearance_d"], obj["clearance_h"]
    if index == 1:
        return obj["clearance_d"], obj["clearance_w"], obj["clearance_h"]
    if index == 2:
        return obj["clearance_w"], obj["clearance_h"], obj["clearance_d"]
    return obj["clearance_h"], obj["clearance_w"], obj["clearance_d"]


def stage2_geometry(objects, rules):
    for obj in objects:
        hclass = rules["materials"][obj["material_code"]]["handling_class"]
        hrule = rules["handling"][hclass]
        obj["handling_class"] = hclass
        obj["clearance_w"] = obj["width_um"] + 2 * hrule["clearance_w"]
        obj["clearance_d"] = obj["depth_um"] + 2 * hrule["clearance_d"]
        obj["clearance_h"] = obj["height_um"] + hrule["clearance_h"]
        obj["handle_dims"] = sorted(
            [
                obj["width_um"] + 2 * hrule["handle_margin"],
                obj["height_um"] + 2 * hrule["handle_margin"],
                obj["depth_um"] + 2 * hrule["handle_margin"],
            ]
        )
        obj["handling_volume"] = (
            (obj["width_um"] + 2 * hrule["handle_margin"])
            * (obj["height_um"] + 2 * hrule["handle_margin"])
            * (obj["depth_um"] + 2 * hrule["handle_margin"])
        )
        obj["aspect_num"], obj["aspect_den"] = reduced_pair(
            obj["height_um"], obj["width_um"]
        )
        obj["crew_class"] = hrule["crew_class"]
        obj["setup_base"] = hrule["setup_base"]
        obj["sensitivity"] = rules["class_rank"][
            rules["materials"][obj["material_code"]]["conservation_class"]
        ]


def stage3_admissibility(objects):
    for obj in objects:
        allowed = [0]
        if obj["rotate_allowed"]:
            allowed.append(1)
        if obj["tip_allowed"]:
            allowed.extend([2, 3])
        obj["orientations"] = allowed


def stage4_bay_candidates(objects, rules):
    for obj in objects:
        candidates = {}
        for bay_id in sorted(rules["bays"]):
            bay = rules["bays"][bay_id]
            chosen = None
            for index in obj["orientations"]:
                fw, fd, fh = orientation_footprint(obj, index)
                if fw <= bay["interior_w"] and fd < bay["interior_d"] and fh <= bay["interior_h"]:
                    chosen = index
                    break
            candidates[bay_id] = chosen
        obj["bay_candidates"] = candidates


def stage5_placement(objects, rules):
    scan = sorted(
        rules["bays"].values(), key=lambda b: (b["bay_priority"], b["bay_id"])
    )
    for obj in objects:
        bay_id = None
        orientation = None
        for bay in scan:
            index = obj["bay_candidates"][bay["bay_id"]]
            if index is not None:
                bay_id = bay["bay_id"]
                orientation = index
                break
        obj["selected_bay"] = bay_id
        obj["selected_orientation"] = orientation
        if bay_id is not None:
            obj["selected_code"] = P_OK
        elif len(obj["orientations"]) == 1:
            obj["selected_code"] = P_NO_ORIENTATION
        else:
            obj["selected_code"] = P_NO_FIT


def conservation_ok(obj, bay, rules):
    material = rules["materials"][obj["material_code"]]
    if bay["lux_level"] > material["max_lux"]:
        return False
    if material["conservation_class"] in rules["exceptions"].get(bay["bay_id"], []):
        return False
    if (
        rules["conservation"][material["conservation_class"]]["needs_dark"]
        and bay["lux_level"] > 0
    ):
        return False
    return True


def stage6_conservation(objects, rules):
    scan = sorted(
        rules["bays"].values(), key=lambda b: (b["bay_priority"], b["bay_id"])
    )
    for obj in objects:
        bay_id = None
        orientation = None
        code = obj["selected_code"]
        if obj["selected_bay"] is not None:
            for bay in scan:
                index = obj["bay_candidates"][bay["bay_id"]]
                if index is None or not conservation_ok(obj, bay, rules):
                    continue
                bay_id = bay["bay_id"]
                orientation = index
                code = P_OK
                break
            else:
                code = P_CONSERVATION_BLOCK
        obj["bay_id"] = bay_id
        obj["orientation"] = orientation
        obj["conservation_code"] = code


def stage7_crate_candidates(objects, rules):
    for obj in objects:
        candidates = []
        for crate_class in sorted(rules["crates"]):
            crate = rules["crates"][crate_class]
            if all(
                a <= b for a, b in zip(obj["handle_dims"], crate["sorted_dims"])
            ):
                candidates.append(
                    {
                        "crate_class": crate_class,
                        "residual": crate["volume"] - obj["handling_volume"],
                    }
                )
        candidates.sort(key=lambda c: (c["residual"], c["crate_class"]))
        obj["crate_candidates"] = candidates
        obj["primary_crate"] = candidates[0]["crate_class"] if candidates else ""


def compute_setup_seconds(objects, rules):
    quantum = as_int(rules["horizon"].get("setup_div", 700), "setup_div")
    for obj in objects:
        # A zero quantum is the zero denominator sentinel: 0/1 rounds to 0.
        if quantum == 0:
            obj["setup_seconds"] = 0
            continue
        obj["setup_seconds"] = round_half_up(
            obj["setup_base"] * obj["mass_g"], quantum
        )


# ---------------------------------------------------------------- stages 10-12


def build_dependency_edges(objects, rules):
    known = {obj["object_id"] for obj in objects}
    edges = {}

    def add(src, dst, kind, weight):
        held = edges.get((src, dst))
        if held is not None and KIND_RANK[held["kind"]] >= KIND_RANK[kind]:
            return
        edges[(src, dst)] = {"from": src, "to": dst, "kind": kind, "weight": weight}

    for rule in rules["dep_rules"]:
        if rule["from"] in known and rule["to"] in known:
            add(rule["from"], rule["to"], D_RULE, rule["weight"])

    by_bay = {}
    for obj in objects:
        if obj["bay_id"] is not None:
            by_bay.setdefault(obj["bay_id"], []).append(obj)
    for bay_id in sorted(by_bay):
        members = sorted(by_bay[bay_id], key=lambda o: o["object_id"])
        for first in members:
            for second in members:
                if first is second:
                    continue
                if first["handling_volume"] > second["handling_volume"]:
                    add(
                        first["object_id"],
                        second["object_id"],
                        D_BAYSIZE,
                        first["handling_volume"],
                    )
                if first["sensitivity"] > second["sensitivity"]:
                    add(
                        first["object_id"],
                        second["object_id"],
                        D_CONSERVATION,
                        first["handling_volume"],
                    )
    return [edges[key] for key in sorted(edges)]


def find_cycle(pairs):
    """Return one directed cycle as a list of (from, to) pairs, or None.

    Iterative so the graph size is never bound by the recursion limit. The
    cycle found is the lexicographically first reachable one, which keeps the
    removal sequence reproducible.
    """
    adjacency = {}
    for src, dst in pairs:
        adjacency.setdefault(src, set()).add(dst)
    color = {}

    for root in sorted(adjacency):
        if color.get(root, 0) != 0:
            continue
        path = []
        iterators = []
        node = root
        color[node] = 1
        path.append(node)
        iterators.append(iter(sorted(adjacency.get(node, ()))))
        while iterators:
            try:
                nxt = next(iterators[-1])
            except StopIteration:
                iterators.pop()
                color[path.pop()] = 2
                continue
            state = color.get(nxt, 0)
            if state == 0:
                color[nxt] = 1
                path.append(nxt)
                iterators.append(iter(sorted(adjacency.get(nxt, ()))))
            elif state == 1:
                start = path.index(nxt)
                walk = path[start:] + [nxt]
                return [(walk[k], walk[k + 1]) for k in range(len(walk) - 1)]
    return None


def count_cycles(pairs):
    """Strongly connected components with more than one node, plus self loops."""
    adjacency = {}
    for src, dst in pairs:
        adjacency.setdefault(src, set()).add(dst)
    index = {}
    low = {}
    on_stack = {}
    stack = []
    counter = [0]

    for root in sorted(adjacency):
        if root in index:
            continue
        work = [(root, iter(sorted(adjacency.get(root, ()))))]
        index[root] = low[root] = counter[0]
        counter[0] += 1
        stack.append(root)
        on_stack[root] = True
        while work:
            node, it = work[-1]
            advanced = False
            for nxt in it:
                if nxt not in index:
                    index[nxt] = low[nxt] = counter[0]
                    counter[0] += 1
                    stack.append(nxt)
                    on_stack[nxt] = True
                    work.append((nxt, iter(sorted(adjacency.get(nxt, ())))))
                    advanced = True
                    break
                if on_stack.get(nxt):
                    low[node] = min(low[node], index[nxt])
            if advanced:
                continue
            work.pop()
            if low[node] == index[node]:
                while True:
                    popped = stack.pop()
                    on_stack[popped] = False
                    if popped == node:
                        break
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])

    groups = {}
    for node in adjacency:
        groups.setdefault(low[node], []).append(node)
    cyclic = 0
    for group in groups.values():
        if len(group) > 1 or (group[0], group[0]) in adjacency:
            cyclic += 1
    return cyclic


def resolve_cycles(edges):
    """Drop the lowest priority edge of a cycle until the graph is acyclic."""
    kept = {(e["from"], e["to"]): dict(e) for e in edges}
    removed = []
    limit = len(edges) * len(edges) + len(edges) + 16
    steps = 0
    while True:
        cycle = find_cycle(list(kept))
        if cycle is None:
            return [kept[key] for key in sorted(kept)], removed, True, 0
        steps += 1
        if steps > limit:
            return (
                [kept[key] for key in sorted(kept)],
                removed,
                False,
                count_cycles(list(kept)),
            )
        best = None
        for pair in cycle:
            edge = kept[pair]
            key = (
                KIND_PRIORITY.get(edge["kind"], 0),
                edge["weight"],
                edge["from"],
                edge["to"],
            )
            if best is None or key < best[0]:
                best = (key, pair)
        removed.append(kept.pop(best[1]))


def stage12_install_order(objects, edges):
    by_id = {obj["object_id"]: obj for obj in objects}
    predecessors = {}
    for edge in edges:
        predecessors.setdefault(edge["to"], set()).add(edge["from"])
    remaining = set(by_id)
    order = []
    while remaining:
        ready = [
            oid for oid in remaining if not (predecessors.get(oid, set()) & remaining)
        ]
        if not ready:
            ready = remaining
        chosen = min(ready, key=lambda oid: (by_id[oid]["setup_seconds"], oid))
        order.append(chosen)
        remaining.discard(chosen)
    for position, oid in enumerate(order, start=1):
        by_id[oid]["install_position"] = position
    return order


# ---------------------------------------------------------------- stages 8-9


def stage8_crate_assignment(objects, install_order, rules):
    remaining = {}
    for crate_class, crate in rules["crates"].items():
        remaining[crate_class] = crate["count"] - rules["prior"].get(crate_class, 0)
    reservations = dict(rules["reservations"])
    by_id = {obj["object_id"]: obj for obj in objects}
    for oid in install_order:
        obj = by_id[oid]
        obj["crate_class"] = None
        obj["crate_code"] = None
        obj["displaced"] = False
        if obj["bay_id"] is None:
            continue
        available = [
            cand
            for cand in obj["crate_candidates"]
            if remaining[cand["crate_class"]] > 0
        ]
        if not available:
            obj["crate_code"] = C_NO_CRATE
            continue
        best = None
        for cand in available:
            reserved_here = (
                0
                if reservations.get((cand["crate_class"], obj["lender_id"]), 0) > 0
                else 1
            )
            key = (reserved_here, cand["residual"], cand["crate_class"])
            if best is None or key < best[0]:
                best = (key, cand)
        chosen = best[1]
        crate_class = chosen["crate_class"]
        obj["crate_class"] = crate_class
        obj["crate_code"] = (
            C_PRIMARY if crate_class == obj["primary_crate"] else C_DISPLACED
        )
        obj["displaced"] = obj["crate_code"] == C_DISPLACED
        remaining[crate_class] -= 1
        reservation_key = (crate_class, obj["lender_id"])
        if reservations.get(reservation_key, 0) > 0:
            reservations[reservation_key] -= 1
    return remaining


def stage9_transport(objects, rules):
    for obj in objects:
        obj["mode"] = ""
        obj["transport_exception"] = False
        if obj["crate_class"] is None:
            continue
        mode = rules["crates"][obj["crate_class"]]["transport_mode"]
        rule = rules["transport"].get(obj["lender_id"])
        if rule is not None and rule[0] == mode:
            mode = rule[1]
            obj["transport_exception"] = True
        obj["mode"] = mode


# ---------------------------------------------------------------- stage 13


def stage13_windows(objects, rules):
    for obj in objects:
        required = (
            -(-obj["setup_seconds"] // SECONDS_PER_DAY)
            if obj["setup_seconds"] > 0
            else 0
        )
        obj["required_days"] = required
        obj["window_start"] = 0
        obj["window_end"] = -1
        obj["schedule_code"] = S_NO_PLACEMENT
        if obj["bay_id"] is None:
            continue
        conservation = rules["conservation"].get(
            rules["materials"][obj["material_code"]]["conservation_class"]
        )
        access = rules["venue_windows"].get(obj["bay_id"])
        crew_window = rules["crew"].get(obj["crew_class"])
        if conservation is not None and access is not None and crew_window is not None:
            low = max(
                conservation["setup_start"], access[0], crew_window[0]
            )
            high = min(conservation["setup_end"], access[1] - 1, crew_window[1])
            obj["window_start"] = low
            obj["window_end"] = high
            days_available = high - low + 1
            if days_available <= 0:
                obj["schedule_code"] = S_NO_WINDOW
            elif required > days_available:
                obj["schedule_code"] = S_SHORT_WINDOW
            else:
                obj["schedule_code"] = S_OK
        if obj["crate_class"] is None and obj["schedule_code"] in (
            S_OK,
            S_NO_WINDOW,
            S_SHORT_WINDOW,
        ):
            obj["schedule_code"] = S_NO_CRATE


# ---------------------------------------------------------------- stage 14


def value_class_of(obj, rules):
    for entry in rules["value_classes"]:
        if obj["declared_value_cents"] >= entry["min_value"]:
            return entry
    return rules["value_classes"][-1] if rules["value_classes"] else None


def insurance_cents(obj, rules):
    lender = rules["lenders"].get(obj["lender_id"])
    if lender is None or lender["den"] == 0:
        return 0
    return round_half_up(obj["declared_value_cents"] * lender["num"], lender["den"])


def jsonl_bytes(records):
    out = bytearray()
    for record in records:
        out += json.dumps(record, separators=(",", ":"), sort_keys=True).encode("utf-8")
        out += b"\n"
    return bytes(out)


def json_bytes(value):
    return json.dumps(value, indent=2, sort_keys=True).encode("utf-8") + b"\n"


def main(argv):
    if len(argv) != 3:
        sys.stderr.write("usage: plan.py <DATA_DIR> <OUT_DIR>\n")
        return 2
    data_dir = os.path.abspath(argv[1])
    out_dir = os.path.abspath(argv[2])

    rules = load_rules(data_dir)
    rules["class_rank"] = {
        code: rank for rank, code in enumerate(sorted(rules["conservation"], key=natural_key))
    }
    objects = load_objects(data_dir)

    stage2_geometry(objects, rules)
    stage3_admissibility(objects)
    stage4_bay_candidates(objects, rules)
    stage5_placement(objects, rules)
    stage6_conservation(objects, rules)
    stage7_crate_candidates(objects, rules)
    compute_setup_seconds(objects, rules)
    dep_edges = build_dependency_edges(objects, rules)
    kept_edges, removed_edges, converged, cycles_left = resolve_cycles(dep_edges)
    install_order = stage12_install_order(objects, kept_edges)
    crate_remaining = stage8_crate_assignment(objects, install_order, rules)
    stage9_transport(objects, rules)
    stage13_windows(objects, rules)

    by_id = {obj["object_id"]: obj for obj in objects}

    total_residual = 0
    for obj in objects:
        if obj["crate_class"] is None:
            continue
        for cand in obj["crate_candidates"]:
            if cand["crate_class"] == obj["crate_class"]:
                total_residual += cand["residual"]
                break
    total_setup_seconds = sum(obj["setup_seconds"] for obj in objects)
    total_insurance = sum(insurance_cents(obj, rules) for obj in objects)
    placed_objects = sum(1 for obj in objects if obj["schedule_code"] == S_OK)

    unschedulable_penalty = 0
    contention_penalty = 0
    unschedulable_count = 0
    displacement_count = 0
    for obj in objects:
        entry = value_class_of(obj, rules)
        if entry is None:
            continue
        if obj["schedule_code"] != S_OK:
            unschedulable_count += 1
            unschedulable_penalty += entry["unschedulable_penalty"]
        if obj["displaced"]:
            displacement_count += 1
            contention_penalty += entry["contention_penalty"]

    objective_names = list(rules["objective_cfg"].get("order") or [])
    value_by_name = {
        "placed_objects": placed_objects,
        "unschedulable_penalty": unschedulable_penalty,
        "contention_penalty": contention_penalty,
        "cycles_broken": len(removed_edges),
        "total_residual": total_residual,
    }
    objective_values = [value_by_name[name] for name in objective_names]

    artifacts = {}

    artifacts["canonical_objects.jsonl"] = jsonl_bytes(
        [
            {
                "object_id": obj["object_id"],
                "lender_id": obj["lender_id"],
                "material_code": obj["material_code"],
                "version": obj["version"],
                "declared_value_cents": obj["declared_value_cents"],
                "mass_g": obj["mass_g"],
            }
            for obj in objects
        ]
    )
    artifacts["normalized_geometry.jsonl"] = jsonl_bytes(
        [
            {
                "object_id": obj["object_id"],
                "clearance_w": obj["clearance_w"],
                "clearance_h": obj["clearance_h"],
                "clearance_d": obj["clearance_d"],
                "aspect_num": obj["aspect_num"],
                "aspect_den": obj["aspect_den"],
            }
            for obj in objects
        ]
    )
    artifacts["admissibility_matrix.jsonl"] = jsonl_bytes(
        [
            {
                "object_id": obj["object_id"],
                "orientations": obj["orientations"],
                "tip_allowed": 1 if obj["tip_allowed"] else 0,
                "rotate_allowed": 1 if obj["rotate_allowed"] else 0,
            }
            for obj in objects
        ]
    )
    artifacts["bay_candidate_matrix.jsonl"] = jsonl_bytes(
        [
            {
                "object_id": obj["object_id"],
                "bay_id": bay_id,
                "fits": 1 if obj["bay_candidates"][bay_id] is not None else 0,
                "orientation": (
                    obj["bay_candidates"][bay_id]
                    if obj["bay_candidates"][bay_id] is not None
                    else -1
                ),
            }
            for obj in objects
            for bay_id in sorted(obj["bay_candidates"])
        ]
    )
    artifacts["selected_placement.jsonl"] = jsonl_bytes(
        [
            {
                "object_id": obj["object_id"],
                "bay_id": obj["selected_bay"] or "",
                "orientation": (
                    obj["selected_orientation"]
                    if obj["selected_orientation"] is not None
                    else -1
                ),
                "code": obj["selected_code"],
            }
            for obj in objects
        ]
    )
    rejected_rows = sorted(
        [
            {
                "object_id": obj["object_id"],
                "code": (
                    obj["conservation_code"]
                    if obj["conservation_code"]
                    in (P_NO_FIT, P_NO_ORIENTATION, P_CONSERVATION_BLOCK)
                    else P_NO_FIT
                ),
            }
            for obj in objects
            if obj["conservation_code"] != P_OK
        ],
        key=lambda r: (r["object_id"], r["code"]),
    )
    artifacts["rejected_placement.jsonl"] = jsonl_bytes(rejected_rows)
    artifacts["conservation_adjusted.jsonl"] = jsonl_bytes(
        [
            {
                "object_id": obj["object_id"],
                "bay_id": obj["bay_id"] or "",
                "orientation": obj["orientation"] if obj["orientation"] is not None else -1,
                "code": obj["conservation_code"],
            }
            for obj in objects
        ]
    )
    artifacts["crate_candidate_matrix.jsonl"] = jsonl_bytes(
        [
            {
                "object_id": obj["object_id"],
                "crate_class": cand["crate_class"],
                "residual": cand["residual"],
            }
            for obj in sorted(objects, key=lambda o: o["object_id"])
            for cand in obj["crate_candidates"]
        ]
    )
    assignment_rows = []
    for obj in sorted(objects, key=lambda o: o["object_id"]):
        if obj["bay_id"] is None:
            continue
        assignment_rows.append(
            {
                "object_id": obj["object_id"],
                "crate_class": obj["crate_class"] or "",
                "code": obj["crate_code"] or C_NO_CRATE,
                "primary_class": obj["primary_crate"],
            }
        )
    artifacts["crate_assignment.jsonl"] = jsonl_bytes(assignment_rows)
    artifacts["crate_capacity_ledger.jsonl"] = jsonl_bytes(
        [
            {
                "crate_class": crate_class,
                "catalog_count": rules["crates"][crate_class]["count"],
                "prior_consumed": rules["prior"].get(crate_class, 0),
                "remaining": crate_remaining.get(crate_class, 0),
            }
            for crate_class in sorted(rules["crates"])
        ]
    )
    artifacts["crate_contention_report.jsonl"] = jsonl_bytes(
        [
            {
                "object_id": obj["object_id"],
                "wanted_class": obj["primary_crate"],
                "assigned_class": obj["crate_class"],
            }
            for obj in sorted(objects, key=lambda o: o["object_id"])
            if obj["displaced"]
        ]
    )
    artifacts["transport_leg_manifest.jsonl"] = jsonl_bytes(
        [
            {
                "object_id": obj["object_id"],
                "crate_class": obj["crate_class"],
                "mode": obj["mode"],
                "exception": 1 if obj["transport_exception"] else 0,
            }
            for obj in objects
            if obj["crate_class"] is not None
        ]
    )
    artifacts["dependency_edges.jsonl"] = jsonl_bytes(dep_edges)
    artifacts["cycle_resolution_audit.jsonl"] = jsonl_bytes(removed_edges)
    artifacts["install_order.jsonl"] = jsonl_bytes(
        [
            {"position": position, "object_id": oid}
            for position, oid in enumerate(install_order, start=1)
        ]
    )
    artifacts["crew_requirement_ledger.jsonl"] = jsonl_bytes(
        [
            {
                "object_id": obj["object_id"],
                "crew_class": obj["crew_class"],
                "setup_seconds": obj["setup_seconds"],
            }
            for obj in objects
        ]
    )
    artifacts["install_window_table.jsonl"] = jsonl_bytes(
        [
            {
                "object_id": obj["object_id"],
                "bay_id": obj["bay_id"] or "",
                "window_start": obj["window_start"],
                "window_end": obj["window_end"],
                "required_days": obj["required_days"],
                "code": obj["schedule_code"],
            }
            for obj in objects
        ]
    )
    artifacts["unschedulable_report.jsonl"] = jsonl_bytes(
        sorted(
            [
                {
                    "object_id": obj["object_id"],
                    "code": obj["schedule_code"],
                }
                for obj in objects
                if obj["schedule_code"] != S_OK
            ],
            key=lambda r: (r["object_id"], r["code"]),
        )
    )
    occupancy = {}
    for obj in objects:
        if obj["bay_id"] is not None:
            occupancy.setdefault(obj["bay_id"], []).append(obj["object_id"])
    artifacts["gallery_occupancy_map.jsonl"] = jsonl_bytes(
        [
            {
                "bay_id": bay_id,
                "object_count": len(occupancy.get(bay_id, ())),
                "objects": sorted(occupancy.get(bay_id, ())),
            }
            for bay_id in sorted(rules["bays"])
        ]
    )
    artifacts["deterministic_audit.jsonl"] = jsonl_bytes(
        [
            {
                "object_id": obj["object_id"],
                "placed_bay": obj["bay_id"] or "",
                "crate_class": obj["crate_class"] or "",
                "displaced": 1 if obj["displaced"] else 0,
                "scheduled": 1 if obj["schedule_code"] == S_OK else 0,
                "install_position": obj["install_position"],
                "setup_seconds": obj["setup_seconds"],
                "insurance_cents": insurance_cents(obj, rules),
            }
            for obj in objects
        ]
    )
    artifacts["penalty_summary.json"] = json_bytes(
        {
            "unschedulable_penalty": unschedulable_penalty,
            "contention_penalty": contention_penalty,
            "cycles_broken": len(removed_edges),
            "total_insurance_cents": total_insurance,
        }
    )
    artifacts["objective_tuple.json"] = json_bytes(
        {"objective": objective_values, "order": objective_names}
    )

    os.makedirs(out_dir, exist_ok=True)
    for name in sorted(artifacts):
        with open(os.path.join(out_dir, name), "wb") as handle:
            handle.write(artifacts[name])

    manifest = {}
    for name in sorted(artifacts):
        manifest[name] = hashlib.sha256(artifacts[name]).hexdigest()
    program_path = os.path.abspath(__file__)
    if os.path.exists(program_path):
        with open(program_path, "rb") as handle:
            program_bytes = handle.read()
        manifest[os.path.basename(program_path)] = hashlib.sha256(
            program_bytes
        ).hexdigest()

    chain = str(rules["horizon"].get("chain_seed", "0" * 64))
    for name in sorted(manifest):
        chain = hashlib.sha256((chain + manifest[name]).encode("utf-8")).hexdigest()

    with open(os.path.join(out_dir, "integrity_manifest.json"), "wb") as handle:
        handle.write(json_bytes(manifest))
    with open(
        os.path.join(out_dir, "final_state.json"), "wb"
    ) as handle:
        handle.write(
            json_bytes(
                {
                    "chain": chain,
                    "repair_converged": 1 if converged else 0,
                    "cycle_edges_removed": len(removed_edges),
                    "final_cycle_count": cycles_left,
                    "displacement_count": displacement_count,
                    "unschedulable_count": unschedulable_count,
                    "placed_objects": placed_objects,
                    "total_setup_seconds": total_setup_seconds,
                    "total_insurance_cents": total_insurance,
                    "objective": objective_values,
                }
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
