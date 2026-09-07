#!/usr/bin/env python3
"""Crash-reporter triage transform.

Reconstructs a fault ledger and its roll-ups from a crash-reporter dump export
plus the fleet's reference tables, and writes the whole triage set under an
output directory.

    python3 transform.py <DATA_DIR> <OUT_DIR>

With no arguments it reads /app/data and writes /app/out.

The crash reporter holds back nine wire parameters.  They are settled first,
against the control figures published for the control-slice corpus, by
enumerating every candidate value in each published domain and recording a
verdict for it; only then is the incident corpus derived.  All clock, byte and
residency arithmetic is carried in Python integers.
"""

import csv
import json
import os
import sys

# --------------------------------------------------------------------------
# fleet constants fixed by the written rules
# --------------------------------------------------------------------------

TICKS_PER_BUILD_INDEX = 900000000      # 1000 ticks per millisecond
SIGNATURE_WIDTH = 16                   # hex characters in a fault signature
CHECK_MODULUS = 65536                  # frame and dump check-total modulus
COUNT_BIAS_MODULUS = 9                 # declared-count bias modulus
DIGITS = '0123456789abcdefghijklmnopqrstuvwxyz'

# dump flag word
DUMP_SPAN_DELTAS = 0x001
DUMP_ENTRY_CHAIN = 0x002
DUMP_UNWIND_DRIFT = 0x004
DUMP_ROTATION = 0x008
DUMP_COUNT_BIAS = 0x010
DUMP_ALT_RADIX = 0x020
DUMP_MODULE_ALIAS = 0x100
DUMP_SPLIT_BASE = 0x800

# frame flag word
FRAME_INDEX_PLACEHOLDER = 0x040
FRAME_SIG_ROTATION = 0x080
FRAME_ALLOC_COARSE = 0x200
FRAME_SIG_RING = 0x400

# rejection categories
REJECTION_KINDS = ('unresolved_module', 'retired_build', 'outside_support',
                   'unprobed_frame', 'unreported_dump')

# withheld wire parameters, in the order the wire rules name them
PARAM_ENTRY_MODULUS = 'entry clock modulus'
PARAM_DRIFT_PERIOD = 'drift period'
PARAM_ROTATION_OFFSET = 'rotation offset'
PARAM_COUNT_MULTIPLIER = 'count multiplier'
PARAM_ALTERNATE_RADIX = 'alternate radix'
PARAM_BASE_SHIFT_WIDTH = 'base shift width'
PARAM_SIGNATURE_OFFSET = 'signature offset'
PARAM_COARSE_UNIT = 'coarse unit'
PARAM_RING_WIDTH = 'ring width'

PARAMETERS = (PARAM_ENTRY_MODULUS, PARAM_DRIFT_PERIOD, PARAM_ROTATION_OFFSET,
              PARAM_COUNT_MULTIPLIER, PARAM_ALTERNATE_RADIX,
              PARAM_BASE_SHIFT_WIDTH, PARAM_SIGNATURE_OFFSET,
              PARAM_COARSE_UNIT, PARAM_RING_WIDTH)

# published candidate domains (calibration-controls.md)
PARAM_DOMAINS = {
    PARAM_ENTRY_MODULUS: [1 << k for k in range(22, 35)],
    PARAM_DRIFT_PERIOD: [48, 96, 192, 384, 768],
    PARAM_ROTATION_OFFSET: list(range(8)),
    PARAM_COUNT_MULTIPLIER: list(range(9)),
    PARAM_ALTERNATE_RADIX: list(range(11, 36)),
    PARAM_BASE_SHIFT_WIDTH: [8, 10, 12, 16],
    PARAM_SIGNATURE_OFFSET: list(range(8)),
    PARAM_COARSE_UNIT: [64, 128, 256, 512, 1024],
    PARAM_RING_WIDTH: [4, 8, 12, 16],
}

# the control figures that can settle each parameter, most direct first: the
# first that leaves exactly one candidate standing is the one recorded
PARAM_CONTROLS = {
    PARAM_COUNT_MULTIPLIER: ['CTRL-A'],
    PARAM_ROTATION_OFFSET: ['CTRL-C', 'CTRL-D', 'CTRL-E', 'CTRL-G'],
    PARAM_ENTRY_MODULUS: ['CTRL-D', 'CTRL-I', 'CTRL-K'],
    PARAM_DRIFT_PERIOD: ['CTRL-E', 'CTRL-I', 'CTRL-K'],
    PARAM_COARSE_UNIT: ['CTRL-F', 'CTRL-K', 'CTRL-I'],
    PARAM_ALTERNATE_RADIX: ['CTRL-B', 'CTRL-K', 'CTRL-I'],
    PARAM_BASE_SHIFT_WIDTH: ['CTRL-B', 'CTRL-K', 'CTRL-I'],
    PARAM_SIGNATURE_OFFSET: ['CTRL-G', 'CTRL-I'],
    PARAM_RING_WIDTH: ['CTRL-G', 'CTRL-I'],
}


