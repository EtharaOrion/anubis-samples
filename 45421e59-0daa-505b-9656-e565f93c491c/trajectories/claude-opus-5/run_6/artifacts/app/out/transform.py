#!/usr/bin/env python3
"""Fault-ledger reconstruction for the crash-reporter fleet.

Usage:  python3 transform.py [DATA_DIR] [OUT_DIR]

Reads the crash reporter dump export, the runtime probe export, the uplink
export and the five reference tables from DATA_DIR; settles the nine withheld
wire parameters against the published control-slice figures; then writes the
whole triage set into OUT_DIR.

Standard library only.  All clock, byte and residency arithmetic is carried in
Python integers.
"""

import csv
import json
import os
import sys

# --------------------------------------------------------------------------
# constants fixed by the written rules
# --------------------------------------------------------------------------

TICKS_PER_BUILD = 900000000          # 1000 ticks/ms * 900000 ms
CHECK_MODULUS = 65536
SIG_HEX_LEN = 16
SIG_ROTATION_MODULUS = 16
COUNT_BIAS_MODULUS = 9
SIGNATURE_SUM_MODULUS = 1000000007
RADIX_DIGITS = '0123456789abcdefghijklmnopqrstuvwxyz'
HEX_DIGITS = set('0123456789abcdef')

# dump flag word
F_SPAN_DIFF = 0x001
F_ENTRY_SUM = 0x002
F_UNWIND_DRIFT = 0x004
F_ROTATED = 0x008
F_COUNT_BIAS = 0x010
F_ALT_RADIX = 0x020
F_ALIAS = 0x100
F_SPLIT_BASE = 0x800

# frame flag word
FF_INDEX_IN_SYMBOL = 0x040
FF_SIG_ROTATED = 0x080
FF_ALLOC_COARSE = 0x200
FF_SIG_RING = 0x400

REJECTION_KINDS = ('outside_support', 'retired_build', 'unprobed_frame',
                   'unreported_dump', 'unresolved_module')

# candidate domains published with the control slice
CANDIDATE_DOMAINS = {
    'entry_clock_modulus': [1 << e for e in range(22, 35)],
    'drift_period': [48, 96, 192, 384, 768],
    'rotation_offset': list(range(0, 8)),
    'count_multiplier': list(range(0, 9)),
    'alternate_radix': list(range(11, 36)),
    'signature_offset': list(range(0, 8)),
    'coarse_unit': [64, 128, 256, 512, 1024],
    'ring_width': [4, 8, 12, 16],
    'base_shift_width': [8, 10, 12, 16],
}

WIRE_CONTROLS = ('CTRL-A', 'CTRL-B', 'CTRL-C', 'CTRL-D', 'CTRL-E', 'CTRL-F',
                 'CTRL-G', 'CTRL-H')
CORPUS_CONTROLS = ('CTRL-I', 'CTRL-J', 'CTRL-K')

# which withheld parameters each published control figure reads through.  A
# control can only falsify a candidate once every other parameter it reads
# through is either settled or swept over.
CONTROL_DEPENDS = {
    'CTRL-A': ('count_multiplier',),
    'CTRL-H': ('count_multiplier',),
    'CTRL-C': ('count_multiplier', 'rotation_offset'),
    'CTRL-D': ('count_multiplier', 'rotation_offset', 'entry_clock_modulus'),
    'CTRL-E': ('count_multiplier', 'rotation_offset', 'drift_period'),
    'CTRL-B': ('count_multiplier', 'alternate_radix', 'base_shift_width'),
    'CTRL-F': ('count_multiplier', 'coarse_unit'),
    'CTRL-G': ('count_multiplier', 'rotation_offset', 'signature_offset',
               'ring_width'),
    'CTRL-I': tuple(CANDIDATE_DOMAINS),
    'CTRL-J': tuple(CANDIDATE_DOMAINS),
    'CTRL-K': tuple(CANDIDATE_DOMAINS),
}

