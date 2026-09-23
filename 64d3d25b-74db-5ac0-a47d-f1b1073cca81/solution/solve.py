#!/usr/bin/env python3
"""Reference solver for the museum touring installation dossier.

This is an independent implementation of the task contract described in
environment/data/rules.md and environment/data/schema.md. It reads the shipped
installation snapshot from a data directory and derives every declared artifact
into an output directory.

The whole pipeline is integer / exact-rational. Lengths live in micrometres,
mass in grams, monetary value in cents, and access windows in integer day
indices. Ratios are stored as reduced numerator/denominator pairs; any stage
that forms a ratio rounds to its quantum with half-up rounding.
"""
from __future__ import annotations

import json
import sys
from hashlib import sha256
from math import gcd
from pathlib import Path

# Source unit -> canonical integer scale.
LEN_TO_UM = {"MM": 1000, "CM": 10_000, "IN": 25_400}
MASS_TO_G = {"G": 1, "KG": 1000}

# Conservation sensitivity, least to most restrictive, indexed for comparison.
SENSITIVITY_ORDER = ("L1", "L2", "L3", "L4")

# Lower rank wins when two derived edges share the same endpoints.
KIND_RANK = {"D_RULE": 0, "D_CONSERVATION": 1, "D_BAYSIZE": 2}

SETUP_QUANTUM = 700          # crew setup rounding quantum
SECONDS_PER_DAY = 3600       # install seconds available per window day
ZERO_CHAIN = "0" * 64        # integrity chain seed


def _round_half_up(top, bot):
    """Nearest integer to top/bot, halves rounding away from zero (both >= 0)."""
    if bot == 0:
        return 0
    whole, rem = divmod(top, bot)
    return whole + 1 if rem * 2 >= bot else whole