class DecodeError(Exception):
    """A candidate parameter assignment cannot read the export."""


# --------------------------------------------------------------------------
# reference tables
# --------------------------------------------------------------------------

class Reference(object):
    def __init__(self, data_dir):
        self.registry = self._rows(data_dir, 'build_registry.csv')
        self.ladder = self._rows(data_dir, 'severity_ladder.csv')
        self.windows = self._rows(data_dir, 'support_windows.csv')
        self.host_zones = self._rows(data_dir, 'host_zones.csv')
        self.tenant_segments = self._rows(data_dir, 'tenant_classes.csv')
        self.zone_of = {r['host_id']: r['zone'] for r in self.host_zones}
        self.segment_of = {r['tenant_class']: r['tenant_segment']
                           for r in self.tenant_segments}
        self.zones = sorted({r['zone'] for r in self.host_zones})
        self.segments = sorted({r['tenant_segment'] for r in self.tenant_segments})
        self.window_of = {r['component_key']: (int(r['open_tick']), int(r['close_tick']))
                          for r in self.windows}
        self.ladder_of = {}
        for r in self.ladder:
            self.ladder_of.setdefault(r['component_key'], {})[int(r['tier_index'])] = \
                int(r['budget_ns'])
        self.registry_rows = {}
        for r in self.registry:
            self.registry_rows.setdefault(r['module_tag'], []).append(r)

    @staticmethod
    def _rows(data_dir, name):
        with open(os.path.join(data_dir, name)) as fh:
            return [dict(row) for row in csv.DictReader(fh)]

    def resolve(self, tag, build_index):
        """Follow the registry chain; None when the module stays unresolved."""
        seen = set()
        while True:
            if tag in seen:
                return None
            seen.add(tag)
            rows = [r for r in self.registry_rows.get(tag, ())
                    if int(r['effective_from_build']) <= build_index
                    <= int(r['effective_to_build'])]
            if len(rows) != 1:
                return None
            row = rows[0]
            if row['canonical_tag'] == row['module_tag']:
                return row
            tag = row['canonical_tag']


# --------------------------------------------------------------------------
# dump export reading
# --------------------------------------------------------------------------

def read_dumps(path):
    dumps = []
    current = None
    with open(path) as fh:
        for raw in fh:
            line = raw.rstrip('\n')
            if not line.strip() or line.startswith('#'):
                continue
            columns = line.split('|')
            if columns[0] == '@':
                current = {
                    'dump_id': columns[1],
                    'module': columns[2],
                    'declared_count': int(columns[3]),
                    'base_field': columns[4],
                    'flags': int(columns[5], 16),
                    'key': int(columns[6]),
                    'check': columns[7],
                    'lines': [],
                }
                dumps.append(current)
            else:
                current['lines'].append(columns)
    return dumps


def frame_check_total(columns):
    return sum(ord(ch) for ch in '|'.join(columns[:8])) % CHECK_MODULUS


def parse_radix_numeral(text, radix):
    value = 0
    for ch in text:
        digit = DIGITS.find(ch)
        if digit < 0 or digit >= radix:
            raise DecodeError('digit %r outside radix %d' % (ch, radix))
        value = value * radix + digit
    return value


def rotate_left(text, n):
    n %= len(text)
    return text[n:] + text[:n]


class WireParameters(object):
    __slots__ = ('entry_modulus', 'drift_period', 'rotation_offset',
                 'count_multiplier', 'alternate_radix', 'base_shift_width',
                 'signature_offset', 'coarse_unit', 'ring_width')

    def __init__(self, **kwargs):
        for name in PARAMETERS:
            setattr(self, self.attr(name), kwargs[name])

    @staticmethod
    def attr(name):
        return {'entry clock modulus': 'entry_modulus',
                'drift period': 'drift_period',
                'rotation offset': 'rotation_offset',
                'count multiplier': 'count_multiplier',
                'alternate radix': 'alternate_radix',
                'base shift width': 'base_shift_width',
                'signature offset': 'signature_offset',
                'coarse unit': 'coarse_unit',
                'ring width': 'ring_width'}[name]

    def get(self, name):
        return getattr(self, self.attr(name))


