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

LENGTH_SCALE = {"MM": 1000, "CM": 10000, "IN": 25400}
MASS_SCALE = {"G": 1, "KG": 1000}
CENTS_PER_UNIT = 100
SECONDS_PER_DAY = 3600

KIND_RULE = "D_RULE"
KIND_CONSERVATION = "D_CONSERVATION"
KIND_BAYSIZE = "D_BAYSIZE"
KIND_RANK = {KIND_BAYSIZE: 0, KIND_CONSERVATION: 1, KIND_RULE: 2}

HASHED_JSON = ("penalty_summary.json", "objective_tuple.json")


# --------------------------------------------------------------------------
# exact arithmetic helpers
# --------------------------------------------------------------------------

def round_half_up(num, den):
    """Round num/den to the nearest integer, halves going to the larger."""
    if den == 0:
        return 0
    if den < 0:
        num, den = -num, -den
    q, r = divmod(num, den)
    if 2 * r >= den:
        q += 1
    return q


def ceil_div(num, den):
    if den == 0:
        return 0
    return -(-num // den)


def reduced_ratio(num, den):
    """Reduce num/den; a zero denominator yields the sentinel 0/1."""
    if den == 0:
        return (0, 1)
    if den < 0:
        num, den = -num, -den
    g = gcd(abs(num), den)
    if g == 0:
        return (0, 1)
    return (num // g, den // g)


# --------------------------------------------------------------------------
# input
# --------------------------------------------------------------------------

def read_rows(data_dir, name):
    path = os.path.join(data_dir, name)
    with open(path, newline="", encoding="utf-8") as fh:
        return [dict(row) for row in csv.DictReader(fh)]


def read_json(data_dir, name):
    with open(os.path.join(data_dir, name), encoding="utf-8") as fh:
        return json.load(fh)


class Snapshot:
    def __init__(self, data_dir):
        self.config = read_json(data_dir, "horizon_config.json")
        self.objective_order = read_json(data_dir, "objective_order.json")

        self.master = read_rows(data_dir, "object_master.csv")
        self.dimensions = read_rows(data_dir, "object_dimensions.csv")
        self.mass = read_rows(data_dir, "object_mass.csv")
        self.value = read_rows(data_dir, "object_value.csv")
        self.orientation = read_rows(data_dir, "object_orientation.csv")
        self.versions = read_rows(data_dir, "object_version_map.csv")
        self.materials = read_rows(data_dir, "material_rules.csv")
        self.handling = read_rows(data_dir, "handling_class_rules.csv")
        self.bays = read_rows(data_dir, "gallery_bays.csv")
        self.bay_exceptions = read_rows(data_dir, "bay_exceptions.csv")
        self.crates = read_rows(data_dir, "crate_catalog.csv")
        self.reservations = read_rows(data_dir, "crate_reservations.csv")
        self.prior_state = read_rows(data_dir, "prior_state.csv")
        self.conservation = read_rows(data_dir, "conservation_rules.csv")
        self.venue_windows = read_rows(data_dir, "venue_windows.csv")
        self.dependency_rules = read_rows(data_dir, "dependency_rules.csv")
        self.crew_roster = read_rows(data_dir, "crew_roster.csv")
        self.transport_rules = read_rows(data_dir, "transport_rules.csv")
        self.lenders = read_rows(data_dir, "lender_master.csv")
        self.value_classes = read_rows(data_dir, "value_class_rules.csv")


# --------------------------------------------------------------------------
# serialization
# --------------------------------------------------------------------------

def write_lines(out_dir, name, records):
    path = os.path.join(out_dir, name)
    text = "".join(
        json.dumps(rec, sort_keys=True, separators=(",", ":")) + "\n"
        for rec in records
    )
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def write_object(out_dir, name, value):
    path = os.path.join(out_dir, name)
    text = json.dumps(value, sort_keys=True, indent=2) + "\n"
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def sha256_file(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


# --------------------------------------------------------------------------
# the derivation
# --------------------------------------------------------------------------

class Dossier:
    def __init__(self, snap):
        self.snap = snap
        cfg = snap.config
        self.clearance_scale = LENGTH_SCALE[cfg["clearance_unit"]]
        self.setup_div = int(cfg["setup_div"])
        self.chain_seed = cfg["chain_seed"]
        self.venue_end_exclusive = cfg["venue_end_convention"] == "exclusive"
        self.conservation_end_exclusive = (
            cfg["conservation_end_convention"] == "exclusive"
        )

        self.index_reference()
        self.stage1_canonical()
        self.stage2_geometry()
        self.stage3_admissibility()
        self.stage4_bay_candidates()
        self.stage5_selected_placement()
        self.stage6_conservation()
        self.stage7_crate_candidates()
        self.stage10_dependency_edges()
        self.stage11_cycle_resolution()
        self.stage13_crew()
        self.stage12_install_order()
        self.stage8_crate_capacity()
        self.stage9_transport()
        self.stage13_windows()
        self.stage14_dossier()

    # -- reference tables ---------------------------------------------------

    def index_reference(self):
        snap = self.snap
        s = self.clearance_scale

        self.material = {r["material_code"]: r for r in snap.materials}
        self.handling = {r["handling_class"]: r for r in snap.handling}
        self.conservation = {r["conservation_class"]: r for r in snap.conservation}
        self.crew = {r["crew_class"]: r for r in snap.crew_roster}
        self.lender = {r["lender_id"]: r for r in snap.lenders}
        self.venue = {r["bay_id"]: r for r in snap.venue_windows}

        self.bay_ids = sorted(r["bay_id"] for r in snap.bays)
        self.bay = {}
        for r in snap.bays:
            self.bay[r["bay_id"]] = {
                "bay_id": r["bay_id"],
                "iw": int(r["interior_w"]) * s,
                "ih": int(r["interior_h"]) * s,
                "id_": int(r["interior_d"]) * s,
                "lux": int(r["lux_level"]),
                "priority": int(r["bay_priority"]),
            }
        self.scan_bays = sorted(
            self.bay_ids,
            key=lambda b: (self.bay[b]["priority"], b),
        )

        self.blocked = {}
        for r in snap.bay_exceptions:
            self.blocked.setdefault(r["bay_id"], set()).add(r["blocked_class"])

        self.crate_ids = sorted(r["crate_class"] for r in snap.crates)
        self.crate = {}
        for r in snap.crates:
            self.crate[r["crate_class"]] = {
                "crate_class": r["crate_class"],
                "iw": int(r["inner_w"]) * s,
                "ih": int(r["inner_h"]) * s,
                "id_": int(r["inner_d"]) * s,
                "count": int(r["count"]),
                "mode": r["transport_mode"],
            }

        self.prior_consumed = {}
        for r in snap.prior_state:
            cls = r["crate_class"]
            self.prior_consumed[cls] = (
                self.prior_consumed.get(cls, 0) + int(r["consumed_count"])
            )

        self.reserved = {}
        for r in snap.reservations:
            key = (r["crate_class"], r["lender_id"])
            self.reserved[key] = self.reserved.get(key, 0) + int(r["reserved_count"])

        self.forbidden_mode = {}
        for r in snap.transport_rules:
            self.forbidden_mode.setdefault(r["lender_id"], []).append(
                (r["forbidden_mode"], r["fallback_mode"])
            )

        self.value_class_rules = sorted(
            (
                {
                    "value_class": r["value_class"],
                    "min_value": int(r["min_value"]),
                    "unschedulable_penalty": int(r["unschedulable_penalty"]),
                    "contention_penalty": int(r["contention_penalty"]),
                }
                for r in snap.value_classes
            ),
            key=lambda r: (-r["min_value"], r["value_class"]),
        )

        # a higher conservation class label is the more sensitive one
        self.sensitivity = {
            cls: rank for rank, cls in enumerate(sorted(self.conservation))
        }

    # -- stage 1 ------------------------------------------------------------

    def stage1_canonical(self):
        snap = self.snap
        self.object_ids = []
        master = {}
        for row in snap.master:
            oid = row["object_id"]
            if oid in master:
                continue
            master[oid] = row
            self.object_ids.append(oid)

        version = {}
        for row in snap.versions:
            oid = row["object_id"]
            v = int(row["version"])
            if oid not in version or v > version[oid]:
                version[oid] = v

        dims = {r["object_id"]: r for r in snap.dimensions}
        mass = {r["object_id"]: r for r in snap.mass}
        value = {r["object_id"]: r for r in snap.value}
        orient = {r["object_id"]: r for r in snap.orientation}

        self.obj = {}
        for oid in self.object_ids:
            row = master[oid]
            d = dims[oid]
            scale = LENGTH_SCALE[d["dim_unit"]]
            m = mass[oid]
            material = self.material[row["material_code"]]
            handling = self.handling[material["handling_class"]]
            o = orient[oid]
            declared = int(value[oid]["declared_value"])
            self.obj[oid] = {
                "object_id": oid,
                "lender_id": row["lender_id"],
                "material_code": row["material_code"],
                "version": version[oid],
                "declared_value": declared,
                "declared_value_cents": declared * CENTS_PER_UNIT,
                "mass_g": int(m["mass"]) * MASS_SCALE[m["mass_unit"]],
                "w": int(d["width"]) * scale,
                "h": int(d["height"]) * scale,
                "d": int(d["depth"]) * scale,
                "tip_allowed": int(o["tip_allowed"]) != 0,
                "rotate_allowed": int(o["rotate_allowed"]) != 0,
                "material": material,
                "handling": handling,
            }

        self.canonical_rows = [
            {
                "object_id": oid,
                "lender_id": self.obj[oid]["lender_id"],
                "material_code": self.obj[oid]["material_code"],
                "version": self.obj[oid]["version"],
                "declared_value_cents": self.obj[oid]["declared_value_cents"],
                "mass_g": self.obj[oid]["mass_g"],
            }
            for oid in self.object_ids
        ]

    # -- stage 2 ------------------------------------------------------------

    def stage2_geometry(self):
        s = self.clearance_scale
        self.geometry_rows = []
        for oid in self.object_ids:
            o = self.obj[oid]
            hc = o["handling"]
            cw = o["w"] + 2 * int(hc["clearance_w"]) * s
            cd = o["d"] + 2 * int(hc["clearance_d"]) * s
            ch = o["h"] + int(hc["clearance_h"]) * s
            num, den = reduced_ratio(o["h"], o["w"])
            o["cw"], o["ch"], o["cd"] = cw, ch, cd
            o["aspect_num"], o["aspect_den"] = num, den
            margin = int(hc["handle_margin"]) * s
            o["hw"] = o["w"] + margin
            o["hh"] = o["h"] + margin
            o["hd"] = o["d"] + margin
            o["handling_volume"] = o["hw"] * o["hh"] * o["hd"]
            self.geometry_rows.append(
                {
                    "object_id": oid,
                    "clearance_w": cw,
                    "clearance_h": ch,
                    "clearance_d": cd,
                    "aspect_num": num,
                    "aspect_den": den,
                }
            )

    # -- stage 3 ------------------------------------------------------------

    def stage3_admissibility(self):
        self.admissibility_rows = []
        for oid in self.object_ids:
            o = self.obj[oid]
            orientations = [0]
            if o["rotate_allowed"]:
                orientations.append(1)
            if o["tip_allowed"]:
                orientations.extend([2, 3])
            o["orientations"] = orientations
            self.admissibility_rows.append(
                {
                    "object_id": oid,
                    "orientations": orientations,
                    "tip_allowed": o["tip_allowed"],
                    "rotate_allowed": o["rotate_allowed"],
                }
            )

    @staticmethod
    def oriented(o, index):
        """Footprint width, footprint depth and vertical extent of an index."""
        cw, ch, cd = o["cw"], o["ch"], o["cd"]
        if index == 0:
            return cw, cd, ch
        if index == 1:
            return cd, cw, ch
        if index == 2:
            return cw, ch, cd
        return ch, cw, cd

    def fitting_orientation(self, oid, bay_id):
        """Smallest admissible orientation index fitting the bay, else None."""
        o = self.obj[oid]
        bay = self.bay[bay_id]
        for index in o["orientations"]:
            fw, fd, fv = self.oriented(o, index)
            if fw <= bay["iw"] and fd < bay["id_"] and fv <= bay["ih"]:
                return index
        return None

    # -- stage 4 ------------------------------------------------------------

    def stage4_bay_candidates(self):
        self.bay_candidate_rows = []
        self.fit = {}
        for oid in self.object_ids:
            for bay_id in self.bay_ids:
                index = self.fitting_orientation(oid, bay_id)
                self.fit[(oid, bay_id)] = index
                self.bay_candidate_rows.append(
                    {
                        "object_id": oid,
                        "bay_id": bay_id,
                        "fits": index is not None,
                        "orientation": -1 if index is None else index,
                    }
                )

    # -- stage 5 ------------------------------------------------------------

    def reject_code(self, oid):
        if self.obj[oid]["orientations"] == [0]:
            return "P_NO_ORIENTATION"
        return "P_NO_FIT"

    def stage5_selected_placement(self):
        self.selected_rows = []
        for oid in self.object_ids:
            o = self.obj[oid]
            chosen = None
            for bay_id in self.scan_bays:
                index = self.fit[(oid, bay_id)]
                if index is not None:
                    chosen = (bay_id, index)
                    break
            if chosen is None:
                o["selected_bay"] = ""
                o["selected_orientation"] = -1
                o["selected_code"] = self.reject_code(oid)
            else:
                o["selected_bay"], o["selected_orientation"] = chosen
                o["selected_code"] = "P_OK"
            self.selected_rows.append(
                {
                    "object_id": oid,
                    "bay_id": o["selected_bay"],
                    "orientation": o["selected_orientation"],
                    "code": o["selected_code"],
                }
            )

    # -- stage 6 ------------------------------------------------------------

    def conservation_ok(self, oid, bay_id):
        """Whether the bay passes conservation, and whether an exception hit."""
        material = self.obj[oid]["material"]
        bay = self.bay[bay_id]
        cls = material["conservation_class"]
        exception = cls in self.blocked.get(bay_id, ())
        needs_dark = int(self.conservation[cls]["needs_dark"]) != 0
        ok = (
            bay["lux"] <= int(material["max_lux"])
            and not exception
            and not (needs_dark and bay["lux"] > 0)
        )
        return ok, exception

    def stage6_conservation(self):
        self.conservation_rows = []
        for oid in self.object_ids:
            o = self.obj[oid]
            chosen = None
            geometric = []
            exception_only = True
            for bay_id in self.scan_bays:
                index = self.fit[(oid, bay_id)]
                if index is None:
                    continue
                geometric.append(bay_id)
                ok, exception = self.conservation_ok(oid, bay_id)
                if ok:
                    if chosen is None:
                        chosen = (bay_id, index)
                elif not exception:
                    exception_only = False
            if chosen is not None:
                o["bay"], o["orientation"] = chosen
                o["placement_code"] = "P_OK"
            elif not geometric:
                o["bay"] = ""
                o["orientation"] = -1
                o["placement_code"] = self.reject_code(oid)
            elif len(geometric) == 1 and exception_only:
                o["bay"] = ""
                o["orientation"] = -1
                o["placement_code"] = "P_BAY_EXCEPTION"
            else:
                o["bay"] = ""
                o["orientation"] = -1
                o["placement_code"] = "P_CONSERVATION_BLOCK"
            self.conservation_rows.append(
                {
                    "object_id": oid,
                    "bay_id": o["bay"],
                    "orientation": o["orientation"],
                    "code": o["placement_code"],
                }
            )

        rejected = set()
        for oid in self.object_ids:
            o = self.obj[oid]
            if o["selected_code"] != "P_OK":
                rejected.add((oid, o["selected_code"]))
            if o["placement_code"] != "P_OK":
                rejected.add((oid, o["placement_code"]))
        self.rejected_rows = [
            {"object_id": oid, "code": code} for oid, code in sorted(rejected)
        ]

    # -- stage 7 ------------------------------------------------------------

    def stage7_crate_candidates(self):
        self.crate_candidate_rows = []
        for oid in sorted(self.object_ids):
            o = self.obj[oid]
            handling_dims = sorted((o["hw"], o["hh"], o["hd"]))
            candidates = []
            for cls in self.crate_ids:
                crate = self.crate[cls]
                inner = sorted((crate["iw"], crate["ih"], crate["id_"]))
                if all(a <= b for a, b in zip(handling_dims, inner)):
                    residual = (
                        crate["iw"] * crate["ih"] * crate["id_"]
                        - o["handling_volume"]
                    )
                    candidates.append((residual, cls))
            candidates.sort(key=lambda c: (c[0], c[1]))
            o["crate_candidates"] = candidates
            o["primary_class"] = candidates[0][1] if candidates else ""
            for residual, cls in candidates:
                self.crate_candidate_rows.append(
                    {"object_id": oid, "crate_class": cls, "residual": residual}
                )

    # -- stage 10 -----------------------------------------------------------

    def stage10_dependency_edges(self):
        edges = {}

        def offer(frm, to, kind, weight):
            if frm == to:
                return
            key = (frm, to)
            current = edges.get(key)
            if current is None or KIND_RANK[kind] > KIND_RANK[current[0]]:
                edges[key] = (kind, weight)

        known = set(self.object_ids)
        for row in self.snap.dependency_rules:
            frm, to = row["from_object"], row["to_object"]
            if frm in known and to in known:
                offer(frm, to, KIND_RULE, int(row["edge_priority"]))

        by_bay = {}
        for oid in self.object_ids:
            bay_id = self.obj[oid]["bay"]
            if bay_id:
                by_bay.setdefault(bay_id, []).append(oid)

        for bay_id in sorted(by_bay):
            members = sorted(by_bay[bay_id])
            for i, a in enumerate(members):
                for b in members[i + 1:]:
                    oa, ob = self.obj[a], self.obj[b]
                    va, vb = oa["handling_volume"], ob["handling_volume"]
                    if va > vb or (va == vb and a < b):
                        first, second = a, b
                    else:
                        first, second = b, a
                    offer(
                        first, second, KIND_BAYSIZE,
                        self.obj[first]["handling_volume"],
                    )
                    sa = self.sensitivity[oa["material"]["conservation_class"]]
                    sb = self.sensitivity[ob["material"]["conservation_class"]]
                    if sa != sb:
                        first, second = (a, b) if sa > sb else (b, a)
                        offer(
                            first, second, KIND_CONSERVATION,
                            self.obj[first]["handling_volume"],
                        )

        self.edges = {
            key: {"kind": kind, "weight": weight}
            for key, (kind, weight) in edges.items()
        }
        self.dependency_rows = [
            {
                "from": frm,
                "to": to,
                "kind": self.edges[(frm, to)]["kind"],
                "weight": self.edges[(frm, to)]["weight"],
            }
            for frm, to in sorted(self.edges)
        ]

    # -- stage 11 -----------------------------------------------------------

    def adjacency(self):
        adj = {node: [] for node in self.graph_nodes}
        for frm, to in self.live_edges:
            adj[frm].append(to)
        for node in adj:
            adj[node].sort()
        return adj

    def find_cycle(self):
        adj = self.adjacency()
        color = {node: 0 for node in self.graph_nodes}
        for start in self.graph_nodes:
            if color[start]:
                continue
            color[start] = 1
            path = [start]
            stack = [(start, iter(adj[start]))]
            while stack:
                node, successors = stack[-1]
                advanced = False
                for nxt in successors:
                    if color[nxt] == 0:
                        color[nxt] = 1
                        path.append(nxt)
                        stack.append((nxt, iter(adj[nxt])))
                        advanced = True
                        break
                    if color[nxt] == 1:
                        index = path.index(nxt)
                        return path[index:] + [nxt]
                if not advanced:
                    color[node] = 2
                    path.pop()
                    stack.pop()
        return None

    def stage11_cycle_resolution(self):
        self.graph_nodes = sorted(self.object_ids)
        self.live_edges = dict(self.edges)
        self.removed_rows = []
        self.repair_converged = False
        for _ in range(len(self.live_edges) + 1):
            cycle = self.find_cycle()
            if cycle is None:
                self.repair_converged = True
                break
            pairs = [(cycle[i], cycle[i + 1]) for i in range(len(cycle) - 1)]
            worst = min(
                pairs,
                key=lambda p: (
                    KIND_RANK[self.live_edges[p]["kind"]],
                    self.live_edges[p]["weight"],
                    p[0],
                    p[1],
                ),
            )
            edge = self.live_edges.pop(worst)
            self.removed_rows.append(
                {
                    "from": worst[0],
                    "to": worst[1],
                    "kind": edge["kind"],
                    "weight": edge["weight"],
                }
            )
        self.final_cycle_count = 0 if self.repair_converged else 1
        self.cycles_broken = len(self.removed_rows)

    # -- stage 13 (crew and setup) -----------------------------------------

    def stage13_crew(self):
        self.crew_rows = []
        for oid in self.object_ids:
            o = self.obj[oid]
            hc = o["handling"]
            o["crew_class"] = hc["crew_class"]
            o["setup_seconds"] = round_half_up(
                int(hc["setup_base"]) * o["mass_g"], self.setup_div
            )
            self.crew_rows.append(
                {
                    "object_id": oid,
                    "crew_class": o["crew_class"],
                    "setup_seconds": o["setup_seconds"],
                }
            )

    # -- stage 12 -----------------------------------------------------------

    def stage12_install_order(self):
        indegree = {node: 0 for node in self.graph_nodes}
        successors = {node: [] for node in self.graph_nodes}
        for frm, to in self.live_edges:
            indegree[to] += 1
            successors[frm].append(to)
        ready = {node for node in self.graph_nodes if indegree[node] == 0}
        order = []
        while ready:
            node = min(ready, key=lambda n: (self.obj[n]["setup_seconds"], n))
            ready.discard(node)
            order.append(node)
            for nxt in sorted(successors[node]):
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    ready.add(nxt)
        self.install_sequence = order
        self.install_position = {oid: i for i, oid in enumerate(order, 1)}
        self.install_rows = [
            {"position": i, "object_id": oid} for i, oid in enumerate(order, 1)
        ]

    # -- stage 8 ------------------------------------------------------------

    def stage8_crate_capacity(self):
        remaining = {
            cls: self.crate[cls]["count"] - self.prior_consumed.get(cls, 0)
            for cls in self.crate_ids
        }
        reserved = dict(self.reserved)
        for oid in self.install_sequence:
            o = self.obj[oid]
            lender = o["lender_id"]
            available = [
                (residual, cls)
                for residual, cls in o["crate_candidates"]
                if remaining[cls] > 0
            ]
            if not available:
                o["crate_class"] = ""
                o["crate_code"] = "C_NO_CRATE"
                o["displaced"] = False
                o["assigned_residual"] = 0
                continue
            residual, cls = min(
                available,
                key=lambda c: (
                    0 if reserved.get((c[1], lender), 0) > 0 else 1,
                    c[0],
                    c[1],
                ),
            )
            remaining[cls] -= 1
            if reserved.get((cls, lender), 0) > 0:
                reserved[(cls, lender)] -= 1
            o["crate_class"] = cls
            o["assigned_residual"] = residual
            o["displaced"] = cls != o["primary_class"]
            o["crate_code"] = "C_DISPLACED" if o["displaced"] else "C_PRIMARY"

        self.crate_assignment_rows = [
            {
                "object_id": oid,
                "crate_class": self.obj[oid]["crate_class"],
                "code": self.obj[oid]["crate_code"],
                "primary_class": self.obj[oid]["primary_class"],
            }
            for oid in sorted(self.object_ids)
        ]
        self.capacity_rows = [
            {
                "crate_class": cls,
                "catalog_count": self.crate[cls]["count"],
                "prior_consumed": self.prior_consumed.get(cls, 0),
                "remaining": remaining[cls],
            }
            for cls in self.crate_ids
        ]
        self.contention_rows = [
            {
                "object_id": oid,
                "wanted_class": self.obj[oid]["primary_class"],
                "assigned_class": self.obj[oid]["crate_class"],
            }
            for oid in sorted(self.object_ids)
            if self.obj[oid]["displaced"]
        ]
        self.displacement_count = len(self.contention_rows)
        self.total_residual = sum(
            self.obj[oid]["assigned_residual"]
            for oid in self.object_ids
            if self.obj[oid]["crate_class"]
        )

    # -- stage 9 ------------------------------------------------------------

    def stage9_transport(self):
        self.transport_rows = []
        for oid in self.object_ids:
            o = self.obj[oid]
            cls = o["crate_class"]
            if not cls:
                continue
            mode = self.crate[cls]["mode"]
            exception = False
            for forbidden, fallback in self.forbidden_mode.get(o["lender_id"], ()):
                if mode == forbidden:
                    mode = fallback
                    exception = True
                    break
            self.transport_rows.append(
                {
                    "object_id": oid,
                    "crate_class": cls,
                    "mode": mode,
                    "exception": exception,
                }
            )

    # -- stage 13 (windows) -------------------------------------------------

    def stage13_windows(self):
        self.window_rows = []
        for oid in self.object_ids:
            o = self.obj[oid]
            cons = self.conservation[o["material"]["conservation_class"]]
            crew = self.crew[o["crew_class"]]
            required_days = ceil_div(o["setup_seconds"], SECONDS_PER_DAY)
            bay_id = o["bay"]
            if not bay_id:
                low, high = 0, -1
                code = "S_NO_PLACEMENT"
            else:
                venue = self.venue[bay_id]
                cons_end = int(cons["setup_end"])
                if self.conservation_end_exclusive:
                    cons_end -= 1
                venue_end = int(venue["access_end"])
                if self.venue_end_exclusive:
                    venue_end -= 1
                low = max(
                    int(cons["setup_start"]),
                    int(venue["access_start"]),
                    int(crew["avail_start"]),
                )
                high = min(cons_end, venue_end, int(crew["avail_end"]))
                if not o["crate_class"]:
                    code = "S_NO_CRATE"
                elif high < low:
                    code = "S_NO_WINDOW"
                elif required_days > high - low + 1:
                    code = "S_SHORT_WINDOW"
                else:
                    code = "S_OK"
            o["window_start"] = low
            o["window_end"] = high
            o["required_days"] = required_days
            o["schedule_code"] = code
            o["scheduled"] = code == "S_OK"
            self.window_rows.append(
                {
                    "object_id": oid,
                    "bay_id": bay_id,
                    "window_start": low,
                    "window_end": high,
                    "required_days": required_days,
                    "code": code,
                }
            )
        self.unschedulable_rows = [
            {"object_id": oid, "code": self.obj[oid]["schedule_code"]}
            for oid in sorted(self.object_ids)
            if not self.obj[oid]["scheduled"]
        ]

    # -- stage 14 -----------------------------------------------------------

    def value_class(self, oid):
        value = self.obj[oid]["declared_value"]
        for rule in self.value_class_rules:
            if value >= rule["min_value"]:
                return rule
        return None

    def stage14_dossier(self):
        occupancy = {bay_id: [] for bay_id in self.bay_ids}
        for oid in sorted(self.object_ids):
            bay_id = self.obj[oid]["bay"]
            if bay_id:
                occupancy.setdefault(bay_id, []).append(oid)
        self.occupancy_rows = [
            {
                "bay_id": bay_id,
                "object_count": len(occupancy[bay_id]),
                "objects": occupancy[bay_id],
            }
            for bay_id in sorted(occupancy)
        ]

        self.unschedulable_penalty = 0
        self.contention_penalty = 0
        self.total_insurance_cents = 0
        self.total_setup_seconds = 0
        for oid in self.object_ids:
            o = self.obj[oid]
            rule = self.value_class(oid)
            if rule is not None:
                if not o["scheduled"]:
                    self.unschedulable_penalty += rule["unschedulable_penalty"]
                if o["displaced"]:
                    self.contention_penalty += rule["contention_penalty"]
            lender = self.lender[o["lender_id"]]
            o["insurance_cents"] = round_half_up(
                o["declared_value_cents"] * int(lender["insurance_rate_num"]),
                int(lender["insurance_rate_den"]),
            )
            self.total_insurance_cents += o["insurance_cents"]
            self.total_setup_seconds += o["setup_seconds"]

        self.unschedulable_count = len(self.unschedulable_rows)
        self.placed_objects = sum(
            1 for oid in self.object_ids if self.obj[oid]["scheduled"]
        )

        self.audit_rows = [
            {
                "object_id": oid,
                "placed_bay": self.obj[oid]["bay"],
                "crate_class": self.obj[oid]["crate_class"],
                "displaced": self.obj[oid]["displaced"],
                "scheduled": self.obj[oid]["scheduled"],
                "install_position": self.install_position[oid],
                "setup_seconds": self.obj[oid]["setup_seconds"],
                "insurance_cents": self.obj[oid]["insurance_cents"],
            }
            for oid in self.object_ids
        ]

        self.objective = [
            self.placed_objects,
            self.unschedulable_penalty,
            self.contention_penalty,
            self.cycles_broken,
            self.total_residual,
        ]


# --------------------------------------------------------------------------
# emission
# --------------------------------------------------------------------------

def emit(dossier, out_dir, program_path):
    lines_artifacts = [
        ("canonical_objects.jsonl", dossier.canonical_rows),
        ("normalized_geometry.jsonl", dossier.geometry_rows),
        ("admissibility_matrix.jsonl", dossier.admissibility_rows),
        ("bay_candidate_matrix.jsonl", dossier.bay_candidate_rows),
        ("selected_placement.jsonl", dossier.selected_rows),
        ("rejected_placement.jsonl", dossier.rejected_rows),
        ("conservation_adjusted.jsonl", dossier.conservation_rows),
        ("crate_candidate_matrix.jsonl", dossier.crate_candidate_rows),
        ("crate_assignment.jsonl", dossier.crate_assignment_rows),
        ("crate_capacity_ledger.jsonl", dossier.capacity_rows),
        ("crate_contention_report.jsonl", dossier.contention_rows),
        ("transport_leg_manifest.jsonl", dossier.transport_rows),
        ("dependency_edges.jsonl", dossier.dependency_rows),
        ("cycle_resolution_audit.jsonl", dossier.removed_rows),
        ("install_order.jsonl", dossier.install_rows),
        ("crew_requirement_ledger.jsonl", dossier.crew_rows),
        ("install_window_table.jsonl", dossier.window_rows),
        ("unschedulable_report.jsonl", dossier.unschedulable_rows),
        ("gallery_occupancy_map.jsonl", dossier.occupancy_rows),
        ("deterministic_audit.jsonl", dossier.audit_rows),
    ]
    for name, rows in lines_artifacts:
        write_lines(out_dir, name, rows)

    write_object(
        out_dir,
        "penalty_summary.json",
        {
            "unschedulable_penalty": dossier.unschedulable_penalty,
            "contention_penalty": dossier.contention_penalty,
            "cycles_broken": dossier.cycles_broken,
            "total_insurance_cents": dossier.total_insurance_cents,
        },
    )
    write_object(
        out_dir,
        "objective_tuple.json",
        {
            "objective": dossier.objective,
            "order": dossier.snap.objective_order["order"],
        },
    )

    manifest = {}
    for name, _ in lines_artifacts:
        manifest[name] = sha256_file(os.path.join(out_dir, name))
    for name in HASHED_JSON:
        manifest[name] = sha256_file(os.path.join(out_dir, name))
    manifest[os.path.basename(program_path)] = sha256_file(program_path)
    write_object(out_dir, "integrity_manifest.json", manifest)

    chain = dossier.chain_seed
    for name in sorted(manifest):
        chain = hashlib.sha256((chain + manifest[name]).encode("utf-8")).hexdigest()

    write_object(
        out_dir,
        "final_state.json",
        {
            "chain": chain,
            "repair_converged": dossier.repair_converged,
            "cycle_edges_removed": dossier.cycles_broken,
            "final_cycle_count": dossier.final_cycle_count,
            "displacement_count": dossier.displacement_count,
            "unschedulable_count": dossier.unschedulable_count,
            "placed_objects": dossier.placed_objects,
            "total_setup_seconds": dossier.total_setup_seconds,
            "total_insurance_cents": dossier.total_insurance_cents,
            "objective": dossier.objective,
        },
    )


def main(argv):
    if len(argv) != 3:
        sys.stderr.write("usage: plan.py <DATA_DIR> <OUT_DIR>\n")
        return 2
    data_dir, out_dir = argv[1], argv[2]
    os.makedirs(out_dir, exist_ok=True)
    dossier = Dossier(Snapshot(data_dir))
    emit(dossier, out_dir, os.path.abspath(__file__))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