def _lowest_terms(top, bot):
    """Reduced (top, bot); a zero denominator collapses to the 0/1 sentinel."""
    if bot == 0:
        return (0, 1)
    common = gcd(top, bot)
    return (top // common, bot // common)


def _emit_object(payload):
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def _emit_lines(records):
    return "".join(_emit_object(record) + "\n" for record in records)


def _emit_doc(payload):
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _read_table(path):
    """Parse a headered CSV into a list of column->value dicts (blank lines skipped)."""
    handle = Path(path)
    if not handle.is_file():
        return []
    kept = [entry for entry in handle.read_text(encoding="utf-8").splitlines() if entry.strip()]
    if not kept:
        return []
    cols = kept[0].split(",")
    return [dict(zip(cols, entry.split(","))) for entry in kept[1:]]


class TouringDossier:
    """Holds the parsed snapshot and derives the installation dossier from it."""

    def __init__(self, data_dir):
        base = Path(data_dir)
        self.cfg = json.loads((base / "horizon_config.json").read_text(encoding="utf-8"))
        self.objective_spec = json.loads((base / "objective_order.json").read_text(encoding="utf-8"))
        self.items = {}
        self.order = []
        self._ingest(base)

    # ---------------------------------------------------------------- parsing
    def _ingest(self, base):
        top_version = {}
        for row in _read_table(base / "object_version_map.csv"):
            marker = row["object_id"]
            top_version[marker] = max(top_version.get(marker, 0), int(row["version"]))

        for row in _read_table(base / "object_master.csv"):
            marker = row["object_id"]
            if marker in self.items:
                continue
            self.items[marker] = dict(
                lender_id=row["lender_id"],
                material_code=row["material_code"],
                version=top_version.get(marker, 1),
            )
            self.order.append(marker)

        for row in _read_table(base / "object_dimensions.csv"):
            rec = self.items[row["object_id"]]
            rec["width"] = int(row["width"])
            rec["height"] = int(row["height"])
            rec["depth"] = int(row["depth"])
            rec["dim_unit"] = row["dim_unit"]

        for row in _read_table(base / "object_mass.csv"):
            rec = self.items[row["object_id"]]
            rec["mass"] = int(row["mass"])
            rec["mass_unit"] = row["mass_unit"]

        for row in _read_table(base / "object_value.csv"):
            self.items[row["object_id"]]["declared_value"] = int(row["declared_value"])

        for row in _read_table(base / "object_orientation.csv"):
            rec = self.items[row["object_id"]]
            rec["tip_allowed"] = int(row["tip_allowed"])
            rec["rotate_allowed"] = int(row["rotate_allowed"])

        self.materials = {}
        for row in _read_table(base / "material_rules.csv"):
            self.materials[row["material_code"]] = dict(
                conservation_class=row["conservation_class"],
                max_lux=int(row["max_lux"]),
                handling_class=row["handling_class"],
            )

        self.handling = {}
        for row in _read_table(base / "handling_class_rules.csv"):
            self.handling[row["handling_class"]] = dict(
                clearance_w=int(row["clearance_w"]),
                clearance_d=int(row["clearance_d"]),
                clearance_h=int(row["clearance_h"]),
                handle_margin=int(row["handle_margin"]),
                crew_class=row["crew_class"],
                setup_base=int(row["setup_base"]),
            )

        self.bays = []
        for row in _read_table(base / "gallery_bays.csv"):
            self.bays.append(dict(
                bay_id=row["bay_id"],
                interior_w=int(row["interior_w"]),
                interior_h=int(row["interior_h"]),
                interior_d=int(row["interior_d"]),
                lux_level=int(row["lux_level"]),
                bay_priority=int(row["bay_priority"]),
            ))

        self.bay_block = {}
        for row in _read_table(base / "bay_exceptions.csv"):
            self.bay_block.setdefault(row["bay_id"], set()).add(row["blocked_class"])

        self.crates = []
        for row in _read_table(base / "crate_catalog.csv"):
            self.crates.append(dict(
                crate_class=row["crate_class"],
                inner_w=int(row["inner_w"]),
                inner_h=int(row["inner_h"]),
                inner_d=int(row["inner_d"]),
                count=int(row["count"]),
                transport_mode=row["transport_mode"],
            ))

        self.reservations = []
        for row in _read_table(base / "crate_reservations.csv"):
            self.reservations.append(dict(
                crate_class=row["crate_class"],
                lender_id=row["lender_id"],
                reserved_count=int(row["reserved_count"]),
            ))

        self.prior = {}
        for row in _read_table(base / "prior_state.csv"):
            self.prior[row["crate_class"]] = int(row["consumed_count"])

        self.cons = {}
        for row in _read_table(base / "conservation_rules.csv"):
            self.cons[row["conservation_class"]] = dict(
                setup_start=int(row["setup_start"]),
                setup_end=int(row["setup_end"]),
                needs_dark=int(row["needs_dark"]),
            )

        self.venue = {}
        for row in _read_table(base / "venue_windows.csv"):
            self.venue[row["bay_id"]] = dict(
                access_start=int(row["access_start"]),
                access_end=int(row["access_end"]),
            )

        self.deps = []
        for row in _read_table(base / "dependency_rules.csv"):
            self.deps.append(dict(
                from_object=row["from_object"],
                to_object=row["to_object"],
                edge_priority=int(row["edge_priority"]),
            ))

        self.crew = {}
        for row in _read_table(base / "crew_roster.csv"):
            self.crew[row["crew_class"]] = dict(
                avail_start=int(row["avail_start"]),
                avail_end=int(row["avail_end"]),
            )

        self.transport = {}
        for row in _read_table(base / "transport_rules.csv"):
            self.transport[row["lender_id"]] = dict(
                forbidden_mode=row["forbidden_mode"],
                fallback_mode=row["fallback_mode"],
            )

        self.lender = {}
        for row in _read_table(base / "lender_master.csv"):
            self.lender[row["lender_id"]] = dict(
                insurance_rate_num=int(row["insurance_rate_num"]),
                insurance_rate_den=int(row["insurance_rate_den"]),
                pref_mode=row["pref_mode"],
            )

        self.value_classes = []
        for row in _read_table(base / "value_class_rules.csv"):
            self.value_classes.append(dict(
                value_class=row["value_class"],
                min_value=int(row["min_value"]),
                unschedulable_penalty=int(row["unschedulable_penalty"]),
                contention_penalty=int(row["contention_penalty"]),
            ))
        self.value_classes.sort(key=lambda entry: -entry["min_value"])

    # ------------------------------------------------------------- geometry
    def _dims_um(self, marker):
        rec = self.items[marker]
        factor = LEN_TO_UM[rec["dim_unit"]]
        return rec["width"] * factor, rec["height"] * factor, rec["depth"] * factor

    def _mass_grams(self, marker):
        rec = self.items[marker]
        return rec["mass"] * MASS_TO_G[rec["mass_unit"]]

    def _handling_rules(self, marker):
        material = self.materials[self.items[marker]["material_code"]]
        return self.handling[material["handling_class"]]

    def _clearance_box(self, marker):
        rules = self._handling_rules(marker)
        w, h, d = self._dims_um(marker)
        return (w + rules["clearance_w"] * 2000,
                h + rules["clearance_h"] * 1000,
                d + rules["clearance_d"] * 2000)

    def _handling_box(self, marker):
        rules = self._handling_rules(marker)
        w, h, d = self._dims_um(marker)
        pad = rules["handle_margin"] * 1000
        return (w + pad * 2, h + pad * 2, d + pad * 2)

    def _orientations(self, marker):
        rec = self.items[marker]
        codes = [0]
        if rec["rotate_allowed"]:
            codes.append(1)
        if rec["tip_allowed"]:
            codes.extend((2, 3))
        return codes

    @staticmethod
    def _footprint(box, ori):
        a, b, c = box
        layouts = {0: (a, c, b), 1: (c, a, b), 2: (a, b, c), 3: (b, a, c)}
        return layouts[ori]

    def _bay_admits(self, marker, bay, ori):
        foot_w, foot_d, upright = self._footprint(self._clearance_box(marker), ori)
        span_w = bay["interior_w"] * 1000
        span_h = bay["interior_h"] * 1000
        span_d = bay["interior_d"] * 1000
        return foot_w <= span_w and foot_d < span_d and upright <= span_h

    def _conservation_ok(self, bay, material):
        klass = material["conservation_class"]
        if klass in self.bay_block.get(bay["bay_id"], set()):
            return False
        if bay["lux_level"] > material["max_lux"]:
            return False
        if self.cons[klass]["needs_dark"] and bay["lux_level"] > 0:
            return False
        return True

    def _choose_bay(self, marker, enforce_conservation):
        material = self.materials[self.items[marker]["material_code"]]
        allowed = self._orientations(marker)
        for bay in sorted(self.bays, key=lambda cell: (cell["bay_priority"], cell["bay_id"])):
            usable = [ori for ori in allowed if self._bay_admits(marker, bay, ori)]
            if not usable:
                continue
            if enforce_conservation and not self._conservation_ok(bay, material):
                continue
            return (bay["bay_id"], usable[0])
        return None

    def _stage_geometry(self):
        normalized = []
        admissibility = []
        candidates = []
        gallery = sorted(self.bays, key=lambda cell: cell["bay_id"])
        for marker in self.order:
            w, h, _d = self._dims_um(marker)
            cw, ch, cd = self._clearance_box(marker)
            ratio_top, ratio_bot = _lowest_terms(h, w)
            normalized.append(dict(
                object_id=marker,
                clearance_w=cw,
                clearance_h=ch,
                clearance_d=cd,
                aspect_num=ratio_top,
                aspect_den=ratio_bot,
            ))
            allowed = self._orientations(marker)
            rec = self.items[marker]
            admissibility.append(dict(
                object_id=marker,
                orientations=allowed,
                tip_allowed=rec["tip_allowed"],
                rotate_allowed=rec["rotate_allowed"],
            ))
            for bay in gallery:
                usable = [ori for ori in allowed if self._bay_admits(marker, bay, ori)]
                candidates.append(dict(
                    object_id=marker,
                    bay_id=bay["bay_id"],
                    fits=bool(usable),
                    orientation=usable[0] if usable else -1,
                ))
        return normalized, admissibility, candidates

    # ------------------------------------------------------------ placement
    def _stage_placement(self):
        selected = []
        rejected = []
        adjusted = []
        homed = {}
        for marker in self.order:
            geo = self._choose_bay(marker, enforce_conservation=False)
            if geo is None:
                sole = len(self._orientations(marker)) == 1
                verdict = "P_NO_ORIENTATION" if sole else "P_NO_FIT"
                rejected.append(dict(object_id=marker, code=verdict))
                selected.append(dict(object_id=marker, bay_id="", orientation=-1, code=verdict))
                adjusted.append(dict(object_id=marker, bay_id="", orientation=-1, code=verdict))
                continue
            selected.append(dict(object_id=marker, bay_id=geo[0], orientation=geo[1], code="P_OK"))
            guarded = self._choose_bay(marker, enforce_conservation=True)
            if guarded is None:
                adjusted.append(dict(object_id=marker, bay_id="", orientation=-1, code="P_CONSERVATION_BLOCK"))
                rejected.append(dict(object_id=marker, code="P_CONSERVATION_BLOCK"))
            else:
                adjusted.append(dict(object_id=marker, bay_id=guarded[0], orientation=guarded[1], code="P_OK"))
                homed[marker] = guarded
        rejected.sort(key=lambda entry: (entry["object_id"], entry["code"]))
        return selected, rejected, adjusted, homed

    # --------------------------------------------------------------- crates
    def _crate_inner_um(self, crate):
        return crate["inner_w"] * 1000, crate["inner_h"] * 1000, crate["inner_d"] * 1000

    def _crate_volume(self, crate):
        iw, ih, idp = self._crate_inner_um(crate)
        return iw * ih * idp

    def _handling_volume(self, marker):
        w, h, d = self._handling_box(marker)
        return w * h * d

    def _slack(self, marker, crate):
        return self._crate_volume(crate) - self._handling_volume(marker)

    def _crate_admits(self, marker, crate):
        need = sorted(self._handling_box(marker))
        room = sorted(self._crate_inner_um(crate))
        return all(need[axis] <= room[axis] for axis in range(3))

    def _stage_crates(self, homed, install_seq):
        headroom = {}
        for crate in self.crates:
            headroom[crate["crate_class"]] = crate["count"] - self.prior.get(crate["crate_class"], 0)
        reserved = {}
        for rsv in self.reservations:
            slot = (rsv["crate_class"], rsv["lender_id"])
            reserved[slot] = reserved.get(slot, 0) + rsv["reserved_count"]

        decision = {}
        crated = {}
        # Shared capacity is consumed in installation order, so a later object
        # only sees the crates that earlier installs left behind.
        for marker in install_seq:
            if marker not in homed:
                continue
            fitting = [crate for crate in self.crates if self._crate_admits(marker, crate)]
            preferred = sorted(fitting, key=lambda crate: (self._slack(marker, crate), crate["crate_class"]))
            primary = preferred[0]["crate_class"] if preferred else ""
            owner = self.items[marker]["lender_id"]

            def by_priority(crate, owner=owner, marker=marker):
                reserved_first = 0 if reserved.get((crate["crate_class"], owner), 0) > 0 else 1
                return (reserved_first, self._slack(marker, crate), crate["crate_class"])

            picked = None
            for crate in sorted(fitting, key=by_priority):
                if headroom.get(crate["crate_class"], 0) > 0:
                    picked = crate["crate_class"]
                    break
            if picked is None:
                decision[marker] = ("", "C_NO_CRATE", primary)
                continue
            headroom[picked] -= 1
            if reserved.get((picked, owner), 0) > 0:
                reserved[(picked, owner)] -= 1
            outcome = "C_PRIMARY" if picked == primary else "C_DISPLACED"
            decision[marker] = (picked, outcome, primary)
            crated[marker] = picked

        candidate_rows = []
        assignment_rows = []
        contention_rows = []
        for marker in sorted(self.order):
            if marker not in homed:
                continue
            fitting = [crate for crate in self.crates if self._crate_admits(marker, crate)]
            preferred = sorted(fitting, key=lambda crate: (self._slack(marker, crate), crate["crate_class"]))
            for crate in preferred:
                candidate_rows.append(dict(
                    object_id=marker,
                    crate_class=crate["crate_class"],
                    residual=self._slack(marker, crate),
                ))
            chosen, outcome, primary = decision[marker]
            assignment_rows.append(dict(
                object_id=marker,
                crate_class=chosen,
                code=outcome,
                primary_class=primary,
            ))
            if outcome == "C_DISPLACED":
                contention_rows.append(dict(
                    object_id=marker,
                    wanted_class=primary,
                    assigned_class=chosen,
                ))

        ledger_rows = []
        for crate in sorted(self.crates, key=lambda item: item["crate_class"]):
            label = crate["crate_class"]
            ledger_rows.append(dict(
                crate_class=label,
                catalog_count=crate["count"],
                prior_consumed=self.prior.get(label, 0),
                remaining=headroom[label],
            ))
        return candidate_rows, assignment_rows, ledger_rows, contention_rows, crated

    def _stage_transport(self, crated):
        mode_of = {crate["crate_class"]: crate["transport_mode"] for crate in self.crates}
        rows = []
        for marker in self.order:
            if marker not in crated:
                continue
            label = crated[marker]
            mode = mode_of[label]
            flagged = 0
            rule = self.transport.get(self.items[marker]["lender_id"])
            if rule and rule["forbidden_mode"] == mode:
                mode = rule["fallback_mode"]
                flagged = 1
            rows.append(dict(object_id=marker, crate_class=label, mode=mode, exception=flagged))
        return rows

    # --------------------------------------------------------- dependencies
    def _stage_dependencies(self, homed):
        edges = []
        for rule in self.deps:
            edges.append({"kind": "D_RULE", "from": rule["from_object"],
                          "to": rule["to_object"], "weight": rule["edge_priority"]})
        grouped = {}
        for marker in self.order:
            if marker in homed:
                grouped.setdefault(homed[marker][0], []).append(marker)
        for bay_id in sorted(grouped):
            members = sorted(grouped[bay_id])
            for left in members:
                for right in members:
                    if left == right:
                        continue
                    vol_left = self._handling_volume(left)
                    vol_right = self._handling_volume(right)
                    if vol_left > vol_right or (vol_left == vol_right and left < right):
                        edges.append({"kind": "D_BAYSIZE", "from": left,
                                      "to": right, "weight": vol_left})
                    rank_left = SENSITIVITY_ORDER.index(
                        self.materials[self.items[left]["material_code"]]["conservation_class"])
                    rank_right = SENSITIVITY_ORDER.index(
                        self.materials[self.items[right]["material_code"]]["conservation_class"])
                    if rank_left > rank_right:
                        edges.append({"kind": "D_CONSERVATION", "from": left,
                                      "to": right, "weight": self._handling_volume(left)})
        winners = {}
        for edge in edges:
            slot = (edge["from"], edge["to"])
            if slot not in winners or KIND_RANK[edge["kind"]] < KIND_RANK[winners[slot]["kind"]]:
                winners[slot] = edge
        return [winners[slot] for slot in sorted(winners)]

    @staticmethod
    def _detect_cycle(nodes, edges):
        present = set(nodes)
        outgoing = {node: [] for node in nodes}
        for edge in edges:
            if edge["from"] in present and edge["to"] in present:
                outgoing[edge["from"]].append(edge)
        for bucket in outgoing.values():
            bucket.sort(key=lambda edge: edge["to"])
        phase = {node: 0 for node in nodes}  # 0 unseen, 1 on-stack, 2 finished
        trail = []

        def walk(node):
            phase[node] = 1
            for edge in outgoing[node]:
                nxt = edge["to"]
                if phase[nxt] == 0:
                    trail.append(edge)
                    hit = walk(nxt)
                    if hit is not None:
                        return hit
                    trail.pop()
                elif phase[nxt] == 1:
                    ring = []
                    seen_entry = False
                    for step in trail:
                        if step["from"] == nxt:
                            seen_entry = True
                        if seen_entry:
                            ring.append(step)
                    ring.append(edge)
                    return ring
            phase[node] = 2
            return None

        for anchor in sorted(nodes):
            if phase[anchor] == 0:
                hit = walk(anchor)
                if hit is not None:
                    return hit
        return None

    def _resolve(self, edges):
        nodes = list(self.order)
        surviving = list(edges)
        broken_edges = []
        for _ in range(10001):
            ring = self._detect_cycle(nodes, surviving)
            if ring is None:
                break
            culprit = max(ring, key=lambda edge: (KIND_RANK[edge["kind"]], edge["weight"],
                                                   edge["from"], edge["to"]))
            broken_edges.append({"kind": culprit["kind"], "from": culprit["from"],
                                 "to": culprit["to"], "weight": culprit["weight"]})
            surviving = [edge for edge in surviving
                         if (edge["from"], edge["to"], edge["kind"])
                         != (culprit["from"], culprit["to"], culprit["kind"])]
        else:
            raise RuntimeError("cycle resolution exceeded its iteration budget")
        return surviving, broken_edges

    # ---------------------------------------------------------- scheduling
    def _setup_seconds(self, marker):
        rules = self._handling_rules(marker)
        return _round_half_up(rules["setup_base"] * self._mass_grams(marker), SETUP_QUANTUM)

    def _install_order(self, surviving):
        nodes = list(self.order)
        indegree = dict.fromkeys(nodes, 0)
        successors = {node: [] for node in nodes}
        for edge in surviving:
            successors[edge["from"]].append(edge["to"])
            indegree[edge["to"]] += 1
        cost = {node: self._setup_seconds(node) for node in nodes}
        frontier = [node for node in nodes if indegree[node] == 0]
        sequence = []
        while frontier:
            frontier.sort(key=lambda node: (cost[node], node))
            head = frontier.pop(0)
            sequence.append(head)
            for target in sorted(successors[head]):
                indegree[target] -= 1
                if indegree[target] == 0:
                    frontier.append(target)
        return sequence

    def _stage_schedule(self, homed, crated):
        crew_rows = []
        window_rows = []
        unscheduled = []
        booked = {}
        for marker in self.order:
            material = self.materials[self.items[marker]["material_code"]]
            rules = self.handling[material["handling_class"]]
            crew_class = rules["crew_class"]
            seconds = self._setup_seconds(marker)
            crew_rows.append(dict(object_id=marker, crew_class=crew_class, setup_seconds=seconds))
            if marker not in homed:
                unscheduled.append(dict(object_id=marker, code="S_NO_PLACEMENT"))
                continue
            if marker not in crated:
                unscheduled.append(dict(object_id=marker, code="S_NO_CRATE"))
                continue
            bay_id = homed[marker][0]
            limits = self.cons[material["conservation_class"]]
            access = self.venue[bay_id]
            roster = self.crew[crew_class]
            low = max(limits["setup_start"], access["access_start"], roster["avail_start"])
            high = min(limits["setup_end"], access["access_end"] - 1, roster["avail_end"])
            need_days = -(-seconds // SECONDS_PER_DAY)
            if low <= high and need_days <= high - low + 1:
                window_rows.append(dict(object_id=marker, bay_id=bay_id, window_start=low,
                                        window_end=high, required_days=need_days, code="S_OK"))
                booked[marker] = (low, high)
            elif low > high:
                window_rows.append(dict(object_id=marker, bay_id=bay_id, window_start=-1,
                                        window_end=-1, required_days=need_days, code="S_NO_WINDOW"))
                unscheduled.append(dict(object_id=marker, code="S_NO_WINDOW"))
            else:
                window_rows.append(dict(object_id=marker, bay_id=bay_id, window_start=low,
                                        window_end=high, required_days=need_days, code="S_SHORT_WINDOW"))
                unscheduled.append(dict(object_id=marker, code="S_SHORT_WINDOW"))
        unscheduled.sort(key=lambda entry: (entry["object_id"], entry["code"]))
        return crew_rows, window_rows, unscheduled, booked

    # -------------------------------------------------------------- pricing
    def _value_class(self, amount):
        for band in self.value_classes:
            if amount >= band["min_value"]:
                return band
        return self.value_classes[-1]

    def _insurance_cents(self, marker):
        rec = self.items[marker]
        rate = self.lender[rec["lender_id"]]
        cents = rec["declared_value"] * 100
        return _round_half_up(cents * rate["insurance_rate_num"], rate["insurance_rate_den"])

    # ------------------------------------------------------------ assembly
    def build(self, program_bytes=b""):
        normalized, admissibility, candidates = self._stage_geometry()
        selected, rejected, adjusted, homed = self._stage_placement()
        edges = self._stage_dependencies(homed)
        surviving, broken_edges = self._resolve(edges)
        sequence = self._install_order(surviving)
        order_rows = [dict(position=index, object_id=marker) for index, marker in enumerate(sequence)]
        crate_candidates, crate_assignment, crate_ledger, contention, crated = \
            self._stage_crates(homed, sequence)
        transport_rows = self._stage_transport(crated)
        crew_rows, window_rows, unscheduled, booked = self._stage_schedule(homed, crated)

        occupancy = {}
        for marker in self.order:
            if marker in homed:
                occupancy.setdefault(homed[marker][0], []).append(marker)
        gallery_rows = []
        for bay in sorted(self.bays, key=lambda cell: cell["bay_id"]):
            residents = sorted(occupancy.get(bay["bay_id"], []))
            gallery_rows.append(dict(bay_id=bay["bay_id"], object_count=len(residents), objects=residents))

        unschedulable_cost = sum(
            self._value_class(self.items[entry["object_id"]]["declared_value"])["unschedulable_penalty"]
            for entry in unscheduled)
        contention_cost = sum(
            self._value_class(self.items[entry["object_id"]]["declared_value"])["contention_penalty"]
            for entry in contention)
        break_count = len(broken_edges)
        insurance_sum = sum(self._insurance_cents(marker) for marker in self.order)
        displaced_ids = {entry["object_id"] for entry in contention}

        crate_by_class = {crate["crate_class"]: crate for crate in self.crates}
        residual_total = 0
        for marker in self.order:
            if marker in crated:
                residual_total += self._crate_volume(crate_by_class[crated[marker]]) - self._handling_volume(marker)

        scheduled_count = len(booked)
        objective_vec = [scheduled_count, unschedulable_cost, contention_cost, break_count, residual_total]
        setup_sum = sum(self._setup_seconds(marker) for marker in self.order)

        audit_rows = []
        for marker in self.order:
            audit_rows.append(dict(
                object_id=marker,
                placed_bay=homed[marker][0] if marker in homed else "",
                crate_class=crated.get(marker, ""),
                displaced=1 if marker in displaced_ids else 0,
                scheduled=1 if marker in booked else 0,
                install_position=sequence.index(marker),
                setup_seconds=self._setup_seconds(marker),
                insurance_cents=self._insurance_cents(marker),
            ))

        canonical = (dict(
            object_id=marker,
            lender_id=self.items[marker]["lender_id"],
            material_code=self.items[marker]["material_code"],
            version=self.items[marker]["version"],
            declared_value_cents=self.items[marker]["declared_value"] * 100,
            mass_g=self._mass_grams(marker),
        ) for marker in self.order)

        art = {}
        art["canonical_objects.jsonl"] = _emit_lines(canonical)
        art["normalized_geometry.jsonl"] = _emit_lines(normalized)
        art["admissibility_matrix.jsonl"] = _emit_lines(admissibility)
        art["bay_candidate_matrix.jsonl"] = _emit_lines(candidates)
        art["selected_placement.jsonl"] = _emit_lines(selected)
        art["rejected_placement.jsonl"] = _emit_lines(rejected)
        art["conservation_adjusted.jsonl"] = _emit_lines(adjusted)
        art["crate_candidate_matrix.jsonl"] = _emit_lines(crate_candidates)
        art["crate_assignment.jsonl"] = _emit_lines(crate_assignment)
        art["crate_capacity_ledger.jsonl"] = _emit_lines(crate_ledger)
        art["crate_contention_report.jsonl"] = _emit_lines(contention)
        art["transport_leg_manifest.jsonl"] = _emit_lines(transport_rows)
        art["dependency_edges.jsonl"] = _emit_lines(surviving)
        art["cycle_resolution_audit.jsonl"] = _emit_lines(broken_edges)
        art["install_order.jsonl"] = _emit_lines(order_rows)
        art["crew_requirement_ledger.jsonl"] = _emit_lines(crew_rows)
        art["install_window_table.jsonl"] = _emit_lines(window_rows)
        art["unschedulable_report.jsonl"] = _emit_lines(unscheduled)
        art["gallery_occupancy_map.jsonl"] = _emit_lines(gallery_rows)
        art["deterministic_audit.jsonl"] = _emit_lines(audit_rows)
        art["penalty_summary.json"] = _emit_doc(dict(
            unschedulable_penalty=unschedulable_cost,
            contention_penalty=contention_cost,
            cycles_broken=break_count,
            total_insurance_cents=insurance_sum,
        ))
        art["objective_tuple.json"] = _emit_doc(dict(
            objective=objective_vec,
            order=self.objective_spec["order"],
        ))

        digests = {}
        for name in sorted(art):
            digests[name] = sha256(art[name].encode("utf-8")).hexdigest()
        digests["plan.py"] = sha256(program_bytes).hexdigest()
        art["integrity_manifest.json"] = _emit_doc(digests)

        running = ZERO_CHAIN
        for name in sorted(digests):
            running = sha256((running + digests[name]).encode("utf-8")).hexdigest()
        leftover = self._detect_cycle(list(self.order), surviving)
        art["final_state.json"] = _emit_doc(dict(
            chain=running,
            repair_converged=leftover is None,
            cycle_edges_removed=break_count,
            final_cycle_count=0 if leftover is None else 1,
            displacement_count=len(contention),
            unschedulable_count=len(unscheduled),
            placed_objects=scheduled_count,
            total_setup_seconds=setup_sum,
            total_insurance_cents=insurance_sum,
            objective=objective_vec,
        ))
        return art


def generate(data_dir, out_dir, program_bytes=b""):
    dossier = TouringDossier(data_dir)
    art = dossier.build(program_bytes)
    destination = Path(out_dir)
    destination.mkdir(parents=True, exist_ok=True)
    for name, payload in art.items():
        (destination / name).write_text(payload, newline="", encoding="utf-8")
    return art


def main(argv):
    if len(argv) != 3:
        raise SystemExit("usage: plan.py <data_dir> <out_dir>")
    _, data_dir, out_dir = argv
    generate(data_dir, out_dir, Path(__file__).read_bytes())


if __name__ == "__main__":
    main(sys.argv)