# the order in which the parameters bite, each with the controls that can
# falsify one of its candidates, most specific first.  The first control that
# leaves exactly one survivor is the one recorded against the rejections.
SEARCH_STAGES = [
    ('count_multiplier',    ('CTRL-A', 'CTRL-H', 'CTRL-C', 'CTRL-F', 'CTRL-B')),
    ('rotation_offset',     ('CTRL-C', 'CTRL-D', 'CTRL-E', 'CTRL-G')),
    ('entry_clock_modulus', ('CTRL-D',)),
    ('drift_period',        ('CTRL-E',)),
    ('alternate_radix',     ('CTRL-B',)),
    ('base_shift_width',    ('CTRL-B',)),
    ('coarse_unit',         ('CTRL-F',)),
    ('signature_offset',    ('CTRL-G',)),
    ('ring_width',          ('CTRL-G',)),
]

# cap on how many joint assignments one control may be swept over for a single
# candidate, so an unexpectedly weak control cannot blow the search up
SWEEP_BUDGET = 200000


# --------------------------------------------------------------------------
# reporter export: parsing and decoding
# --------------------------------------------------------------------------

def parse_export(path):
    """Split the pipe-delimited export into dumps with their raw lines."""
    dumps = []
    cur = None
    with open(path, 'r', encoding='utf-8') as fh:
        for raw in fh:
            line = raw.rstrip('\n').rstrip('\r')
            if not line or line.startswith('#'):
                continue
            cols = line.split('|')
            if cols[0] == '@':
                cur = {
                    'dump_id': cols[1],
                    'module_col': cols[2],
                    'declared': int(cols[3]),
                    'base_field': cols[4],
                    'flags': int(cols[5], 16),
                    'key': int(cols[6]),
                    'check': int(cols[7], 16),
                    'lines': [],
                }
                dumps.append(cur)
            else:
                if cur is None:
                    raise ValueError('fault frame line before any dump header')
                cur['lines'].append(cols)
    return dumps


def frame_check_total(cols):
    """Sum of the character codes of the eight preceding columns, mod 65536."""
    return sum(ord(ch) for ch in '|'.join(cols[:8])) % CHECK_MODULUS


def parse_in_radix(text, radix):
    value = 0
    for ch in text:
        digit = RADIX_DIGITS.find(ch)
        if digit < 0 or digit >= radix:
            return None
        value = value * radix + digit
    return value


def rotate_left(text, k):
    k %= len(text)
    return text[k:] + text[:k]


