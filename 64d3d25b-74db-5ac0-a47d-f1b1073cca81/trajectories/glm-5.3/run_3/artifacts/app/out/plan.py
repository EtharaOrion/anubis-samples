#!/usr/bin/env python3
"""Installation dossier planner.

Rebuilds the travelling exhibition installation dossier from an administrative
snapshot:  python3 plan.py <DATA_DIR> <OUT_DIR>

Implements the staged derivation defined by rules.md / schema.md / constants.md /
reason_codes.md in the data directory, using integer and exact rational
arithmetic only.  The data directory path is taken from the command line, never
pinned, and identical inputs produce identical output bytes.
"""

import csv
import hashlib
import json
import os
import sys
from math import gcd

MM_UM = 1000
CM_UM = 10000
IN_UM = 25400
LENGTH_FACTORS = {"MM": MM_UM, "CM": CM_UM, "IN": IN_UM}
MASS_FACTORS = {"G": 1, "KG": 1000}
SECONDS_PER_DAY = 3600

PLACEMENT_CODE_OK = "P_OK"
CRATE_CODE_PRIMARY = "C_PRIMARY"
SCHEDULE_OK = "S_OK"

KIND_EXPLICIT = "D_RULE"
KIND_CONSERVATION = "D_CONSERVATION"
KIND_BAYSIZE = "D_BAYSIZE"
# removal precedence: bay size edges go first, explicit rules last
KIND_REMOVAL_RANK = {KIND_BAYSIZE: 0, KIND_CONSERVATION: 1, KIND_EXPLICIT: 2}
KIND_PRECEDENCE_RANK = {KIND_EXPLICIT: 2, KIND_CONSERVATION: 1, KIND_BAYSIZE: 0}

HASHED_ARTIFACTS = [
    "admissibility_matrix.jsonl",
    "bay_candidate_matrix.jsonl",
    "canonical_objects.jsonl",
    "conservation_adjusted.jsonl",
    "crate_assignment.jsonl",
    "crate_candidate_matrix.jsonl",
    "crate_capacity_ledger.jsonl",
    "crate_contention_report.jsonl",
    "crew_requirement_ledger.jsonl",
    "cycle_resolution_audit.jsonl",
    "dependency_edges.jsonl",
    "deterministic_audit.jsonl",
    "gallery_occupancy_map.jsonl",
    "install_order.jsonl",
    "install_window_table.jsonl",
    "normalized_geometry.jsonl",
    "objective_tuple.json",
    "penalty_summary.json",
    "rejected_placement.jsonl",
    "selected_placement.jsonl",
    "transport_leg_manifest.jsonl",
    "unschedulable_report.jsonl",
]
PROGRAM_NAME = "plan.py"


# ---------------------------------------------------------------- input loading

def read_csv_rows(path):
    """Rows of a shipped csv as {column: string}, or [] when absent."""
    if not os.path.exists(path):
        return []
    with open(path, "r", newline="", encoding="utf-8") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def read_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def first_int(value):
    return int(str(value).strip())