def decode_export(path, params):
    """Read one dump export into recovered dump and frame records.

    Frames are recovered in the export's logical sequence: dumps in the order
    written, and within a dump the fault frames in logical order (the export
    order un-rotated).  The running resident-span total and the fault-signature
    ring advance over that whole sequence.
    """
    dumps = read_dumps(path)
    ring = [None] * params.ring_width
    ring_position = 0
    span_running_total = 0
    signatures_recovered = True

    for ordinal, dump in enumerate(dumps, 1):
        flags = dump['flags']
        bias = (ordinal * params.count_multiplier) % COUNT_BIAS_MODULUS \
            if flags & DUMP_COUNT_BIAS else 0
        count = dump['declared_count'] - bias
        if count < 1 or count > len(dump['lines']):
            raise DecodeError('dump %s declares %d frames over %d lines'
                              % (dump['dump_id'], count, len(dump['lines'])))
        frame_lines = dump['lines'][:count]
        trailer_lines = dump['lines'][count:]

        turn = (dump['key'] + params.rotation_offset) % count \
            if flags & DUMP_ROTATION else 0
        logical_lines = (frame_lines[count - turn:] + frame_lines[:count - turn]
                         if turn else list(frame_lines))

        high = (parse_radix_numeral(dump['base_field'], params.alternate_radix)
                if flags & DUMP_ALT_RADIX else int(dump['base_field']))
        if flags & DUMP_SPLIT_BASE:
            if not trailer_lines:
                raise DecodeError('dump %s splits its base but carries no trailer'
                                  % dump['dump_id'])
            base = (high << params.base_shift_width) | int(trailer_lines[0][0])
        else:
            base = high

        records = []
        previous_entry = None
        previous_span = None
        previous_previous_span = None
        for position, columns in enumerate(logical_lines):
            frame_flags = int(columns[6], 16)
            symbol_column = columns[7]
            kind, _, numeral = symbol_column.rpartition(':')
            frame_index = (int(numeral) if frame_flags & FRAME_INDEX_PLACEHOLDER
                           else int(columns[0]))

            if flags & DUMP_ENTRY_CHAIN and position:
                entry_stamp = (int(columns[1]) - previous_entry) % params.entry_modulus
            else:
                entry_stamp = int(columns[1])

            written_span = int(columns[3])
            if flags & DUMP_SPAN_DELTAS:
                if previous_span is None:
                    resident_span = written_span
                elif previous_previous_span is None:
                    resident_span = written_span + previous_span
                else:
                    resident_span = (written_span + 2 * previous_span
                                     - previous_previous_span)
            else:
                resident_span = written_span

            allocation_column = columns[4]
            if frame_flags & FRAME_ALLOC_COARSE:
                if not allocation_column.startswith('~'):
                    raise DecodeError('frame allocation lacks its coarse marker')
                allocated_bytes = int(allocation_column[1:]) * params.coarse_unit
            else:
                if allocation_column.startswith('~'):
                    raise DecodeError('frame allocation carries an unmarked coarse value')
                allocated_bytes = int(allocation_column)

            records.append({
                'source_dump': dump['dump_id'],
                'module_tag_written': dump['module'],
                'frame_index': frame_index,
                'entry_stamp': entry_stamp,
                'unwind_written': int(columns[2]),
                'resident_span': resident_span,
                'allocated_bytes': allocated_bytes,
                'signature_column': columns[5],
                'frame_flags': frame_flags,
                'symbol': kind,
            })
            previous_entry = entry_stamp
            previous_previous_span, previous_span = previous_span, resident_span

        # the dump's first fault frame, in logical order, anchors the clock
        first_entry = records[0]['entry_stamp'] if records else 0

        for record in records:
            record['capture_tick'] = base + (record['entry_stamp'] - first_entry) \
                % params.entry_modulus
            record['build_index'] = record['capture_tick'] // TICKS_PER_BUILD_INDEX

            if flags & DUMP_UNWIND_DRIFT:
                record['unwind_stamp'] = (record['unwind_written']
                                          - span_running_total % params.drift_period)
            else:
                record['unwind_stamp'] = record['unwind_written']

            column = record['signature_column']
            if record['frame_flags'] & FRAME_SIG_RING:
                if not column.startswith('^'):
                    raise DecodeError('frame signature lacks its ring marker')
                slot = (ring_position - int(column[1:])) % params.ring_width
                signature = ring[slot]
                # a slot nothing has occupied yet falsifies this assignment, but
                # only for the figures that read fault signatures
                signatures_recovered = signatures_recovered and signature is not None
            else:
                if column.startswith('^'):
                    raise DecodeError('frame signature carries an unmarked ring distance')
                if record['frame_flags'] & FRAME_SIG_ROTATION:
                    turn_back = (SIGNATURE_WIDTH
                                 - (record['frame_index'] + params.signature_offset)
                                 % SIGNATURE_WIDTH) % SIGNATURE_WIDTH
                    column = rotate_left(column, turn_back)
                signature = column
            record['fault_signature'] = signature

            ring[ring_position] = signature
            ring_position = (ring_position + 1) % params.ring_width
            span_running_total += record['resident_span']

        # every frame check total and the dump check total must hold as written
        dump_total = 0
        for columns in frame_lines:
            written = int(columns[8], 16)
            if frame_check_total(columns) != written:
                raise DecodeError('frame check total mismatch in dump %s'
                                  % dump['dump_id'])
            dump_total += written
        if dump_total % CHECK_MODULUS != int(dump['check'], 16):
            raise DecodeError('dump check total mismatch for %s' % dump['dump_id'])

        dump['frames'] = records
        dump['frame_count'] = count
        dump['base_tick'] = base
        dump['ordinal'] = ordinal
    return dumps, signatures_recovered