def decode_export(dumps, params):
    """Recover every field of every fault frame under a parameter assignment.

    Never raises on a wrong assignment: fields that cannot be recovered come
    back as None so the control figures they feed simply fail to reproduce.
    Returns (dump_records, frames) or None when the dump layout itself is
    inconsistent (which is what settles the count multiplier).
    """
    count_multiplier = params['count_multiplier']
    rotation_offset = params['rotation_offset']
    entry_modulus = params['entry_clock_modulus']
    drift_period = params['drift_period']
    alt_radix = params['alternate_radix']
    shift_width = params['base_shift_width']
    coarse_unit = params['coarse_unit']
    sig_offset = params['signature_offset']
    ring_width = params['ring_width']

    records = []
    for ordinal, dump in enumerate(dumps, 1):
        flags = dump['flags']

        # --- how many of the dump's lines are fault frames
        count = dump['declared']
        if flags & F_COUNT_BIAS:
            count -= (ordinal * count_multiplier) % COUNT_BIAS_MODULUS
        if count < 1 or count > len(dump['lines']):
            return None
        frames = dump['lines'][:count]
        trailers = dump['lines'][count:]
        if (flags & F_SPLIT_BASE) and not trailers:
            return None

        # --- the shipped check totals, against the columns they cover
        running_check = 0
        for cols in frames:
            if len(cols) != 9:
                return None
            if '%04x' % frame_check_total(cols) != cols[8]:
                return None
            running_check = (running_check + int(cols[8], 16)) % CHECK_MODULUS
        if running_check != dump['check']:
            return None

        # --- authoritative frame indices
        indices = []
        for cols in frames:
            fflags = int(cols[6], 16)
            if fflags & FF_INDEX_IN_SYMBOL:
                indices.append(int(cols[7].rsplit(':', 1)[1]))
            else:
                indices.append(int(cols[0]))

        # --- logical order (the written lines are the logical ones rotated left)
        if flags & F_ROTATED:
            k = (dump['key'] + rotation_offset) % count
            order = [(m - k) % count for m in range(count)]
        else:
            order = list(range(count))
        lframes = [frames[i] for i in order]
        lindices = [indices[i] for i in order]

        # --- resident spans
        column = [int(cols[3]) for cols in lframes]
        if flags & F_SPAN_DIFF:
            spans = []
            delta = 0
            for i, value in enumerate(column):
                if i == 0:
                    spans.append(value)
                else:
                    delta = value if i == 1 else delta + value
                    spans.append(spans[-1] + delta)
        else:
            spans = list(column)

        # --- entry stamps
        column = [int(cols[1]) for cols in lframes]
        if flags & F_ENTRY_SUM:
            entries = [column[0]]
            for i in range(1, count):
                entries.append((column[i] - entries[-1]) % entry_modulus)
        else:
            entries = list(column)

        # --- dump base
        field = dump['base_field']
        if flags & F_ALT_RADIX:
            high = parse_in_radix(field, alt_radix)
        else:
            high = int(field)
        base = None
        if high is not None:
            if flags & F_SPLIT_BASE:
                low = int(trailers[0][0])
                if 0 <= low < (1 << shift_width):
                    base = (high << shift_width) | low
            else:
                base = high
            if base is not None and base < 0:
                base = None

        records.append({
            'dump': dump, 'ordinal': ordinal, 'count': count,
            'frames': lframes, 'indices': lindices, 'spans': spans,
            'entries': entries, 'base': base, 'trailers': trailers,
        })

    # --- second pass: state that advances over the export as a whole
    resident_running = 0
    ring = [None] * ring_width
    ring_pos = 0
    ring_filled = 0
    out_frames = []
    for rec in records:
        flags = rec['dump']['flags']
        for i in range(rec['count']):
            cols = rec['frames'][i]
            fflags = int(cols[6], 16)

            unwind = int(cols[2])
            if flags & F_UNWIND_DRIFT:
                unwind -= resident_running % drift_period
            resident_running += rec['spans'][i]

            column = cols[5]
            if fflags & FF_SIG_RING:
                distance = int(column[1:])
                signature = None
                if 1 <= distance <= ring_filled:
                    signature = ring[(ring_pos - distance) % ring_width]
            elif fflags & FF_SIG_ROTATED:
                shift = (rec['indices'][i] + sig_offset) % SIG_ROTATION_MODULUS
                signature = rotate_left(column, -shift)
            else:
                signature = column
            if signature is not None and (
                    len(signature) != SIG_HEX_LEN
                    or any(ch not in HEX_DIGITS for ch in signature)):
                signature = None
            ring[ring_pos] = signature
            ring_pos = (ring_pos + 1) % ring_width
            ring_filled += 1

            column = cols[4]
            if fflags & FF_ALLOC_COARSE:
                allocated = int(column.lstrip('~')) * coarse_unit
            else:
                allocated = int(column)

            entry = rec['entries'][i]
            capture = None
            if rec['base'] is not None:
                capture = rec['base'] + ((entry - rec['entries'][0]) % entry_modulus)

            out_frames.append({
                'source_dump': rec['dump']['dump_id'],
                'module_col': rec['dump']['module_col'],
                'frame_index': rec['indices'][i],
                'enter_ns': entry,
                'unwind_ns': unwind,
                'cost_ns': rec['spans'][i],
                'bytes_allocated': allocated,
                'fault_signature': signature,
                'symbol': cols[7].rsplit(':', 1)[0],
                'capture_tick': capture,
            })
    return records, out_frames


def wire_controls(decoded):
    """The eight control figures that read straight off the decoded export."""
    if decoded is None:
        return {name: None for name in WIRE_CONTROLS}
    records, frames = decoded
    bases = [rec['base'] for rec in records]
    sigs = [fr['fault_signature'] for fr in frames]
    return {
        'CTRL-A': len(frames),
        'CTRL-B': (None if any(b is None for b in bases) else sum(bases)),
        'CTRL-C': sum(fr['cost_ns'] for fr in frames),
        'CTRL-D': sum(fr['enter_ns'] for fr in frames),
        'CTRL-E': sum(fr['unwind_ns'] for fr in frames),
        'CTRL-F': sum(fr['bytes_allocated'] for fr in frames),
        'CTRL-G': (None if any(s is None for s in sigs)
                   else sum(int(s, 16) for s in sigs) % SIGNATURE_SUM_MODULUS),
        'CTRL-H': sum(fr['frame_index'] for fr in frames),
    }