class Snapshot(object):
    """Every shipped record, parsed and keyed for lookups."""

    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.horizon = read_json(os.path.join(data_dir, "horizon_config.json"))
        self.objective_spec = read_json(os.path.join(data_dir, "objective_order.json"))
        self.object_order = []
        self.objects = {}
        for row in read_csv_rows(os.path.join(data_dir, "object_master.csv")):
            oid = row["object_id"].strip()
            if oid in self.objects:
                continue  # stale duplicate row: first occurrence wins
            self.objects[oid] = {
                "object_id": oid,
                "lender_id": row["lender_id"].strip(),
                "material_code": row["material_code"].strip(),
            }
            self.object_order.append(oid)
        self.dimensions = {
            row["object_id"].strip(): row
            for row in read_csv_rows(os.path.join(data_dir, "object_dimensions.csv"))
        }
        self.masses = {
            row["object_id"].strip(): row
            for row in read_csv_rows(os.path.join(data_dir, "object_mass.csv"))
        }
        self.values = {
            row["object_id"].strip(): row
            for row in read_csv_rows(os.path.join(data_dir, "object_value.csv"))
        }
        self.orientations = {
            row["object_id"].strip(): row
            for row in read_csv_rows(os.path.join(data_dir, "object_orientation.csv"))
        }
        versions = {}
        for row in read_csv_rows(os.path.join(data_dir, "object_version_map.csv")):
            oid = row["object_id"].strip()
            version = first_int(row["version"])
            if version > versions.get(oid, version - 1):
                versions[oid] = version
        self.versions = versions
        self.materials = {
            row["material_code"].strip(): row
            for row in read_csv_rows(os.path.join(data_dir, "material_rules.csv"))
        }
        self.handling = {
            row["handling_class"].strip(): row
            for row in read_csv_rows(os.path.join(data_dir, "handling_class_rules.csv"))
        }
        self.bays = {}
        self.bay_order = []
        for row in read_csv_rows(os.path.join(data_dir, "gallery_bays.csv")):
            bay_id = row["bay_id"].strip()
            if bay_id not in self.bays:
                self.bays[bay_id] = row
                self.bay_order.append(bay_id)
        self.bay_exceptions = {}
        for row in read_csv_rows(os.path.join(data_dir, "bay_exceptions.csv")):
            self.bay_exceptions.setdefault(row["bay_id"].strip(), set()).add(
                row["blocked_class"].strip())
        self.crates = {}
        self.crate_order = []
        for row in read_csv_rows(os.path.join(data_dir, "crate_catalog.csv")):
            crate_class = row["crate_class"].strip()
            if crate_class not in self.crates:
                self.crates[crate_class] = row
                self.crate_order.append(crate_class)
        self.reservations = {}
        for row in read_csv_rows(os.path.join(data_dir, "crate_reservations.csv")):
            key = (row["crate_class"].strip(), row["lender_id"].strip())
            self.reservations[key] = self.reservations.get(key, 0) + first_int(
                row["reserved_count"])
        self.prior_consumed = {}
        for row in read_csv_rows(os.path.join(data_dir, "prior_state.csv")):
            self.prior_consumed[row["crate_class"].strip()] = first_int(
                row["consumed_count"])
        self.conservation = {
            row["conservation_class"].strip(): row
            for row in read_csv_rows(os.path.join(data_dir, "conservation_rules.csv"))
        }
        self.conservation_order = [row["conservation_class"].strip()
                                   for row in read_csv_rows(
                                       os.path.join(data_dir, "conservation_rules.csv"))]
        self.venue_windows = {
            row["bay_id"].strip(): row
            for row in read_csv_rows(os.path.join(data_dir, "venue_windows.csv"))
        }
        self.crew = {
            row["crew_class"].strip(): row
            for row in read_csv_rows(os.path.join(data_dir, "crew_roster.csv"))
        }
        self.transport_rules = {
            (row["lender_id"].strip(), row["forbidden_mode"].strip()):
                row["fallback_mode"].strip()
            for row in read_csv_rows(os.path.join(data_dir, "transport_rules.csv"))
        }
        self.lenders = {
            row["lender_id"].strip(): row
            for row in read_csv_rows(os.path.join(data_dir, "lender_master.csv"))
        }
        value_rows = read_csv_rows(os.path.join(data_dir, "value_class_rules.csv"))
        self.value_classes = [{
            "value_class": row["value_class"].strip(),
            "min_value": int(row["min_value"].strip()),
            "unschedulable_penalty": int(row["unschedulable_penalty"].strip()),
            "contention_penalty": int(row["contention_penalty"].strip()),
        } for row in value_rows]
        # The highest class whose minimum value an object meets outranks the rest.
        self.value_classes.sort(key=lambda item: (-item["min_value"], item["value_class"]))
        self.dependencies = read_csv_rows(os.path.join(data_dir, "dependency_rules.csv"))


# ------------------------------------------------------------------- arithmetic

def round_half_up(numerator, denominator):
    """Round the exact rational numerator/denominator half up to an integer."""
    if denominator == 0:
        return 0
    if denominator < 0:
        numerator, denominator = -numerator, -denominator
    quotient, remainder = divmod(numerator, denominator)
    if 2 * remainder >= denominator:
        quotient += 1
    return quotient


def reduced_pair(numerator, denominator):
    """A ratio carried as a reduced pair of integers; zero denominator is 0/1."""
    if denominator == 0 or numerator == 0:
        return 0, 1
    divisor = gcd(abs(numerator), abs(denominator))
    numerator //= divisor
    denominator //= divisor
    if denominator < 0:
        numerator, denominator = -numerator, -denominator
    return numerator, denominator


def to_micrometres(text, unit_code):
    return first_int(text) * LENGTH_FACTORS[unit_code.strip()]


def to_grams(text, unit_code):
    return first_int(text) * MASS_FACTORS[unit_code.strip()]


# ------------------------------------------------------------------ serialization

def write_jsonl(path, records):
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")))
            handle.write("\n")