# --------------------------------------------------------------------------
# side exports
# --------------------------------------------------------------------------

def read_probe(path):
    probe = {}
    with open(path) as fh:
        for row in csv.DictReader(fh):
            probe[(row['module_tag'], int(row['frame_index']))] = {
                'samples': int(row['samples']),
                'peak_ns': int(row['peak_ns']),
            }
    return probe


def read_uplink(path):
    uplink = {}
    with open(path) as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            record = json.loads(raw)
            uplink[(record['module_tag'], record['frame_index'])] = {
                'host_id': record['host_id'],
                'tenant_class': record['tenant_class'],
                'bytes_uploaded': int(record['bytes_uploaded']),
            }
    return uplink


def read_controls(path):
    with open(path) as fh:
        return {name: int(value) for name, value in json.load(fh).items()}


# --------------------------------------------------------------------------
# corpus triage
# --------------------------------------------------------------------------

def export_figures(dumps, signatures_recovered):
    """Wire-level figures of one decoded export, before any acceptance work."""
    frames = [r for d in dumps for r in d['frames']]
    return {
        'frame_count': sum(d['frame_count'] for d in dumps),
        'base_total': sum(d['base_tick'] for d in dumps),
        'span_total': sum(f['resident_span'] for f in frames),
        'entry_total': sum(f['entry_stamp'] for f in frames),
        'unwind_total': sum(f['unwind_stamp'] for f in frames),
        'allocated_total': sum(f['allocated_bytes'] for f in frames),
        'signature_total': (sum(int(f['fault_signature'], 16) for f in frames)
                            if signatures_recovered else None),
        'frame_index_total': sum(f['frame_index'] for f in frames),
    }