def assert_wire_closure(decoded):
    """The invariants the notes say hold for every corpus the fleet produces."""
    records, frames = decoded
    for rec in records:
        assert rec['count'] >= 1, 'dump holds no fault frame'
        assert rec['count'] <= len(rec['dump']['lines'])
        assert rec['base'] is not None and rec['base'] >= 0, 'dump base unrecovered'
        assert all(span >= 0 for span in rec['spans']), 'resident span negative'
    for fr in frames:
        assert fr['fault_signature'] is not None, 'signature unrecovered'
        assert fr['unwind_ns'] <= fr['enter_ns'], 'unwind stamp after entry stamp'


# --------------------------------------------------------------------------
# reference tables and the side exports
# --------------------------------------------------------------------------

def read_csv_rows(path):
    with open(path, 'r', encoding='utf-8', newline='') as fh:
        return list(csv.DictReader(fh))


def read_jsonl(path):
    rows = []
    with open(path, 'r', encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


class Reference(object):
    def __init__(self, data_dir):
        self.registry = []
        for row in read_csv_rows(os.path.join(data_dir, 'build_registry.csv')):
            self.registry.append({
                'module_tag': row['module_tag'],
                'canonical_tag': row['canonical_tag'],
                'component_key': row['component_key'],
                'tier_index': int(row['tier_index']),
                'from_build': int(row['effective_from_build']),
                'to_build': int(row['effective_to_build']),
                'status': row['status'],
            })
        self.by_tag = {}
        for row in self.registry:
            self.by_tag.setdefault(row['module_tag'], []).append(row)

        self.ladder = {}
        for row in read_csv_rows(os.path.join(data_dir, 'severity_ladder.csv')):
            self.ladder.setdefault(row['component_key'], {})[int(row['tier_index'])] = \
                int(row['budget_ns'])

        self.windows = {}
        for row in read_csv_rows(os.path.join(data_dir, 'support_windows.csv')):
            self.windows[row['component_key']] = (int(row['open_tick']),
                                                  int(row['close_tick']))

        self.host_zone = {}
        for row in read_csv_rows(os.path.join(data_dir, 'host_zones.csv')):
            self.host_zone[row['host_id']] = row['zone']
        self.zones = sorted(set(self.host_zone.values()))

        self.tenant_segment = {}
        for row in read_csv_rows(os.path.join(data_dir, 'tenant_classes.csv')):
            self.tenant_segment[row['tenant_class']] = row['tenant_segment']
        self.segments = sorted(set(self.tenant_segment.values()))

        self._resolved = {}

    def resolve(self, tag, build_index):
        """Walk the registry to the terminal row covering this build index."""
        if (tag, build_index) in self._resolved:
            return self._resolved[(tag, build_index)]
        seen = set()
        cursor = tag
        result = None
        while True:
            if cursor in seen:
                break
            seen.add(cursor)
            rows = [r for r in self.by_tag.get(cursor, [])
                    if r['from_build'] <= build_index <= r['to_build']]
            if len(rows) != 1:
                break
            row = rows[0]
            if row['canonical_tag'] == cursor:
                result = row
                break
            cursor = row['canonical_tag']
        self._resolved[(tag, build_index)] = result
        return result


def load_corpus_sides(data_dir, prefix):
    probe = {}
    for row in read_csv_rows(os.path.join(data_dir, prefix + 'runtime_probe.csv')):
        probe[(row['module_tag'], int(row['frame_index']))] = {
            'samples': int(row['samples']),
            'peak_ns': int(row['peak_ns']),
        }
    uplink = {}
    for row in read_jsonl(os.path.join(data_dir, prefix + 'uplink_reports.jsonl')):
        uplink[(row['module_tag'], int(row['frame_index']))] = {
            'host_id': row['host_id'],
            'tenant_class': row['tenant_class'],
            'bytes_uploaded': int(row['bytes_uploaded']),
        }
    return probe, uplink


# --------------------------------------------------------------------------
# the fault ledger
# --------------------------------------------------------------------------

def build_ledger(frames, probe, uplink, ref):
    """Screen every fault frame; return accepted rows plus rejection tallies."""
    rejections = dict((kind, 0) for kind in REJECTION_KINDS)
    rejected_total = 0
    accepted = []

    for fr in frames:
        build_index = fr['capture_tick'] // TICKS_PER_BUILD
        row = ref.resolve(fr['module_col'], build_index)
        broken = []

        # A frame that does not resolve onto a still-carried module has no
        # module identity to check the remaining conditions against, so the
        # identity verdict stands alone.  The other three are independent and
        # a frame breaking several is counted once against each.
        if row is None:
            broken.append('unresolved_module')
        elif row['status'] != 'supported':
            broken.append('retired_build')
        else:
            module_tag = row['canonical_tag']
            component = row['component_key']
            open_tick, close_tick = ref.windows[component]
            if not (open_tick < fr['capture_tick'] < close_tick):
                broken.append('outside_support')
            key = (module_tag, fr['frame_index'])
            if key not in probe:
                broken.append('unprobed_frame')
            if key not in uplink:
                broken.append('unreported_dump')

        if broken:
            for kind in broken:
                rejections[kind] += 1
            rejected_total += 1
            continue

        key = (module_tag, fr['frame_index'])
        measured = probe[key]
        report = uplink[key]
        accepted.append({
            'bytes_allocated': fr['bytes_allocated'],
            'bytes_uploaded': report['bytes_uploaded'],
            'capture_tick': fr['capture_tick'],
            'component_key': component,
            'cost_ns': fr['cost_ns'],
            'declared_tier': row['tier_index'],
            'enter_ns': fr['enter_ns'],
            'fault_signature': fr['fault_signature'],
            'frame_index': fr['frame_index'],
            'host_zone': ref.host_zone[report['host_id']],
            'module_tag': module_tag,
            'peak_ns': measured['peak_ns'],
            'samples': measured['samples'],
            'source_dump': fr['source_dump'],
            'symbol': fr['symbol'],
            'tenant_segment': ref.tenant_segment[report['tenant_class']],
            'unwind_ns': fr['unwind_ns'],
        })

    accepted.sort(key=lambda r: r['capture_tick'])
    return accepted, rejections, rejected_total


# --------------------------------------------------------------------------
# quarantine settlement
# --------------------------------------------------------------------------

def settle(accepted, ref):
    """Highest effective-tier assignment under which no module above tier zero
    counts more frames over budget than within budget."""
    modules = {}
    for row in accepted:
        entry = modules.get(row['module_tag'])
        if entry is None:
            entry = modules[row['module_tag']] = {
                'component_key': row['component_key'],
                'declared_tier': row['declared_tier'],
                'peaks': [],
            }
        entry['peaks'].append(row['peak_ns'])

    effective = dict((tag, m['declared_tier']) for tag, m in modules.items())

    def allowances(effective):
        envelopes = {}
        for tag, m in modules.items():
            comp = m['component_key']
            budget = ref.ladder[comp][effective[tag]]
            if comp not in envelopes or budget < envelopes[comp]:
                envelopes[comp] = budget
        for comp in list(envelopes):
            envelopes[comp] *= len(ref.ladder[comp])
        out = {}
        for tag, m in modules.items():
            comp = m['component_key']
            out[tag] = min(ref.ladder[comp][effective[tag]], envelopes[comp])
        return out

    passes = 0
    allowance = allowances(effective)
    while True:
        allowance = allowances(effective)
        demote = []
        for tag, m in modules.items():
            within = sum(1 for p in m['peaks'] if p <= allowance[tag])
            if effective[tag] > 0 and (len(m['peaks']) - within) > within:
                demote.append(tag)
        passes += 1
        if not demote:
            break
        for tag in demote:
            effective[tag] -= 1

    quarantined = []
    for tag, m in modules.items():
        within = sum(1 for p in m['peaks'] if p <= allowance[tag])
        if effective[tag] == 0 and (len(m['peaks']) - within) > within:
            quarantined.append(tag)

    for row in accepted:
        tag = row['module_tag']
        row['effective_tier'] = effective[tag]
        row['within_budget'] = row['peak_ns'] <= allowance[tag]

    return {
        'modules': modules,
        'effective': effective,
        'allowance': allowance,
        'passes': passes,
        'quarantined': sorted(quarantined),
    }


def run_corpus(frames, probe, uplink, ref):
    accepted, rejections, rejected_total = build_ledger(frames, probe, uplink, ref)
    settlement = settle(accepted, ref)
    allocated = sum(r['bytes_allocated'] for r in accepted)
    uploaded = sum(r['bytes_uploaded'] for r in accepted)
    return {
        'frames': frames,
        'accepted': accepted,
        'rejections': rejections,
        'rejected_total': rejected_total,
        'settlement': settlement,
        'allocated_bytes': allocated,
        'uploaded_bytes': uploaded,
        'residual_bytes': allocated - uploaded,
    }


# --------------------------------------------------------------------------
# parameter settlement against the published control figures
# --------------------------------------------------------------------------

def corpus_controls(decoded, probe, uplink, ref):
    """CTRL-I, CTRL-J and CTRL-K: the figures that need the whole derivation."""
    try:
        result = run_corpus(decoded[1], probe, uplink, ref)
    except (KeyError, AssertionError, TypeError, ValueError):
        return dict((name, None) for name in CORPUS_CONTROLS)
    return {
        'CTRL-I': len(result['accepted']),
        'CTRL-J': len(result['settlement']['quarantined']),
        'CTRL-K': result['residual_bytes'],
    }


def _sweep(partners):
    """Every joint assignment of a list of (name, domain) pairs."""
    combos = [{}]
    for pname, domain in partners:
        combos = [dict(c, **{pname: v}) for c in combos for v in domain]
    return combos


def settle_parameters(cal_dumps, probe, uplink, ref, published):
    """Sweep every candidate value of every withheld parameter against the
    published control figures, recording a verdict for the whole domain before
    the parameter's value is selected.

    A candidate survives a control when some assignment of the parameters that
    control also reads through - the ones not yet settled - reproduces the
    published figure.  The first control that leaves exactly one survivor
    selects the value and is recorded as the falsifier of the rest.
    """
    settled = {}
    baseline = dict((name, CANDIDATE_DOMAINS[name][0]) for name in CANDIDATE_DOMAINS)
    report = []
    evaluations = [0]

    def survivors_under(name, control):
        """Which candidates of `name` this control leaves standing."""
        others = [p for p in CONTROL_DEPENDS[control]
                  if p != name and p not in settled]
        partners = [(p, CANDIDATE_DOMAINS[p]) for p in others]
        size = len(CANDIDATE_DOMAINS[name])
        for _, domain in partners:
            size *= len(domain)
        if size > SWEEP_BUDGET:
            return None
        needs_corpus = control in CORPUS_CONTROLS
        keep = set()
        for value in CANDIDATE_DOMAINS[name]:
            for combo in _sweep(partners):
                assignment = dict(baseline)
                assignment.update(settled)
                assignment.update(combo)
                assignment[name] = value
                evaluations[0] += 1
                decoded = decode_export(cal_dumps, assignment)
                got = wire_controls(decoded)
                if needs_corpus and decoded is not None:
                    got.update(corpus_controls(decoded, probe, uplink, ref))
                if got.get(control) is not None and got[control] == published[control]:
                    keep.add(value)
                    break
        return keep

    for name, controls in SEARCH_STAGES:
        domain = CANDIDATE_DOMAINS[name]
        chosen_control = None
        survivors = None
        best = None
        # try each control in turn until one isolates a single survivor; the
        # corpus-level figures are the last resort
        for control in tuple(controls) + CORPUS_CONTROLS:
            keep = survivors_under(name, control)
            if keep is None:
                continue
            if best is None or len(keep) < len(best[1]):
                best = (control, keep)
            if len(keep) == 1:
                chosen_control, survivors = control, keep
                break
        if survivors is None and best is not None:
            chosen_control, survivors = best
        if survivors is None or len(survivors) != 1:
            raise RuntimeError('parameter %s did not settle: %r'
                               % (name, sorted(survivors or ())))

        selected = sorted(survivors)[0]
        settled[name] = selected
        report.append({
            'name': name,
            'candidates': len(domain),
            'control': chosen_control,
            'selected': selected,
            'rejected': [{'value': v, 'control': chosen_control}
                         for v in domain if v != selected],
        })

    return settled, {'evaluations': evaluations[0], 'parameters': report}


# --------------------------------------------------------------------------
# report shapes
# --------------------------------------------------------------------------

def group_report(rows, group_key, group_value, cross_key, cross_name):
    by_component = {}
    by_cross = {}
    modules = set()
    total_uploaded = 0
    total_cost = 0
    within = 0
    for row in rows:
        by_component[row['component_key']] = \
            by_component.get(row['component_key'], 0) + row['bytes_uploaded']
        by_cross[row[cross_key]] = by_cross.get(row[cross_key], 0) + row['bytes_uploaded']
        modules.add(row['module_tag'])
        total_uploaded += row['bytes_uploaded']
        total_cost += row['cost_ns']
        if row['within_budget']:
            within += 1
    return {
        'accepted_count': len(rows),
        'by_component': by_component,
        cross_name: by_cross,
        'modules': sorted(modules),
        group_key: group_value,
        'total_bytes_uploaded': total_uploaded,
        'total_cost_ns': total_cost,
        'within_budget_count': within,
    }


def build_reports(result, ref):
    accepted = result['accepted']
    reports = {}

    by_component = {}
    by_zone = {}
    by_segment = {}
    by_tier = {}
    total_alloc = 0
    total_uploaded = 0
    total_cost = 0
    within = 0
    for row in accepted:
        by_component[row['component_key']] = \
            by_component.get(row['component_key'], 0) + row['bytes_uploaded']
        by_zone[row['host_zone']] = by_zone.get(row['host_zone'], 0) + row['bytes_uploaded']
        by_segment[row['tenant_segment']] = \
            by_segment.get(row['tenant_segment'], 0) + row['bytes_uploaded']
        key = str(row['effective_tier'])
        by_tier[key] = by_tier.get(key, 0) + 1
        total_alloc += row['bytes_allocated']
        total_uploaded += row['bytes_uploaded']
        total_cost += row['cost_ns']
        if row['within_budget']:
            within += 1

    reports['summary.json'] = {
        'accepted_count': len(accepted),
        'by_component': by_component,
        'by_effective_tier': by_tier,
        'by_tenant_segment': by_segment,
        'by_zone': by_zone,
        'rejected_count': result['rejected_total'],
        'total_bytes_allocated': total_alloc,
        'total_bytes_uploaded': total_uploaded,
        'total_cost_ns': total_cost,
        'within_budget_count': within,
    }

    settlement = result['settlement']
    modules = []
    for tag in sorted(settlement['modules']):
        info = settlement['modules'][tag]
        rows = [r for r in accepted if r['module_tag'] == tag]
        allowance = settlement['allowance'][tag]
        eff = settlement['effective'][tag]
        modules.append({
            'accepted_count': len(rows),
            'allowance_ns': allowance,
            'budget_ns': ref.ladder[info['component_key']][eff],
            'component_key': info['component_key'],
            'declared_tier': info['declared_tier'],
            'effective_tier': eff,
            'module_tag': tag,
            'net_samples': sum(r['samples'] for r in rows),
            'quarantined': tag in settlement['quarantined'],
            'total_bytes_uploaded': sum(r['bytes_uploaded'] for r in rows),
            'within_budget_count': sum(1 for r in rows if r['within_budget']),
        })
    reports['product_summary.json'] = {'modules': modules}

    for zone in ref.zones:
        rows = [r for r in accepted if r['host_zone'] == zone]
        reports['region_%s.json' % zone] = group_report(
            rows, 'zone', zone, 'tenant_segment', 'by_tenant_segment')
    for segment in ref.segments:
        rows = [r for r in accepted if r['tenant_segment'] == segment]
        reports['segment_%s.json' % segment] = group_report(
            rows, 'tenant_segment', segment, 'host_zone', 'by_zone')

    return reports


LEDGER_FIELDS = ('bytes_allocated', 'bytes_uploaded', 'capture_tick',
                 'component_key', 'cost_ns', 'effective_tier', 'enter_ns',
                 'fault_signature', 'frame_index', 'host_zone', 'module_tag',
                 'peak_ns', 'samples', 'source_dump', 'symbol',
                 'tenant_segment', 'unwind_ns', 'within_budget')


def write_json(path, obj):
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(obj, fh, indent=2, sort_keys=True)
        fh.write('\n')


def write_ledger(path, accepted):
    with open(path, 'w', encoding='utf-8') as fh:
        for row in accepted:
            trimmed = dict((k, row[k]) for k in LEDGER_FIELDS)
            fh.write(json.dumps(trimmed, sort_keys=True, separators=(',', ':')))
            fh.write('\n')


# --------------------------------------------------------------------------
# closure checks
# --------------------------------------------------------------------------

def assert_closure(result, reports, ref):
    accepted = result['accepted']
    summary = reports['summary.json']

    ticks = set(r['capture_tick'] for r in accepted)
    assert len(ticks) == len(accepted), 'two ledger rows share a capture tick'
    assert all(accepted[i]['capture_tick'] < accepted[i + 1]['capture_tick']
               for i in range(len(accepted) - 1)), 'ledger not increasing'
    assert summary['accepted_count'] + summary['rejected_count'] == len(result['frames'])

    zone_total = sum(reports['region_%s.json' % z]['total_bytes_uploaded']
                     for z in ref.zones)
    seg_total = sum(reports['segment_%s.json' % s]['total_bytes_uploaded']
                    for s in ref.segments)
    assert zone_total == summary['total_bytes_uploaded'], 'zone bytes do not close'
    assert seg_total == summary['total_bytes_uploaded'], 'segment bytes do not close'
    assert sum(reports['region_%s.json' % z]['accepted_count']
               for z in ref.zones) == len(accepted)
    assert sum(reports['segment_%s.json' % s]['accepted_count']
               for s in ref.segments) == len(accepted)

    reported = set(m['module_tag'] for m in reports['product_summary.json']['modules'])
    assert reported == set(r['module_tag'] for r in accepted), 'module roll-up mismatch'
    assert (result['allocated_bytes'] - result['uploaded_bytes']
            == result['residual_bytes'])


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def main(argv):
    data_dir = argv[1] if len(argv) > 1 else '/app/data'
    out_dir = argv[2] if len(argv) > 2 else '/app/out'
    os.makedirs(out_dir, exist_ok=True)

    ref = Reference(data_dir)
    with open(os.path.join(data_dir, 'control-slice-figures.json'),
              'r', encoding='utf-8') as fh:
        published = json.load(fh)

    # --- settle the withheld wire parameters against the control slice first
    cal_dumps = parse_export(os.path.join(data_dir, 'calibration_fault_frames.psv'))
    cal_probe, cal_uplink = load_corpus_sides(data_dir, 'calibration_')
    params, search = settle_parameters(cal_dumps, cal_probe, cal_uplink, ref, published)

    # --- reproduce every published control figure under the settled assignment
    cal_decoded = decode_export(cal_dumps, params)
    assert cal_decoded is not None, 'control slice does not decode'
    assert_wire_closure(cal_decoded)
    reproduced = wire_controls(cal_decoded)
    cal_result = run_corpus(cal_decoded[1], cal_probe, cal_uplink, ref)
    reproduced['CTRL-I'] = len(cal_result['accepted'])
    reproduced['CTRL-J'] = len(cal_result['settlement']['quarantined'])
    reproduced['CTRL-K'] = cal_result['residual_bytes']
    comparison = [{'control': name,
                   'published': published[name],
                   'reproduced': reproduced[name]}
                  for name in sorted(published)]
    for entry in comparison:
        assert entry['published'] == entry['reproduced'], \
            'control %s does not reproduce' % entry['control']

    # --- now the incident corpus
    dumps = parse_export(os.path.join(data_dir, 'fault_frames.psv'))
    decoded = decode_export(dumps, params)
    assert decoded is not None, 'incident corpus does not decode'
    assert_wire_closure(decoded)
    probe, uplink = load_corpus_sides(data_dir, '')
    result = run_corpus(decoded[1], probe, uplink, ref)
    reports = build_reports(result, ref)
    assert_closure(result, reports, ref)

    reports['data_quality.json'] = {
        'accepted_count': len(result['accepted']),
        'calibration_comparison': comparison,
        'candidate_search': search,
        'frame_count': len(result['frames']),
        'reconciliation': {
            'allocated_bytes': result['allocated_bytes'],
            'residual_bytes': result['residual_bytes'],
            'uploaded_bytes': result['uploaded_bytes'],
        },
        'rejections': result['rejections'],
        'settlement': {
            'passes': result['settlement']['passes'],
            'quarantined_modules': result['settlement']['quarantined'],
        },
    }

    write_ledger(os.path.join(out_dir, 'normalized.jsonl'), result['accepted'])
    for name, obj in reports.items():
        write_json(os.path.join(out_dir, name), obj)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