def write_json(path, value):
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(value, sort_keys=True, indent=2))
        handle.write("\n")


# ------------------------------------------------------------------- the planner

class Planner(object):

    def __init__(self, snapshot):
        self.s = snapshot
        self._volume_cache = {}
        self.run()

    # stage 1
    def canonicalize(self):
        s = self.s
        self.canonical = {}
        for oid in s.object_order:
            master = s.objects[oid]
            dims = s.dimensions[oid]
            record = {
                "object_id": oid,
                "lender_id": master["lender_id"],
                "material_code": master["material_code"],
                "version": s.versions.get(oid, 0),
                "declared_value_cents": first_int(s.values[oid]["declared_value"]) * 100,
                "mass_g": to_grams(s.masses[oid]["mass"], s.masses[oid]["mass_unit"]),
                "width": to_micrometres(dims["width"], dims["dim_unit"]),
                "height": to_micrometres(dims["height"], dims["dim_unit"]),
                "depth": to_micrometres(dims["depth"], dims["dim_unit"]),
            }
            material = s.materials[record["material_code"]]
            record["conservation_class"] = material["conservation_class"].strip()
            record["max_lux"] = first_int(material["max_lux"])
            record["handling_class"] = material["handling_class"].strip()
            handling = s.handling[record["handling_class"]]
            record["crew_class"] = handling["crew_class"].strip()
            self.canonical[oid] = record

    # stage 2
    def normalize_geometry(self):
        s = self.s
        self.geometry = {}
        for oid in s.object_order:
            record = self.canonical[oid]
            handling = s.handling[record["handling_class"]]
            margin_w = to_micrometres(handling["clearance_w"], "MM")
            margin_d = to_micrometres(handling["clearance_d"], "MM")
            margin_h = to_micrometres(handling["clearance_h"], "MM")
            record["clearance_w"] = record["width"] + 2 * margin_w
            record["clearance_d"] = record["depth"] + 2 * margin_d
            record["clearance_h"] = record["height"] + margin_h
            aspect_num, aspect_den = reduced_pair(record["height"], record["width"])
            self.geometry[oid] = {
                "object_id": oid,
                "clearance_w": record["clearance_w"],
                "clearance_h": record["clearance_h"],
                "clearance_d": record["clearance_d"],
                "aspect_num": aspect_num,
                "aspect_den": aspect_den,
            }

    # stage 3
    def admissibility(self):
        s = self.s
        self.admissible = {}
        self.admissibility_rows = []
        for oid in s.object_order:
            row = s.orientations[oid]
            tip = first_int(row["tip_allowed"])
            rotate = first_int(row["rotate_allowed"])
            orientations = [0]
            if rotate:
                orientations.append(1)
            if tip:
                orientations.extend([2, 3])
            self.admissible[oid] = orientations
            self.admissibility_rows.append({
                "object_id": oid,
                "orientations": orientations,
                "tip_allowed": tip,
                "rotate_allowed": rotate,
            })

    def orientation_shape(self, oid, index):
        record = self.canonical[oid]
        if index == 0:
            return record["clearance_w"], record["clearance_d"], record["clearance_h"]
        if index == 1:
            return record["clearance_d"], record["clearance_w"], record["clearance_h"]
        if index == 2:
            return record["clearance_w"], record["clearance_h"], record["clearance_d"]
        return record["clearance_h"], record["clearance_w"], record["clearance_d"]

    def fits_bay(self, oid, bay_id, index):
        footprint_w, footprint_d, vertical = self.orientation_shape(oid, index)
        bay = self.s.bays[bay_id]
        interior_w = to_micrometres(bay["interior_w"], "MM")
        interior_h = to_micrometres(bay["interior_h"], "MM")
        interior_d = to_micrometres(bay["interior_d"], "MM")
        return (footprint_w <= interior_w and footprint_d < interior_d
                and vertical <= interior_h)

    # stage 4
    def bay_candidates(self):
        s = self.s
        self.bay_fit = {}
        self.bay_candidate_rows = []
        bay_ids = sorted(s.bays)
        for oid in s.object_order:
            for bay_id in bay_ids:
                fitting = [index for index in self.admissible[oid]
                           if self.fits_bay(oid, bay_id, index)]
                smallest = min(fitting) if fitting else -1
                self.bay_fit[(oid, bay_id)] = (bool(fitting), smallest)
                self.bay_candidate_rows.append({
                    "object_id": oid,
                    "bay_id": bay_id,
                    "fits": 1 if fitting else 0,
                    "orientation": smallest,
                })

    def scan_bays(self):
        """Bays in ascending priority then ascending bay id order."""
        return [bay_id for bay_id in sorted(
            self.s.bays,
            key=lambda bay_id: (first_int(self.s.bays[bay_id]["bay_priority"]), bay_id))]

    # stage 5
    def select_placement(self):
        s = self.s
        self.primary_placement = {}
        self.selected_rows = []
        self.placement_code = {}
        for oid in s.object_order:
            chosen_bay = ""
            chosen_orientation = -1
            for bay_id in self.scan_bays():
                fitting, smallest = self.bay_fit[(oid, bay_id)]
                if fitting:
                    chosen_bay = bay_id
                    chosen_orientation = smallest
                    break
            if chosen_bay:
                self.primary_placement[oid] = {"bay_id": chosen_bay,
                                               "orientation": chosen_orientation}
                code = PLACEMENT_CODE_OK
            else:
                self.primary_placement[oid] = None
                code = "P_NO_ORIENTATION" if self.admissible[oid] == [0] else "P_NO_FIT"
            self.placement_code[oid] = code
            self.selected_rows.append({
                "object_id": oid,
                "bay_id": chosen_bay if chosen_bay else "",
                "orientation": chosen_orientation,
                "code": code,
            })
        self.rejected_rows = [{"object_id": oid, "code": self.placement_code[oid]}
                              for oid in s.object_order
                              if self.primary_placement[oid] is None]
        self.rejected_rows.sort(key=lambda row: (row["object_id"], row["code"]))

    def conservation_ok(self, oid, bay_id):
        s = self.s
        record = self.canonical[oid]
        bay = s.bays[bay_id]
        lux = first_int(bay["lux_level"])
        conservation = s.conservation[record["conservation_class"]]
        if lux > record["max_lux"]:
            return False
        if record["conservation_class"] in s.bay_exceptions.get(bay_id, set()):
            return False
        if first_int(conservation["needs_dark"]) and lux > 0:
            return False
        return True

    # stage 6
    def conservation_adjustment(self):
        s = self.s
        self.conservation_placement = {}
        self.conservation_rows = []
        for oid in s.object_order:
            chosen_bay = ""
            chosen_orientation = -1
            if self.primary_placement[oid] is not None:
                for bay_id in self.scan_bays():
                    fitting, smallest = self.bay_fit[(oid, bay_id)]
                    if fitting and self.conservation_ok(oid, bay_id):
                        chosen_bay = bay_id
                        chosen_orientation = smallest
                        break
            if chosen_bay:
                self.conservation_placement[oid] = {
                    "bay_id": chosen_bay, "orientation": chosen_orientation}
                code = PLACEMENT_CODE_OK
            else:
                self.conservation_placement[oid] = None
                code = (self.placement_code[oid]
                        if self.primary_placement[oid] is None else "P_CONSERVATION_BLOCK")
            self.conservation_rows.append({
                "object_id": oid,
                "bay_id": chosen_bay,
                "orientation": chosen_orientation,
                "code": code,
            })

    # stage 7
    def crate_candidates(self):
        s = self.s
        self.candidates = {}
        self.crate_candidate_rows = []
        for oid in s.object_order:
            envelope = self.handling_envelope(oid)
            entries = []
            for crate_class in s.crate_order:
                crate = s.crates[crate_class]
                interior = sorted([to_micrometres(crate["inner_w"], "MM"),
                                   to_micrometres(crate["inner_h"], "MM"),
                                   to_micrometres(crate["inner_d"], "MM")])
                if all(envelope[axis] <= interior[axis] for axis in range(3)):
                    residual = (interior[0] * interior[1] * interior[2]
                                - envelope[0] * envelope[1] * envelope[2])
                    entries.append({"crate_class": crate_class, "residual": residual})
            entries.sort(key=lambda entry: (entry["residual"], entry["crate_class"]))
            self.candidates[oid] = entries
            for entry in entries:
                self.crate_candidate_rows.append({
                    "object_id": oid, "crate_class": entry["crate_class"],
                    "residual": entry["residual"]})
        self.crate_candidate_rows.sort(
            key=lambda row: (row["object_id"], row["residual"], row["crate_class"]))
        self.primary_crate = {
            oid: (self.candidates[oid][0]["crate_class"] if self.candidates[oid] else "")
            for oid in s.object_order}

    def handling_envelope(self, oid):
        record = self.canonical[oid]
        handling = self.s.handling[record["handling_class"]]
        margin = to_micrometres(handling["handle_margin"], "MM")
        return sorted([record["width"] + 2 * margin,
                       record["height"] + 2 * margin,
                       record["depth"] + 2 * margin])

    # stage 13 crew and setup (needed before install order and crate claiming)
    def crew_setup(self):
        s = self.s
        self.setup_seconds = {}
        self.crew_rows = []
        setup_div = first_int(s.horizon["setup_div"])
        for oid in s.object_order:
            record = self.canonical[oid]
            handling = s.handling[record["handling_class"]]
            seconds = round_half_up(first_int(handling["setup_base"]) * record["mass_g"],
                                    setup_div)
            self.setup_seconds[oid] = seconds
            self.crew_rows.append({
                "object_id": oid, "crew_class": record["crew_class"],
                "setup_seconds": seconds})

    # stage 10
    def dependency_graph(self):
        s = self.s
        edges = {}

        def add_edge(from_id, to_id, kind, weight):
            existing = edges.get((from_id, to_id))
            if existing is None or KIND_PRECEDENCE_RANK[kind] > KIND_PRECEDENCE_RANK[
                    existing[0]]:
                edges[(from_id, to_id)] = (kind, weight)

        for row in s.dependencies:
            add_edge(row["from_object"].strip(), row["to_object"].strip(),
                     KIND_EXPLICIT, first_int(row["edge_priority"]))
        bays = {}
        for oid, placement in self.conservation_placement.items():
            if placement is not None:
                bays.setdefault(placement["bay_id"], []).append(oid)
        sensitivity = {name: rank for rank, name in enumerate(s.conservation_order)}
        for bay_id in sorted(bays):
            members = sorted(bays[bay_id])
            for earlier in members:
                for later in members:
                    if earlier == later:
                        continue
                    earlier_volume = self.handling_volume(earlier)
                    later_volume = self.handling_volume(later)
                    if earlier_volume > later_volume or (
                            earlier_volume == later_volume and earlier < later):
                        add_edge(earlier, later, KIND_BAYSIZE, earlier_volume)
                    # a later conservation class in the rules file is the more
                    # sensitive one and is installed first
                    earlier_rank = sensitivity[self.canonical[earlier]["conservation_class"]]
                    later_rank = sensitivity[self.canonical[later]["conservation_class"]]
                    if earlier_rank > later_rank or (
                            earlier_rank == later_rank and earlier < later):
                        add_edge(earlier, later, KIND_CONSERVATION, earlier_volume)
        self.dependency_rows = [{"from": from_id, "to": to_id,
                                  "kind": kind, "weight": weight}
                                 for (from_id, to_id), (kind, weight) in edges.items()]
        self.dependency_rows.sort(key=lambda row: (row["from"], row["to"]))
        self.graph = {oid: set() for oid in s.object_order}
        for (from_id, to_id) in edges:
            self.graph[from_id].add(to_id)

    def handling_volume(self, oid):
        if oid not in self._volume_cache:
            envelope = self.handling_envelope(oid)
            self._volume_cache[oid] = envelope[0] * envelope[1] * envelope[2]
        return self._volume_cache[oid]

    # stage 11
    def resolve_cycles(self):
        graph = {node: set(targets) for node, targets in self.graph.items()}
        edge_records = {(row["from"], row["to"]): dict(row)
                        for row in self.dependency_rows}

        def find_cycle():
            state = {}
            stack = []

            def visit(node):
                state[node] = 1
                stack.append(node)
                for target in sorted(graph[node]):
                    if state.get(target, 0) == 0:
                        found = visit(target)
                        if found is not None:
                            return found
                    elif state.get(target, 0) == 1:
                        return stack[stack.index(target):] + [target]
                stack.pop()
                state[node] = 2
                return None

            for node in sorted(graph):
                if state.get(node, 0) == 0:
                    found = visit(node)
                    if found is not None:
                        return found
            return None

        removed = []
        while True:
            cycle = find_cycle()
            if cycle is None:
                break
            candidates = []
            for index in range(len(cycle) - 1):
                key = (cycle[index], cycle[index + 1])
                if key in edge_records:
                    candidates.append(key)
            if not candidates:
                break
            chosen = min(candidates, key=lambda key: (
                KIND_REMOVAL_RANK[edge_records[key]["kind"]],
                edge_records[key]["weight"], key[0], key[1]))
            removed.append(edge_records.pop(chosen))
            graph[chosen[0]].discard(chosen[1])
        self.cycle_rows = removed
        self.acyclic_graph = graph

    # stage 12
    def install_order(self):
        graph = self.acyclic_graph
        predecessors = {oid: set() for oid in graph}
        for oid, targets in graph.items():
            for target in targets:
                predecessors[target].add(oid)
        remaining = set(graph)
        order = []
        while remaining:
            ready = [oid for oid in remaining
                     if predecessors[oid] <= (set(graph) - remaining)]
            chosen = min(ready, key=lambda oid: (self.setup_seconds[oid], oid))
            order.append(chosen)
            remaining.discard(chosen)
        self.order = order
        self.position = {oid: index + 1 for index, oid in enumerate(order)}
        self.install_rows = [{"position": index + 1, "object_id": oid}
                             for index, oid in enumerate(order)]

    # stage 8
    def assign_crates(self):
        s = self.s
        remaining = {}
        for crate_class in s.crate_order:
            remaining[crate_class] = (first_int(s.crates[crate_class]["count"])
                                      - s.prior_consumed.get(crate_class, 0))
        reservation_pool = dict(s.reservations)
        self.crate_assignment = {}
        self.displaced = {}
        for oid in self.order:
            if self.conservation_placement[oid] is None:
                continue
            available = [entry for entry in self.candidates[oid]
                         if remaining[entry["crate_class"]] > 0]
            if not available:
                self.crate_assignment[oid] = ""
                self.displaced[oid] = False
                continue
            lender = self.canonical[oid]["lender_id"]

            def choice_key(entry):
                crate_class = entry["crate_class"]
                outstanding = reservation_pool.get((crate_class, lender), 0)
                return (0 if outstanding > 0 else 1, entry["residual"], crate_class)

            chosen = min(available, key=choice_key)
            crate_class = chosen["crate_class"]
            self.crate_assignment[oid] = crate_class
            remaining[crate_class] -= 1
            if reservation_pool.get((crate_class, lender), 0) > 0:
                reservation_pool[(crate_class, lender)] -= 1
            self.displaced[oid] = (crate_class != self.primary_crate[oid])
        self.crate_assignment_rows = []
        self.contention_rows = []
        for oid in s.object_order:
            if oid not in self.crate_assignment:
                continue
            crate_class = self.crate_assignment[oid]
            if not crate_class:
                code = "C_NO_CRATE"
                primary = self.primary_crate[oid]
            elif self.displaced[oid]:
                code = "C_DISPLACED"
                primary = self.primary_crate[oid]
            else:
                code = CRATE_CODE_PRIMARY
                primary = crate_class
            self.crate_assignment_rows.append({
                "object_id": oid, "crate_class": crate_class,
                "code": code, "primary_class": primary})
            if self.displaced[oid]:
                self.contention_rows.append({
                    "object_id": oid, "wanted_class": self.primary_crate[oid],
                    "assigned_class": crate_class})
        self.crate_assignment_rows.sort(key=lambda row: row["object_id"])
        self.contention_rows.sort(key=lambda row: row["object_id"])
        self.capacity_rows = [{
            "crate_class": crate_class,
            "catalog_count": first_int(s.crates[crate_class]["count"]),
            "prior_consumed": s.prior_consumed.get(crate_class, 0),
            "remaining": remaining[crate_class],
        } for crate_class in sorted(s.crates)]

    # stage 9
    def transport_legs(self):
        s = self.s
        self.transport_rows = []
        for oid in s.object_order:
            crate_class = self.crate_assignment.get(oid, "")
            if not crate_class:
                continue
            mode = s.crates[crate_class]["transport_mode"].strip()
            lender = self.canonical[oid]["lender_id"]
            fallback = s.transport_rules.get((lender, mode))
            exception = 0
            if fallback is not None:
                mode = fallback
                exception = 1
            self.transport_rows.append({
                "object_id": oid, "crate_class": crate_class,
                "mode": mode, "exception": exception})

    # stage 13 windows
    def windows(self):
        s = self.s
        self.window_rows = []
        self.unschedulable_rows = []
        self.scheduled = {}
        for oid in s.object_order:
            placement = self.conservation_placement[oid]
            crate_class = self.crate_assignment.get(oid, "")
            if placement is None:
                code = "S_NO_PLACEMENT"
            elif not crate_class:
                code = "S_NO_CRATE"
            else:
                conservation = s.conservation[self.canonical[oid]["conservation_class"]]
                venue = s.venue_windows[placement["bay_id"]]
                crew = s.crew[self.canonical[oid]["crew_class"]]
                low = max(first_int(conservation["setup_start"]),
                          first_int(venue["access_start"]),
                          first_int(crew["avail_start"]))
                high = min(first_int(conservation["setup_end"]),
                           first_int(venue["access_end"]) - 1,
                           first_int(crew["avail_end"]))
                days_available = high - low + 1
                required_days = -(-self.setup_seconds[oid] // SECONDS_PER_DAY)
                if days_available <= 0:
                    code = "S_NO_WINDOW"
                elif required_days > days_available:
                    code = "S_SHORT_WINDOW"
                else:
                    code = SCHEDULE_OK
                self.window_rows.append({
                    "object_id": oid, "bay_id": placement["bay_id"],
                    "window_start": low, "window_end": high,
                    "required_days": required_days, "code": code})
                if code != SCHEDULE_OK:
                    self.unschedulable_rows.append({"object_id": oid, "code": code})
            if placement is None or not crate_class:
                self.window_rows.append({
                    "object_id": oid,
                    "bay_id": placement["bay_id"] if placement is not None else "",
                    "window_start": 0, "window_end": 0,
                    "required_days": 0, "code": code})
                self.unschedulable_rows.append({"object_id": oid, "code": code})
            self.scheduled[oid] = (code == SCHEDULE_OK)
        self.unschedulable_rows.sort(key=lambda row: (row["object_id"], row["code"]))

    # stage 14
    def dossier(self):
        s = self.s
        bays = {}
        for oid, placement in self.conservation_placement.items():
            if placement is not None:
                bays.setdefault(placement["bay_id"], []).append(oid)
        self.occupancy_rows = [{
            "bay_id": bay_id,
            "object_count": len(sorted(bays.get(bay_id, []))),
            "objects": sorted(bays.get(bay_id, [])),
        } for bay_id in sorted(s.bays)]

        def value_class(oid):
            cents = self.canonical[oid]["declared_value_cents"]
            for entry in s.value_classes:
                if cents >= entry["min_value"]:
                    return entry
            return s.value_classes[-1]

        self.unschedulable_penalty = sum(
            value_class(row["object_id"])["unschedulable_penalty"]
            for row in self.unschedulable_rows)
        self.contention_penalty = sum(
            value_class(row["object_id"])["contention_penalty"]
            for row in self.contention_rows)
        self.residual_by_object = {}
        self.total_residual = 0
        for oid in s.object_order:
            crate_class = self.crate_assignment.get(oid, "")
            residual = 0
            if crate_class:
                for entry in self.candidates[oid]:
                    if entry["crate_class"] == crate_class:
                        residual = entry["residual"]
                        break
            self.residual_by_object[oid] = residual
            self.total_residual += residual
        self.insurance = {}
        self.total_insurance = 0
        for oid in s.object_order:
            record = self.canonical[oid]
            lender = s.lenders[record["lender_id"]]
            cents = round_half_up(record["declared_value_cents"]
                                  * first_int(lender["insurance_rate_num"]),
                                  first_int(lender["insurance_rate_den"]))
            self.insurance[oid] = cents
            self.total_insurance += cents
        self.total_setup = sum(self.setup_seconds[oid] for oid in s.object_order)
        self.placed_objects = sum(1 for oid in s.object_order if self.scheduled[oid])
        self.objective = [self.placed_objects, self.unschedulable_penalty,
                          self.contention_penalty, len(self.cycle_rows), self.total_residual]
        self.audit_rows = [{
            "object_id": oid,
            "placed_bay": (self.conservation_placement[oid]["bay_id"]
                           if self.conservation_placement[oid] else ""),
            "crate_class": self.crate_assignment.get(oid, ""),
            "displaced": 1 if self.displaced.get(oid) else 0,
            "scheduled": 1 if self.scheduled[oid] else 0,
            "install_position": self.position[oid],
            "setup_seconds": self.setup_seconds[oid],
            "insurance_cents": self.insurance[oid],
        } for oid in s.object_order]

    def run(self):
        self.canonicalize()
        self.normalize_geometry()
        self.admissibility()
        self.bay_candidates()
        self.select_placement()
        self.conservation_adjustment()
        self.crate_candidates()
        self.crew_setup()
        self.dependency_graph()
        self.resolve_cycles()
        self.install_order()
        self.assign_crates()
        self.transport_legs()
        self.windows()
        self.dossier()


def main(argv):
    if len(argv) != 3:
        sys.stderr.write("usage: plan.py <DATA_DIR> <OUT_DIR>\n")
        return 2
    data_dir, out_dir = argv[1], argv[2]
    if not os.path.isdir(data_dir):
        sys.stderr.write("data directory not found: %s\n" % data_dir)
        return 2
    os.makedirs(out_dir, exist_ok=True)
    planner = Planner(Snapshot(data_dir))
    s = planner.s

    canonical_rows = []
    for oid in s.object_order:
        record = planner.canonical[oid]
        canonical_rows.append({
            "object_id": record["object_id"],
            "lender_id": record["lender_id"],
            "material_code": record["material_code"],
            "version": record["version"],
            "declared_value_cents": record["declared_value_cents"],
            "mass_g": record["mass_g"],
        })
    for name, rows in [
        ("canonical_objects.jsonl", canonical_rows),
        ("normalized_geometry.jsonl", [planner.geometry[oid] for oid in s.object_order]),
        ("admissibility_matrix.jsonl", planner.admissibility_rows),
        ("bay_candidate_matrix.jsonl", planner.bay_candidate_rows),
        ("selected_placement.jsonl", planner.selected_rows),
        ("rejected_placement.jsonl", planner.rejected_rows),
        ("conservation_adjusted.jsonl", planner.conservation_rows),
        ("crate_candidate_matrix.jsonl", planner.crate_candidate_rows),
        ("crate_assignment.jsonl", planner.crate_assignment_rows),
        ("crate_capacity_ledger.jsonl", planner.capacity_rows),
        ("crate_contention_report.jsonl", planner.contention_rows),
        ("transport_leg_manifest.jsonl", planner.transport_rows),
        ("dependency_edges.jsonl", planner.dependency_rows),
        ("cycle_resolution_audit.jsonl", planner.cycle_rows),
        ("install_order.jsonl", planner.install_rows),
        ("crew_requirement_ledger.jsonl", planner.crew_rows),
        ("install_window_table.jsonl", planner.window_rows),
        ("unschedulable_report.jsonl", planner.unschedulable_rows),
        ("gallery_occupancy_map.jsonl", planner.occupancy_rows),
        ("deterministic_audit.jsonl", planner.audit_rows),
    ]:
        write_jsonl(os.path.join(out_dir, name), rows)

    objective_names = list(s.objective_spec["order"])
    objective_by_name = {
        "placed_objects": planner.placed_objects,
        "unschedulable_penalty": planner.unschedulable_penalty,
        "contention_penalty": planner.contention_penalty,
        "cycles_broken": len(planner.cycle_rows),
        "total_residual": planner.total_residual,
    }
    objective_tuple = [objective_by_name[name] for name in objective_names]

    write_json(os.path.join(out_dir, "penalty_summary.json"), {
        "unschedulable_penalty": planner.unschedulable_penalty,
        "contention_penalty": planner.contention_penalty,
        "cycles_broken": len(planner.cycle_rows),
        "total_insurance_cents": planner.total_insurance,
    })
    write_json(os.path.join(out_dir, "objective_tuple.json"), {
        "objective": objective_tuple,
        "order": objective_names,
    })

    manifest = {}
    for name in HASHED_ARTIFACTS:
        with open(os.path.join(out_dir, name), "rb") as handle:
            manifest[name] = hashlib.sha256(handle.read()).hexdigest()
    program_path = os.path.abspath(__file__)
    with open(program_path, "rb") as handle:
        manifest[PROGRAM_NAME] = hashlib.sha256(handle.read()).hexdigest()
    write_json(os.path.join(out_dir, "integrity_manifest.json"),
               {name: manifest[name] for name in sorted(manifest)})

    chain = s.horizon["chain_seed"]
    for name in sorted(manifest):
        chain = hashlib.sha256((chain + manifest[name]).encode("utf-8")).hexdigest()

    graph = planner.acyclic_graph
    state = {}

    def has_cycle(node):
        state[node] = 1
        for target in sorted(graph[node]):
            if state.get(target, 0) == 1:
                return True
            if state.get(target, 0) == 0 and has_cycle(target):
                return True
        state[node] = 2
        return False

    final_cycle_count = 1 if any(has_cycle(node) for node in sorted(graph)
                                 if state.get(node, 0) == 0) else 0
    write_json(os.path.join(out_dir, "final_state.json"), {
        "chain": chain,
        "repair_converged": final_cycle_count == 0,
        "cycle_edges_removed": len(planner.cycle_rows),
        "final_cycle_count": final_cycle_count,
        "displacement_count": len(planner.contention_rows),
        "unschedulable_count": len(planner.unschedulable_rows),
        "placed_objects": planner.placed_objects,
        "total_setup_seconds": planner.total_setup,
        "total_insurance_cents": planner.total_insurance,
        "objective": objective_tuple,
    })
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