class Triage(object):
    """Full derivation of one corpus: ledger, rejections and settlement."""

    def __init__(self, data_dir, prefix, params, reference):
        self.reference = reference
        self.dumps, self.signatures_recovered = decode_export(
            self._path(data_dir, prefix, 'fault_frames.psv'), params)
        self.probe = read_probe(self._path(data_dir, prefix, 'runtime_probe.csv'))
        self.uplink = read_uplink(self._path(data_dir, prefix, 'uplink_reports.jsonl'))
        self.frame_count = sum(d['frame_count'] for d in self.dumps)
        self.frames = [r for d in self.dumps for r in d['frames']]
        self._classify()
        self._settle()

    @staticmethod
    def _path(data_dir, prefix, name):
        return os.path.join(data_dir, (prefix + name) if prefix else name)

    # -- acceptance --------------------------------------------------------
    def _classify(self):
        reference = self.reference
        accepted = []
        rejections = dict((kind, 0) for kind in REJECTION_KINDS)
        for frame in self.frames:
            resolved = reference.resolve(frame['module_tag_written'],
                                         frame['build_index'])
            if resolved is None:
                rejections['unresolved_module'] += 1
                continue
            frame['canonical_tag'] = resolved['canonical_tag']
            frame['component_key'] = resolved['component_key']
            frame['declared_tier'] = int(resolved['tier_index'])
            if resolved['status'] != 'supported':
                rejections['retired_build'] += 1
                continue
            key = (frame['canonical_tag'], frame['frame_index'])
            probe = self.probe.get(key)
            report = self.uplink.get(key)
            window = reference.window_of.get(frame['component_key'])
            inside = bool(window) and window[0] < frame['capture_tick'] < window[1]
            if not inside:
                rejections['outside_support'] += 1
            if probe is None:
                rejections['unprobed_frame'] += 1
            if report is None:
                rejections['unreported_dump'] += 1
            if not inside or probe is None or report is None:
                continue
            frame['samples'] = probe['samples']
            frame['peak_ns'] = probe['peak_ns']
            frame['host_id'] = report['host_id']
            frame['host_zone'] = reference.zone_of[report['host_id']]
            frame['tenant_class'] = report['tenant_class']
            frame['tenant_segment'] = reference.segment_of[report['tenant_class']]
            frame['bytes_uploaded'] = report['bytes_uploaded']
            accepted.append(frame)
        accepted.sort(key=lambda f: (f['capture_tick'], f['source_dump'],
                                     f['frame_index']))
        self.accepted = accepted
        self.rejections = rejections
        self.rejected_count = self.frame_count - len(accepted)

    # -- quarantine settlement --------------------------------------------
    def _settle(self):
        reference = self.reference
        self.modules = {}
        for frame in self.accepted:
            entry = self.modules.setdefault(frame['canonical_tag'], {
                'module_tag': frame['canonical_tag'],
                'component_key': frame['component_key'],
                'declared_tier': frame['declared_tier'],
                'effective_tier': frame['declared_tier'],
                'frames': [],
            })
            entry['frames'].append(frame)

        def budgets():
            table = {}
            for tag, module in self.modules.items():
                ladder = reference.ladder_of[module['component_key']]
                table[tag] = ladder[module['effective_tier']]
            return table

        def allowances():
            budget = budgets()
            envelope = {}
            for component in {m['component_key'] for m in self.modules.values()}:
                members = [t for t, m in self.modules.items()
                           if m['component_key'] == component]
                lowest = min(budget[t] for t in members)
                envelope[component] = lowest * len(reference.ladder_of[component])
            return dict((tag, min(budget[tag], envelope[self.modules[tag]['component_key']]))
                        for tag in self.modules), budget

        passes = 0
        while True:
            passes += 1
            allowance, _ = allowances()
            demoted = False
            for tag in sorted(self.modules):
                module = self.modules[tag]
                if module['effective_tier'] == 0:
                    continue
                over = sum(1 for f in module['frames'] if f['peak_ns'] > allowance[tag])
                within = len(module['frames']) - over
                if over > within:
                    module['effective_tier'] -= 1
                    demoted = True
            if not demoted:
                break

        self.settlement_passes = passes
        allowance, budget = allowances()
        for tag, module in self.modules.items():
            module['allowance_ns'] = allowance[tag]
            module['budget_ns'] = budget[tag]
            over = sum(1 for f in module['frames'] if f['peak_ns'] > allowance[tag])
            module['within_budget_count'] = len(module['frames']) - over
            module['quarantined'] = (module['effective_tier'] == 0 and over > module['within_budget_count'])
        self.allowance_of = dict((tag, m['allowance_ns']) for tag, m in self.modules.items())

        for frame in self.accepted:
            frame['effective_tier'] = self.modules[frame['canonical_tag']]['effective_tier']
            frame['within_budget'] = (frame['peak_ns']
                                      <= self.allowance_of[frame['canonical_tag']])

        self.quarantined_modules = sorted(t for t, m in self.modules.items()
                                          if m['quarantined'])
        self.allocated_bytes = sum(f['allocated_bytes'] for f in self.accepted)
        self.uploaded_bytes = sum(f['bytes_uploaded'] for f in self.accepted)

    # -- figures -----------------------------------------------------------
    def control_figures(self):
        figures = export_figures(self.dumps, self.signatures_recovered)
        figures['accepted_count'] = len(self.accepted)
        figures['quarantined_count'] = len(self.quarantined_modules)
        figures['residual_bytes'] = self.allocated_bytes - self.uploaded_bytes
        return figures


CONTROL_OF_FIGURE = {
    'CTRL-A': 'frame_count',
    'CTRL-B': 'base_total',
    'CTRL-C': 'span_total',
    'CTRL-D': 'entry_total',
    'CTRL-E': 'unwind_total',
    'CTRL-F': 'allocated_total',
    'CTRL-G': 'signature_total',
    'CTRL-H': 'frame_index_total',
    'CTRL-I': 'accepted_count',
    'CTRL-J': 'quarantined_count',
    'CTRL-K': 'residual_bytes',
}

SIGNATURE_TOTAL_MODULUS = 1000000007


# --------------------------------------------------------------------------
# calibration: settle the withheld wire parameters
# --------------------------------------------------------------------------

# the figure a parameter is settled against reads only the parameters settled
# before it, so each is tested against a fully readable export
SETTLEMENT_ORDER = (
    PARAM_COUNT_MULTIPLIER,
    PARAM_ROTATION_OFFSET,
    PARAM_ENTRY_MODULUS,
    PARAM_DRIFT_PERIOD,
    PARAM_COARSE_UNIT,
    PARAM_ALTERNATE_RADIX,
    PARAM_BASE_SHIFT_WIDTH,
    PARAM_SIGNATURE_OFFSET,
    PARAM_RING_WIDTH,
)

# parameters one control figure settles jointly
PARAM_PARTNER = {
    PARAM_ALTERNATE_RADIX: PARAM_BASE_SHIFT_WIDTH,
    PARAM_BASE_SHIFT_WIDTH: PARAM_ALTERNATE_RADIX,
    PARAM_SIGNATURE_OFFSET: PARAM_RING_WIDTH,
    PARAM_RING_WIDTH: PARAM_SIGNATURE_OFFSET,
}

# stand-ins for parameters not yet settled: chosen to keep the export readable
READABLE_STANDIN = {
    PARAM_ENTRY_MODULUS: 1 << 34,
    PARAM_DRIFT_PERIOD: 768,
    PARAM_ROTATION_OFFSET: 0,
    PARAM_COUNT_MULTIPLIER: 0,
    PARAM_ALTERNATE_RADIX: 35,
    PARAM_BASE_SHIFT_WIDTH: 16,
    PARAM_SIGNATURE_OFFSET: 0,
    PARAM_COARSE_UNIT: 1024,
    PARAM_RING_WIDTH: 4,
}


def calibration_export(data_dir, params):
    try:
        return decode_export(os.path.join(data_dir,
                                          'calibration_fault_frames.psv'), params)
    except (DecodeError, KeyError, ValueError, OverflowError):
        return None


def calibration_figure(name, data_dir, reference, params):
    """One control figure of the control slice under one parameter assignment."""
    if name in ('CTRL-I', 'CTRL-J', 'CTRL-K'):
        try:
            figures = Triage(data_dir, 'calibration_', params,
                             reference).control_figures()
        except (DecodeError, KeyError, ValueError, OverflowError):
            return None
    else:
        decoded = calibration_export(data_dir, params)
        if decoded is None:
            return None
        figures = export_figures(*decoded)
    value = figures[CONTROL_OF_FIGURE[name]]
    if name == 'CTRL-G' and value is not None:
        value %= SIGNATURE_TOTAL_MODULUS
    return value


def count_multiplier_survivors(data_dir, controls):
    """Candidates the dump headers, check totals and frame-count control leave.

    Every other figure depends on the fault-frame count, so the count
    multiplier settles first, against the export alone.
    """
    dumps = read_dumps(os.path.join(data_dir, 'calibration_fault_frames.psv'))
    published = controls.get('CTRL-A')
    survivors = set()
    for candidate in PARAM_DOMAINS[PARAM_COUNT_MULTIPLIER]:
        total_frames = 0
        for ordinal, dump in enumerate(dumps, 1):
            bias = (ordinal * candidate) % COUNT_BIAS_MODULUS \
                if dump['flags'] & DUMP_COUNT_BIAS else 0
            count = dump['declared_count'] - bias
            if count < 1 or count > len(dump['lines']):
                break
            check = sum(int(columns[8], 16) for columns in dump['lines'][:count])
            if check % CHECK_MODULUS != int(dump['check'], 16):
                break
            total_frames += count
        else:
            if published is None or total_frames == published:
                survivors.add(candidate)
    return survivors


def settle_parameters(data_dir, reference, controls):
    """Enumerate every candidate of every parameter and settle each by its control.

    Each parameter is tested by holding the values already settled and asking
    whether the candidate reproduces a published control figure; where one
    figure settles two parameters jointly, a candidate survives when some value
    of its partner reproduces the figure alongside it.  Every candidate in
    every published domain earns a recorded verdict before any selection.
    """
    chosen = {}
    search = {}
    count_survivors = count_multiplier_survivors(data_dir, controls)

    def assignment(overrides):
        values = dict(READABLE_STANDIN)
        values.update(chosen)
        values.update(overrides)
        return WireParameters(**values)

    def survives(control, name, candidate):
        """Does `candidate` reproduce `control`, alone or with a partner value?"""
        if name == PARAM_COUNT_MULTIPLIER and candidate not in count_survivors:
            return False       # the dump headers and check totals already kill it
        published = controls[control]
        partner = PARAM_PARTNER.get(name)
        trials = [assignment({name: candidate})] if partner is None else \
            [assignment({name: candidate, partner: other})
             for other in PARAM_DOMAINS[partner]
             if partner not in chosen or other == chosen[partner]]
        for trial in trials:
            if calibration_figure(control, data_dir, reference, trial) == published:
                return True
        return False

    for name in SETTLEMENT_ORDER:
        domain = PARAM_DOMAINS[name]
        control_used = None
        survivors = list(domain)
        rejected = []
        for control in PARAM_CONTROLS[name]:
            if control not in controls:
                continue
            still = []
            for candidate in survivors:
                if survives(control, name, candidate):
                    still.append(candidate)
                else:
                    rejected.append({'value': candidate, 'control': control})
            survivors = still
            if len(survivors) == 1:
                control_used = control
                break
            if not survivors:
                break
        if control_used is None:
            control_used = next((c for c in PARAM_CONTROLS[name] if c in controls),
                                '')
        if len(survivors) != 1:
            raise SystemExit('parameter %r settles on %d candidates: %r'
                             % (name, len(survivors), survivors))
        chosen[name] = survivors[0]
        search[name] = {'candidates': len(PARAM_DOMAINS[name]),
                        'control': control_used,
                        'selected': survivors[0],
                        'rejected': rejected}

    parameters = [dict(search[name], name=name) for name in PARAMETERS]
    evaluations = sum(p['candidates'] for p in parameters)
    return WireParameters(**chosen), {'evaluations': evaluations,
                                      'parameters': parameters}


def calibrate(data_dir, reference):
    """Settle the withheld wire parameters against the published control figures."""
    path = os.path.join(data_dir, 'control-slice-figures.json')
    if not os.path.exists(path):
        # no control slice shipped: the candidate space is empty and the export
        # is read with the plain stand-in assignment
        return WireParameters(**READABLE_STANDIN), \
            {'evaluations': 0, 'parameters': []}, []
    controls = read_controls(path)
    params, search = settle_parameters(data_dir, reference, controls)
    comparison = [{'control': name, 'published': controls[name],
                   'reproduced': calibration_figure(name, data_dir, reference,
                                                    params)}
                  for name in sorted(controls)]
    return params, search, comparison


# --------------------------------------------------------------------------
# report assembly
# --------------------------------------------------------------------------

def group_totals(frames, key_field, value_of):
    totals = {}
    for frame in frames:
        totals[frame[key_field]] = totals.get(frame[key_field], 0) + value_of(frame)
    return totals


def module_list(frames):
    return sorted({f['canonical_tag'] for f in frames})


def zone_report(zone, frames):
    return {
        'zone': zone,
        'accepted_count': len(frames),
        'by_component': group_totals(frames, 'component_key',
                                     lambda f: f['bytes_uploaded']),
        'by_tenant_segment': group_totals(frames, 'tenant_segment',
                                          lambda f: f['bytes_uploaded']),
        'modules': module_list(frames),
        'total_bytes_uploaded': sum(f['bytes_uploaded'] for f in frames),
        'total_cost_ns': sum(f['resident_span'] for f in frames),
        'within_budget_count': sum(1 for f in frames if f['within_budget']),
    }


def segment_report(segment, frames):
    return {
        'tenant_segment': segment,
        'accepted_count': len(frames),
        'by_component': group_totals(frames, 'component_key',
                                     lambda f: f['bytes_uploaded']),
        'by_zone': group_totals(frames, 'host_zone', lambda f: f['bytes_uploaded']),
        'modules': module_list(frames),
        'total_bytes_uploaded': sum(f['bytes_uploaded'] for f in frames),
        'total_cost_ns': sum(f['resident_span'] for f in frames),
        'within_budget_count': sum(1 for f in frames if f['within_budget']),
    }


def summary_report(triage):
    frames = triage.accepted
    by_effective_tier = {}
    for frame in frames:
        key = str(frame['effective_tier'])
        by_effective_tier[key] = by_effective_tier.get(key, 0) + 1
    return {
        'accepted_count': len(frames),
        'rejected_count': triage.rejected_count,
        'by_component': group_totals(frames, 'component_key',
                                     lambda f: f['bytes_uploaded']),
        'by_zone': group_totals(frames, 'host_zone', lambda f: f['bytes_uploaded']),
        'by_tenant_segment': group_totals(frames, 'tenant_segment',
                                          lambda f: f['bytes_uploaded']),
        'by_effective_tier': by_effective_tier,
        'total_bytes_allocated': triage.allocated_bytes,
        'total_bytes_uploaded': triage.uploaded_bytes,
        'total_cost_ns': sum(f['resident_span'] for f in frames),
        'within_budget_count': sum(1 for f in frames if f['within_budget']),
    }


def product_report(triage):
    modules = []
    for tag in sorted(triage.modules):
        module = triage.modules[tag]
        frames = module['frames']
        modules.append({
            'module_tag': tag,
            'component_key': module['component_key'],
            'declared_tier': module['declared_tier'],
            'effective_tier': module['effective_tier'],
            'budget_ns': module['budget_ns'],
            'allowance_ns': module['allowance_ns'],
            'accepted_count': len(frames),
            'within_budget_count': module['within_budget_count'],
            'net_samples': sum(f['samples'] for f in frames),
            'total_bytes_uploaded': sum(f['bytes_uploaded'] for f in frames),
            'quarantined': module['quarantined'],
        })
    return {'modules': modules}


def data_quality_report(triage, search, comparison):
    return {
        'frame_count': triage.frame_count,
        'accepted_count': len(triage.accepted),
        'rejections': dict((kind, triage.rejections[kind]) for kind in REJECTION_KINDS),
        'reconciliation': {
            'allocated_bytes': triage.allocated_bytes,
            'uploaded_bytes': triage.uploaded_bytes,
            'residual_bytes': triage.allocated_bytes - triage.uploaded_bytes,
        },
        'settlement': {
            'passes': triage.settlement_passes,
            'quarantined_modules': triage.quarantined_modules,
        },
        'candidate_search': search,
        'calibration_comparison': comparison,
    }


def ledger_row(frame):
    return {
        'capture_tick': frame['capture_tick'],
        'source_dump': frame['source_dump'],
        'frame_index': frame['frame_index'],
        'module_tag': frame['canonical_tag'],
        'component_key': frame['component_key'],
        'symbol': frame['symbol'],
        'fault_signature': frame['fault_signature'],
        'enter_ns': frame['entry_stamp'],
        'unwind_ns': frame['unwind_stamp'],
        'cost_ns': frame['resident_span'],
        'bytes_allocated': frame['allocated_bytes'],
        'bytes_uploaded': frame['bytes_uploaded'],
        'samples': frame['samples'],
        'peak_ns': frame['peak_ns'],
        'host_zone': frame['host_zone'],
        'tenant_segment': frame['tenant_segment'],
        'effective_tier': frame['effective_tier'],
        'within_budget': frame['within_budget'],
    }


def write_json(out_dir, name, payload):
    with open(os.path.join(out_dir, name), 'w') as fh:
        fh.write(json.dumps(payload, indent=2, sort_keys=True))
        fh.write('\n')


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def run(data_dir, out_dir):
    reference = Reference(data_dir)
    params, search, comparison = calibrate(data_dir, reference)
    triage = Triage(data_dir, '', params, reference)

    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)

    with open(os.path.join(out_dir, 'normalized.jsonl'), 'w') as fh:
        for frame in triage.accepted:
            fh.write(json.dumps(ledger_row(frame), sort_keys=True,
                                separators=(',', ':')))
            fh.write('\n')

    write_json(out_dir, 'summary.json', summary_report(triage))
    write_json(out_dir, 'product_summary.json', product_report(triage))
    for zone in reference.zones:
        write_json(out_dir, 'region_%s.json' % zone,
                   zone_report(zone, [f for f in triage.accepted
                                      if f['host_zone'] == zone]))
    for segment in reference.segments:
        write_json(out_dir, 'segment_%s.json' % segment,
                   segment_report(segment, [f for f in triage.accepted
                                            if f['tenant_segment'] == segment]))
    write_json(out_dir, 'data_quality.json',
               data_quality_report(triage, search, comparison))


def main(argv):
    if len(argv) == 3:
        data_dir, out_dir = argv[1], argv[2]
    elif len(argv) == 1:
        data_dir, out_dir = '/app/data', '/app/out'
    else:
        sys.stderr.write('usage: %s [DATA_DIR OUT_DIR]\n' % argv[0])
        return 2
    run(data_dir, out_dir)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
