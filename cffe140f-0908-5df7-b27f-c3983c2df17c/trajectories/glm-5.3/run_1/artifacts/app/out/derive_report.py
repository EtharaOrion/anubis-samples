#!/usr/bin/env python3
"""Settle a sealed tactics-league season from its replay journals.

Usage:  python3 derive_report.py DATA_DIR OUT_DIR

Reads the sealed captures, the policy documents and the published rosters that
live in DATA_DIR, pins the unstated governing quantities against the published
control figures, settles the season and writes nine reports into OUT_DIR.

Standard library only.  No network, no installs, no reads outside DATA_DIR and
no writes outside OUT_DIR.
"""

import collections
import csv
import hashlib
import itertools
import json
import os
import re
import struct
import sys

M32 = 1 << 32
CAPTURE_MAGIC = b'RJNL'
SEGMENT_MAGIC = b'SEG0'
ENVELOPE_WIDTH = 36
SEGMENT_ENVELOPE_WIDTH = 28
KIND_NAMES = {1: 'deploy', 2: 'output', 3: 'eliminate', 4: 'disband',
              5: 'concede', 6: 'transfer', 8: 'close'}
TIER_ORDER = ['champion', 'contender', 'qualifier', 'provisional']


class DecodeError(Exception):
    """A capture would not decode under the parameters in force.

    ``figure`` names the published control figure that the failure falsifies:
    the first figure when the capture does not even hold together structurally,
    the second when records cannot be recovered from it.
    """

    def __init__(self, message, figure='C2'):
        Exception.__init__(self, message)
        self.figure = figure


# --------------------------------------------------------------------------
# byte helpers
# --------------------------------------------------------------------------

def rotl(b, t):
    w = len(b)
    t %= w
    return b[t:] + b[:t] if t else b


def rotr(b, t):
    return rotl(b, (-t) % len(b))


def gcd(a, b):
    while b:
        a, b = b, a % b
    return a


# --------------------------------------------------------------------------
# policy documents
# --------------------------------------------------------------------------

def read_text(path):
    with open(path, 'r', encoding='utf-8') as fh:
        return fh.read()


def backticks(line):
    return re.findall(r'`([^`]*)`', line)


def named_roster(text, label, cast=str):
    """The published roster written after 'label:' in a policy document."""
    for line in text.splitlines():
        if line.strip().startswith(label + ':'):
            vals = backticks(line)
            if vals:
                return [cast(v.strip()) for v in vals]
    return None


def named_scalar(text, label, cast=int, default=None):
    for line in text.splitlines():
        if line.strip().startswith(label + ':'):
            vals = backticks(line)
            if vals:
                try:
                    return cast(vals[0].strip())
                except (TypeError, ValueError):
                    pass
    return default


def load_documents(data_dir):
    docs = {}
    for name in sorted(os.listdir(data_dir)):
        if name.endswith('.md'):
            docs[name] = read_text(os.path.join(data_dir, name))
    return docs


def parse_guard_table(replay_guards):
    """bit index -> guard name, from the replay-guards bit field table."""
    table = {}
    for line in replay_guards.splitlines():
        m = re.match(r'^\|\s*(\d+)\s*\|\s*([a-z0-9_]+)\s*\|$', line.strip())
        if m:
            table[int(m.group(1))] = m.group(2)
    return table


def parse_control_figures(calibration):
    """Published control figures in published order: 'C1' -> (label, value)."""
    out = collections.OrderedDict()
    for line in calibration.splitlines():
        m = re.match(r'^\s*-\s*(C\d+)\s+(.+?)\s*:\s*(-?\d+)\s*$', line)
        if m:
            out[m.group(1)] = (m.group(2), int(m.group(3)))
    return out


def metric_key(label):
    """Normalise a published figure label to the key used in computed metrics."""
    key = label.strip().lower().replace(' ', '_')
    if key.endswith('_entrants'):
        key = key[:-len('_entrants')]
    return key


# --------------------------------------------------------------------------
# capture container
# --------------------------------------------------------------------------

class Capture(object):
    def __init__(self, path, data):
        self.path = path
        self.filename = os.path.basename(path)
        self.data = data
        if len(data) < ENVELOPE_WIDTH or data[:4] != CAPTURE_MAGIC:
            raise DecodeError('%s: not an RJNL capture' % self.filename, 'C1')
        (self.marker, self.version, self.reserved, self.guard_register,
         self.nonce_stored, self.dseg, self.drec, self.base_tick,
         self.capture_id, self.dros, self.envelope_checksum) = struct.unpack(
            '<4sBBHIIIIIII', data[:ENVELOPE_WIDTH])
        self.envelope_sum = sum(data[:32]) % M32
        self.envelope_checksum_ok = (self.envelope_sum == self.envelope_checksum)
        self.role = {1: 'reference', 2: 'control', 3: 'season'}.get(
            self.capture_id % 10, 'unknown')
        self.segments = self._split()

    def _split(self):
        segs = []
        i = ENVELOPE_WIDTH
        data = self.data
        while i + SEGMENT_ENVELOPE_WIDTH <= len(data):
            marker, slot, mid, cnt, salt, pw, pc = struct.unpack(
                '<4sIIIIII', data[i:i + SEGMENT_ENVELOPE_WIDTH])
            if marker != SEGMENT_MAGIC:
                raise DecodeError('%s: bad segment marker at %d' % (self.filename, i), 'C1')
            if i + SEGMENT_ENVELOPE_WIDTH + pw > len(data):
                raise DecodeError('%s: truncated segment payload at %d' % (self.filename, i), 'C1')
            payload = data[i + SEGMENT_ENVELOPE_WIDTH:i + SEGMENT_ENVELOPE_WIDTH + pw]
            segs.append({
                'offset': i, 'slot': slot, 'match_id': mid, 'declared_count': cnt,
                'salt': salt, 'payload_width': pw, 'payload_checksum': pc,
                'payload': payload,
                'payload_checksum_ok': (sum(payload) % M32 == pc),
            })
            i += SEGMENT_ENVELOPE_WIDTH + pw
        if i != len(data):
            raise DecodeError('%s: trailing bytes after the last segment' % self.filename, 'C1')
        return segs

    def guards(self, table):
        return {name: bool(self.guard_register >> bit & 1) for bit, name in table.items()}


# --------------------------------------------------------------------------
# decoding
# --------------------------------------------------------------------------

def segment_step(nonce, nseg):
    base = (nonce >> 8) % nseg
    c = base + 2
    while gcd(c, nseg) != 1:
        c += 1
    return c


def logical_slot_order(nonce, nseg, segperm):
    """Storage slot holding each logical position, listed in logical order."""
    if not segperm or nseg <= 1:
        return list(range(nseg))
    step = segment_step(nonce, nseg)
    inv = pow(step, -1, nseg)
    return [((logical - nonce) * inv) % nseg for logical in range(nseg)]


def decode_capture(cap, guard_table, engine_tag, window_depth, sentinel, roster,
                   empty_roster=False):
    """Decode one capture into the ordered list of records recovered from it.

    Every structural inconsistency raises DecodeError, so that a wrong set of
    governing quantities is rejected by the data rather than by assertion.
    """
    G = cap.guards(guard_table)
    bias = (sum(engine_tag.encode('utf-8')) % 11) if G.get('countbias') else 0
    nonce = struct.unpack('>I', struct.pack('<I', cap.nonce_stored))[0] \
        if G.get('endian') else cap.nonce_stored
    declared_segments = cap.dseg - bias
    declared_records = cap.drec - bias
    declared_roster = cap.dros - bias
    if not cap.envelope_checksum_ok:
        raise DecodeError('%s: envelope checksum mismatch' % cap.filename, 'C1')
    if declared_segments != len(cap.segments):
        raise DecodeError('%s: declares %d segments but holds %d'
                          % (cap.filename, declared_segments, len(cap.segments)), 'C1')
    if declared_segments < 1:
        raise DecodeError('%s: declares no segments' % cap.filename, 'C1')
    by_slot = {}
    for s in cap.segments:
        if s['slot'] in by_slot:
            raise DecodeError('%s: duplicate storage slot %d' % (cap.filename, s['slot']), 'C1')
        by_slot[s['slot']] = s
        if not s['payload_checksum_ok']:
            raise DecodeError('%s: payload checksum mismatch in slot %d'
                              % (cap.filename, s['slot']), 'C1')
        s['count'] = s['declared_count'] - bias
        if s['count'] < 0:
            raise DecodeError('%s: negative record count in slot %d'
                              % (cap.filename, s['slot']), 'C1')
    order = logical_slot_order(nonce, declared_segments, G.get('segperm'))
    for slot in order:
        if slot not in by_slot:
            raise DecodeError('%s: no segment stored in slot %d' % (cap.filename, slot), 'C1')
    dict_roster = () if empty_roster else tuple(roster)
    n_dict = len(dict_roster)

    recs = []
    running_value_sum = 0
    window = []
    previous_tick = None
    for logical, slot in enumerate(order):
        seg = by_slot[slot]
        eff_salt = ((seg['salt'] ^ running_value_sum) % M32) if G.get('driftkey') else seg['salt']
        payload = seg['payload']
        j = 0
        d1 = 0
        last_tick = 0
        for k in range(seg['count']):
            if j + 2 > len(payload):
                raise DecodeError('%s: record overrun in slot %d record %d'
                                  % (cap.filename, slot, k))
            visible, shadow = payload[j], payload[j + 1]
            if G.get('shadow'):
                width = shadow ^ (eff_salt & 0xff)
            else:
                width = visible
                if shadow != 0:
                    raise DecodeError('%s: idle shadow guard carries a shadow byte'
                                      % cap.filename, 'C1')
            if width < 10 or j + 2 + width > len(payload):
                raise DecodeError('%s: implausible record width %d in slot %d record %d'
                                  % (cap.filename, width, slot, k))
            blob = payload[j + 2:j + 2 + width]
            if G.get('keystream'):
                key = hashlib.sha256(('%s:%d:%d:%d' % (engine_tag, seg['match_id'],
                                                       eff_salt, k)).encode('utf-8')).digest()
                blob = bytes(a ^ b for a, b in zip(blob, key))
            if G.get('rotate'):
                blob = rotr(blob, (eff_salt + len(recs)) % width)
            q = 0
            directives = []
            if G.get('escape'):
                while q + 1 < len(blob) and blob[q] == sentinel and len(directives) < 2:
                    directives.append(blob[q + 1])
                    q += 2
            kind, mode = blob[q], blob[q + 1]
            if kind not in KIND_NAMES or mode not in (0, 1, 2):
                raise DecodeError('%s: bad event kind %d or attribution mode %d in slot %d record %d'
                                  % (cap.filename, kind, mode, slot, k))
            if mode == 0:
                literal = blob[q + 2:q + 5]
                if len(literal) != 3:
                    raise DecodeError('%s: short literal attribution in slot %d record %d'
                                      % (cap.filename, slot, k))
                attr = literal.decode('ascii', 'replace')
                p = q + 5
            else:
                attr = blob[q + 2]
                p = q + 3
            if p + 10 != len(blob):
                raise DecodeError('%s: record body length mismatch in slot %d record %d'
                                  % (cap.filename, slot, k))
            unit, = struct.unpack('<H', blob[p:p + 2])
            tick, = struct.unpack('<I', blob[p + 2:p + 6])
            stored_value, = struct.unpack('<I', blob[p + 6:p + 10])
            if G.get('delta2'):
                d1 = (d1 + tick) % M32
                last_tick = (last_tick + d1) % M32
                tick = last_tick
            value = ((stored_value ^ (nonce if previous_tick is None else previous_tick)) % M32) \
                if G.get('chain') else stored_value
            if mode == 0:
                key = attr
            elif mode == 1:
                if attr >= len(window):
                    raise DecodeError('%s: back reference %d outside the %d deep window'
                                      % (cap.filename, attr, len(window)))
                key = window[attr]
            elif n_dict:
                key = dict_roster[attr % n_dict]
            else:
                key = '#alias%d' % attr
            if key in window:
                window.remove(key)
            window.append(key)
            if len(window) > window_depth:
                window.pop(0)
            for d in directives:
                op, operand = d >> 4, d & 15
                if op == 1:
                    key = ('#directive%d' % operand) if not n_dict \
                        else dict_roster[operand % n_dict]
                elif op == 2:
                    if n_dict and key in dict_roster:
                        key = dict_roster[(dict_roster.index(key) + operand) % n_dict]
                else:
                    raise DecodeError('%s: unknown escape operation %d' % (cap.filename, op))
            recs.append({
                'logical_segment': logical, 'storage_slot': slot,
                'match_id': seg['match_id'], 'record_ordinal': len(recs),
                'segment_ordinal': k, 'squad_key': key, 'unit': unit, 'kind': kind,
                'tick': tick, 'value': value, 'attribution_mode': mode,
                'directives': directives,
            })
            previous_tick = tick
            running_value_sum = (running_value_sum + value) % M32
            j += 2 + width
        filler = seg['payload_width'] - j
        if filler != 3 + (seg['salt'] % 5):
            raise DecodeError('%s: filler width %d where %d was expected in slot %d'
                              % (cap.filename, filler, 3 + seg['salt'] % 5, slot), 'C2')
        seg['effective_salt'] = eff_salt
        seg['logical_position'] = logical
        seg['filler_width'] = filler
    backward_places = 0
    ticks_by_segment = collections.defaultdict(list)
    for r in recs:
        ticks_by_segment[r['logical_segment']].append(r['tick'])
    for ticks in ticks_by_segment.values():
        for a, b in zip(ticks, ticks[1:]):
            if b < a:
                backward_places += 1
    return {
        'capture': cap, 'guards': G, 'bias': bias, 'nonce': nonce,
        'declared_segments': declared_segments, 'declared_records': declared_records,
        'declared_roster': declared_roster, 'records': recs, 'order': order,
        'backward_places': backward_places,
        'record_count_residual': len(recs) - declared_records,
    }


def distinct_keys(records):
    seen = []
    for r in records:
        if r['squad_key'] not in seen:
            seen.append(r['squad_key'])
    return seen


def build_roster_dictionary(reference, guard_table, engine_tag, window_depth, sentinel):
    """Distinct attribution keys as written on the wire, in first-met logical order."""
    roster = ()
    decoded = None
    for _ in range(4):
        decoded = decode_capture(reference, guard_table, engine_tag, window_depth,
                                 sentinel, roster, empty_roster=True)
        seen = [k for k in distinct_keys(decoded['records']) if not k.startswith('#')]
        if tuple(seen) == roster:
            break
        roster = tuple(seen)
    decoded = decode_capture(reference, guard_table, engine_tag, window_depth,
                             sentinel, roster)
    return list(distinct_keys(decoded['records'])), decoded


# --------------------------------------------------------------------------
# registry and topology
# --------------------------------------------------------------------------

class Registry(object):
    def __init__(self, data_dir):
        self.path = data_dir
        self.entrants = self._rows('entrant-registry.csv')
        self.by_key = {e['squad_key']: e for e in self.entrants}
        self.order = [e['squad_key'] for e in self.entrants]
        self.divisions = self._rows('division-topology.csv', 'division')
        self.licences = self._rows('licence-classes.csv', 'licence_class')
        self.conferences = []
        for d in self.divisions.values():
            if d['conference'] not in self.conferences:
                self.conferences.append(d['conference'])
        self.segments = []
        for lc in self.licences.values():
            if lc['segment'] not in self.segments:
                self.segments.append(lc['segment'])

    def _rows(self, name, key=None):
        with open(os.path.join(self.path, name), newline='', encoding='utf-8') as fh:
            rows = list(csv.DictReader(fh))
        return {r[key]: r for r in rows} if key else rows

    def division(self, key):
        e = self.by_key.get(key)
        return e['division'] if e else None

    def conference(self, key):
        return self.divisions.get(self.division(key) or '', {}).get('conference')

    def licence(self, key):
        e = self.by_key.get(key)
        return e['licence_class'] if e else None

    def segment(self, key):
        return self.licences.get(self.licence(key) or '', {}).get('segment')

    def ladder_weight(self, key):
        v = self.divisions.get(self.division(key) or '', {}).get('ladder_weight')
        return int(v) if v is not None else 1

    def levy_rate(self, key):
        v = self.licences.get(self.licence(key) or '', {}).get('levy_rate')
        return int(v) if v is not None else 1

    def is_ranked(self, key):
        e = self.by_key.get(key)
        return bool(e and e.get('is_ranked', '').strip().lower() == 'yes')

    def entrant_id(self, key):
        e = self.by_key.get(key)
        return e['entrant_id'] if e else None


# --------------------------------------------------------------------------
# ownership settlement
# --------------------------------------------------------------------------

def handovers(records, roster):
    hand = {}
    for r in records:
        if r['kind'] == 6:
            if not roster:
                raise DecodeError('a transfer event appears with no roster dictionary')
            hand[r['squad_key']] = roster[r['value'] % len(roster)]
    return hand


def closed_sets(hand):
    sets = []
    for start in hand:
        path, cur = [], start
        while cur in hand and cur not in path:
            path.append(cur)
            cur = hand[cur]
        if cur in path:
            cycle = path[path.index(cur):]
            if len(cycle) > 1 and sorted(cycle) not in [sorted(s) for s in sets]:
                sets.append(cycle)
    return sets


def effective_hand(hand):
    """A closed set of hand overs settles on the member whose key sorts first."""
    eff = dict(hand)
    for cycle in closed_sets(hand):
        rep = min(cycle)
        for member in cycle:
            eff[member] = rep
    return eff


def settle_ownership(squads, hand):
    eff = effective_hand(hand)
    owner = {k: k for k in squads}
    sweeps = 0
    while True:
        sweeps += 1
        new = {k: eff.get(v, v) for k, v in owner.items()}
        if new == owner:
            break
        owner = new
    return owner, sweeps


# --------------------------------------------------------------------------
# season settlement
# --------------------------------------------------------------------------

def settle_season(records, registry, budget, roster):
    by_segment = collections.defaultdict(list)
    for r in records:
        by_segment[r['logical_segment']].append(r)

    hand = handovers(records, roster)
    squads = list(distinct_keys(records))
    for target in hand.values():
        if target not in squads:
            squads.append(target)
    for key in registry.order:
        if key not in squads:
            squads.append(key)
    owner, sweeps = settle_ownership(squads, hand)

    matches = {}
    for logical in sorted(by_segment):
        rs = by_segment[logical]
        concede, elim, disband, deploy = {}, {}, {}, {}
        close_tick = None
        for r in rs:
            k = r['squad_key']
            if r['kind'] == 8:
                close_tick = r['tick']
            elif r['kind'] == 5:
                concede[k] = r['tick']
            elif r['kind'] == 3:
                elim[(k, r['unit'])] = r['tick']
            elif r['kind'] == 4:
                disband[(k, r['unit'])] = r['tick']
            elif r['kind'] == 1:
                deploy[(k, r['unit'])] = (r['tick'], r['value'])
        gross, voided = collections.Counter(), collections.Counter()
        for r in rs:
            if r['kind'] != 2:
                continue
            k = r['squad_key']
            if k in concede and r['tick'] >= concede[k]:
                voided[k] += r['value']
            elif (k, r['unit']) in elim and r['tick'] >= elim[(k, r['unit'])]:
                voided[k] += r['value']
            else:
                gross[k] += r['value']
        closing = close_tick if close_tick is not None else max(r['tick'] for r in rs)
        raw = collections.Counter()
        for (k, u), (t0, rate) in deploy.items():
            raw[k] += rate * (disband.get((k, u), closing) - t0)
        participants = set(gross) | set(voided) | set(raw) | {r['squad_key'] for r in rs}
        matches[logical] = {
            'logical_segment': logical, 'match_id': rs[0]['match_id'],
            'gross': gross, 'voided': voided, 'raw': raw, 'concede': concede,
            'charged': collections.Counter(), 'participants': participants,
            'escape_records': sum(1 for r in rs if r['directives']),
            'close_tick': closing,
        }

    charged, overflow = collections.Counter(), collections.Counter()
    for logical in sorted(matches):
        m = matches[logical]
        for k in m['raw']:
            take = min(max(0, budget - charged[k]), m['raw'][k])
            charged[k] += take
            overflow[k] += m['raw'][k] - take
            m['charged'][k] = take

    season, standing, streak, played = (collections.Counter() for _ in range(4))
    for logical in sorted(matches):
        m = matches[logical]
        for k in m['participants']:
            played[k] += 1
        scores = {k: m['gross'][k] * registry.ladder_weight(k)
                  - m['charged'][k] * registry.levy_rate(k)
                  for k in m['participants']}
        best = None
        for k, v in scores.items():
            if k in m['concede']:
                continue
            if best is None or v > best:
                best = v
        winners = sorted(k for k, v in scores.items()
                         if k not in m['concede'] and v == best)
        m['scores'] = scores
        m['winners'] = winners
        for k, v in scores.items():
            season[k] += v
            if k in m['concede']:
                continue
            if len(winners) == 1 and k == winners[0]:
                standing[k] += 3
            elif len(winners) > 1 and k in winners:
                standing[k] += 1
    for k in season:
        run = 0
        for logical in sorted(matches):
            m = matches[logical]
            if k not in m['scores']:
                continue
            outright = len(m['winners']) == 1 and k == m['winners'][0]
            run = run + 1 if outright else 0
            if run >= 2:
                streak[k] += 2

    return {'matches': matches, 'hand': hand, 'owner': owner, 'sweeps': sweeps,
            'squads': squads, 'charged': charged, 'overflow': overflow,
            'season': season, 'standing': standing, 'streak': streak, 'played': played}


def season_totals(settled):
    gross, voided, raw = collections.Counter(), collections.Counter(), collections.Counter()
    for m in settled['matches'].values():
        gross.update(m['gross'])
        voided.update(m['voided'])
        raw.update(m['raw'])
    return gross, voided, raw


def tier_of(score, threshold):
    if score >= 3 * threshold:
        return 'champion'
    if score >= 2 * threshold:
        return 'contender'
    if score >= threshold:
        return 'qualifier'
    return 'provisional'


def compute_control_metrics(decoded, registry, budget, threshold, roster):
    recs = decoded['records']
    st = settle_season(recs, registry, budget, roster)
    gross, voided, raw = season_totals(st)
    tiers = collections.Counter(tier_of(st['season'][k], threshold) for k in st['season'])
    return {
        'matches_opened': len(decoded['order']),
        'records_recovered': len(recs),
        'distinct_squads': len(distinct_keys(recs)),
        'gross_output': sum(gross.values()),
        'voided_output': sum(voided.values()),
        'raw_upkeep': sum(raw.values()),
        'charged_upkeep': sum(st['charged'].values()),
        'overflow_upkeep': sum(st['overflow'].values()),
        'champion': tiers.get('champion', 0),
        'contender': tiers.get('contender', 0),
        'qualifier': tiers.get('qualifier', 0),
        'provisional': tiers.get('provisional', 0),
    }


# --------------------------------------------------------------------------
# reports
# --------------------------------------------------------------------------

ENTRY_FIELDS = ('squad_key', 'entrant_id', 'division', 'conference', 'licence_class',
                'segment', 'matches', 'gross_output', 'voided_output',
                'charged_upkeep', 'overflow_upkeep', 'season_score',
                'standing_points', 'streak_credit', 'tier')


def group_report(kind, name, entries, divisions):
    return {
        'group_kind': kind,
        'group_name': name,
        'divisions': sorted(d for d in divisions if d),
        'entrants': len(entries),
        'gross_output': sum(e['gross_output'] for e in entries),
        'voided_output': sum(e['voided_output'] for e in entries),
        'charged_upkeep': sum(e['charged_upkeep'] for e in entries),
        'overflow_upkeep': sum(e['overflow_upkeep'] for e in entries),
        'season_score': sum(e['season_score'] for e in entries),
        'standing_points': sum(e['standing_points'] for e in entries),
        'tier_counts': {t: sum(1 for e in entries if e['tier'] == t) for t in TIER_ORDER},
        'entries': sorted(entries, key=lambda e: e['squad_key']),
    }


def capture_reconciliation(cap, dec, registry):
    squads = distinct_keys(dec['records'])
    return {
        'file': cap.filename,
        'role': cap.role,
        'capture_identifier': cap.capture_id,
        'guard_register': cap.guard_register,
        'guards_active': sorted(n for n, on in dec['guards'].items() if on),
        'capture_nonce': dec['nonce'],
        'count_bias': dec['bias'],
        'envelope_checksum_declared': cap.envelope_checksum,
        'envelope_checksum_computed': cap.envelope_sum,
        'envelope_checksum_agrees': cap.envelope_checksum_ok,
        'payload_checksums_agree': all(s['payload_checksum_ok'] for s in cap.segments),
        'declared_segment_count': dec['declared_segments'],
        'recovered_segment_count': len(cap.segments),
        'segment_count_residual': len(cap.segments) - dec['declared_segments'],
        'declared_record_count': dec['declared_records'],
        'recovered_record_count': len(dec['records']),
        'record_count_residual': dec['record_count_residual'],
        'declared_roster_size': dec['declared_roster'],
        'distinct_squads_recovered': len(squads),
        'registered_squads_recovered': sum(1 for k in squads if k in registry.by_key),
        'roster_size_residual': len(squads) - dec['declared_roster'],
        'unregistered_squads': sorted(k for k in squads if k not in registry.by_key),
        'backward_tick_places': dec['backward_places'],
    }


def write_json(path, obj):
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(obj, fh, indent=2)
        fh.write('\n')


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def run(data_dir, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    docs = load_documents(data_dir)
    registry = Registry(data_dir)

    guards_doc = docs.get('replay-guards.md', '')
    guard_table = parse_guard_table(guards_doc)
    engine_roster = named_roster(guards_doc, 'Engine tag roster', str)
    depth_roster = named_roster(guards_doc, 'Window depth roster', int)
    sentinel_roster = named_roster(guards_doc, 'Sentinel roster', lambda v: int(v, 0))
    budget_roster = named_roster(docs.get('upkeep-policy.md', ''), 'Budget cap roster', int)
    threshold_roster = named_roster(docs.get('standings-policy.md', ''),
                                    'Promotion threshold roster', int)
    figures = parse_control_figures(docs.get('calibration-controls.md', ''))
    bracket_doc = docs.get('bracket-policy.md', '')
    policy = {
        'eligibility_match_floor': named_scalar(bracket_doc, 'Eligibility match floor', int, 2),
        'ladder_weight_prominence': named_scalar(bracket_doc, 'Ladder weight prominence', int, 4),
        'standing_points_floor': named_scalar(bracket_doc, 'Standing points floor', int, 3),
        'combined_ladder_weight_floor': named_scalar(bracket_doc, 'Combined ladder weight floor', int, 7),
        'combined_gross_output_floor': named_scalar(bracket_doc, 'Combined gross output floor', int, 900),
        'review_weight': named_scalar(bracket_doc, 'Review weight', int, 25),
        'docket_size': named_scalar(bracket_doc, 'Docket size', int, 5),
        'docket_conference_cap': named_scalar(bracket_doc, 'Docket conference cap', int, 2),
    }

    captures = []
    for name in sorted(os.listdir(data_dir)):
        if name.endswith('.rjnl'):
            with open(os.path.join(data_dir, name), 'rb') as fh:
                captures.append(Capture(os.path.join(data_dir, name), fh.read()))
    by_role = collections.defaultdict(list)
    for c in captures:
        by_role[c.role].append(c)
    missing = [r for r in ('reference', 'control', 'season') if not by_role.get(r)]
    if missing:
        raise SystemExit('no capture found for the role(s): %s' % ', '.join(missing))
    reference = sorted(by_role['reference'], key=lambda c: -len(c.data))[0]
    control = sorted(by_role['control'], key=lambda c: -len(c.data))[0]
    season_cap = sorted(by_role['season'], key=lambda c: -len(c.data))[0]

    # -- stage 0: the reference capture and its labelled transcript
    transcript, transcript_name = None, None
    for name in sorted(os.listdir(data_dir)):
        if name.endswith('.jsonl'):
            transcript_name = name
            with open(os.path.join(data_dir, name), 'r', encoding='utf-8') as fh:
                transcript = [json.loads(line) for line in fh if line.strip()]
            break
    reference_error = None
    roster, reference_decoded = [], None
    for tag in engine_roster or ['']:
        for depth in depth_roster or [4]:
            for sent in sentinel_roster or [0]:
                try:
                    roster, reference_decoded = build_roster_dictionary(
                        reference, guard_table, tag, depth, sent)
                except DecodeError as exc:
                    reference_error = str(exc)
                    continue
                break
            if reference_decoded:
                break
        if reference_decoded:
            break
    if reference_decoded is None:
        raise SystemExit('the reference capture does not decode: %s' % reference_error)
    transcript_ok = None
    if transcript is not None:
        fields = ('kind', 'logical_segment', 'match_id', 'record_ordinal',
                  'squad_key', 'tick', 'unit', 'value')
        got = [{f: r[f] for f in fields} for r in reference_decoded['records']]
        want = [{f: e[f] for f in fields} for e in transcript]
        transcript_ok = (got == want)

    # -- stage 1: pin the guard quantities against the published control figures
    ids = list(figures)
    stage_guard = ids[:6]
    stage_budget = ids[6:8]
    stage_tier = ids[8:]

    def first_failure(metrics, upto=None):
        for cid in (upto if upto is not None else ids):
            label, published = figures[cid]
            got = metrics.get(metric_key(label))
            if got is None or got != published:
                return cid, label, published, got
        return None

    guard_combinations = []
    guard_survivors = []
    for tag, depth, sent in itertools.product(engine_roster, depth_roster, sentinel_roster):
        try:
            dec = decode_capture(control, guard_table, tag, depth, sent, roster)
            metrics = compute_control_metrics(dec, registry, budget_roster[0],
                                              threshold_roster[0], roster)
            error = None
        except DecodeError as exc:
            metrics, dec = None, None
            error = str(exc)
            error_figure = exc.figure
        row = {'engine_tag': tag, 'window_depth': depth, 'sentinel': sent,
               'verdict': 'rejected', 'falsifier': None, 'reproduces': []}
        if metrics is None:
            cid = error_figure if error_figure in figures else ids[0]
            row['falsifier'] = {'figure': cid, 'label': figures[cid][0],
                                'reason': error}
        else:
            failed = first_failure(metrics, stage_guard)
            row['reproduces'] = [cid for cid in stage_guard
                                 if metrics.get(metric_key(figures[cid][0])) == figures[cid][1]]
            if failed is None:
                row['verdict'] = 'accepted'
            else:
                row['falsifier'] = {'figure': failed[0], 'label': failed[1],
                                    'published': failed[2], 'reproduced': failed[3]}
            row['metrics'] = metrics
        guard_combinations.append(row)
        if row['verdict'] == 'accepted':
            guard_survivors.append((row, dec))
    if not guard_survivors:
        raise SystemExit('no candidate reproduces the published control figures')

    # -- stage 2: pin the season budget
    budget_rows = []
    budget_survivors = []
    for row, dec in guard_survivors:
        for budget in budget_roster:
            metrics = compute_control_metrics(dec, registry, budget,
                                              threshold_roster[0], roster)
            entry = {'engine_tag': row['engine_tag'], 'window_depth': row['window_depth'],
                     'sentinel': row['sentinel'], 'budget': budget,
                     'verdict': 'rejected', 'falsifier': None}
            failed = first_failure(metrics, stage_guard + stage_budget)
            if failed is None:
                entry['verdict'] = 'accepted'
            else:
                entry['falsifier'] = {'figure': failed[0], 'label': failed[1],
                                      'published': failed[2], 'reproduced': failed[3]}
            budget_rows.append(entry)
            if entry['verdict'] == 'accepted':
                budget_survivors.append((entry, dec))
    if not budget_survivors:
        raise SystemExit('no budget reproduces the published charged and overflow upkeep')

    # -- stage 3: pin the promotion threshold
    threshold_rows = []
    final = []
    for entry, dec in budget_survivors:
        for threshold in threshold_roster:
            metrics = compute_control_metrics(dec, registry, entry['budget'],
                                              threshold, roster)
            trow = {'engine_tag': entry['engine_tag'], 'window_depth': entry['window_depth'],
                    'sentinel': entry['sentinel'], 'budget': entry['budget'],
                    'threshold': threshold, 'verdict': 'rejected', 'falsifier': None}
            failed = first_failure(metrics, ids)
            if failed is None:
                trow['verdict'] = 'accepted'
            else:
                trow['falsifier'] = {'figure': failed[0], 'label': failed[1],
                                     'published': failed[2], 'reproduced': failed[3]}
            threshold_rows.append(trow)
            if trow['verdict'] == 'accepted':
                final.append((trow, metrics))
    if not final:
        raise SystemExit('no promotion threshold reproduces the published tier counts')
    chosen, reproduced_metrics = final[0]
    engine_tag = chosen['engine_tag']
    window_depth = chosen['window_depth']
    sentinel = chosen['sentinel']
    budget = chosen['budget']
    threshold = chosen['threshold']

    calibration = []
    for cid, (label, published) in figures.items():
        got = reproduced_metrics.get(metric_key(label))
        calibration.append({'figure': cid, 'label': label, 'published': published,
                            'reproduced': got, 'agrees': got == published})

    # -- decode and settle the season
    season_dec = decode_capture(season_cap, guard_table, engine_tag, window_depth,
                                sentinel, roster)
    recs = season_dec['records']
    settled = settle_season(recs, registry, budget, roster)
    gross, voided, raw = season_totals(settled)
    tiers = {k: tier_of(settled['season'][k], threshold) for k in settled['season']}

    ranked_keys = sorted(settled['season'],
                         key=lambda k: (-settled['season'][k], -settled['standing'][k], k))
    ranking = [{'rank': i, 'squad_key': k, 'season_score': settled['season'][k],
                'standing_points': settled['standing'][k],
                'streak_credit': settled['streak'][k], 'matches': settled['played'][k],
                'tier': tiers[k]} for i, k in enumerate(ranked_keys, 1)]

    per_squad = {}
    for k in sorted(settled['season']):
        per_squad[k] = {
            'squad_key': k, 'entrant_id': registry.entrant_id(k),
            'division': registry.division(k), 'conference': registry.conference(k),
            'licence_class': registry.licence(k), 'segment': registry.segment(k),
            'matches': settled['played'][k], 'gross_output': gross[k],
            'voided_output': voided[k], 'raw_upkeep': raw[k],
            'charged_upkeep': settled['charged'][k],
            'overflow_upkeep': settled['overflow'][k],
            'season_score': settled['season'][k],
            'standing_points': settled['standing'][k],
            'streak_credit': settled['streak'][k], 'tier': tiers[k],
            'is_ranked': registry.is_ranked(k), 'registered': k in registry.by_key,
            'own_effective_owner': settled['owner'].get(k, k) == k,
            'handed_entry_away': k in settled['hand'],
            'effective_owner': settled['owner'].get(k, k),
        }

    conference_reports, segment_reports = {}, {}
    for conf in sorted(registry.conferences):
        entries = [{f: per_squad[k][f] for f in ENTRY_FIELDS}
                   for k in sorted(per_squad) if registry.conference(k) == conf]
        conference_reports[conf] = group_report(
            'conference', conf, entries, {e['division'] for e in entries})
    for seg in sorted(registry.segments):
        entries = [{f: per_squad[k][f] for f in ENTRY_FIELDS}
                   for k in sorted(per_squad) if registry.segment(k) == seg]
        segment_reports[seg] = group_report(
            'segment', seg, entries, {e['division'] for e in entries})

    # -- reconciliation
    unregistered = sorted(k for k in per_squad if k not in registry.by_key)
    absent = [k for k in registry.order if k not in per_squad]
    season_gross_total = sum(gross.values())
    conference_gross_total = sum(r['gross_output'] for r in conference_reports.values())
    output_reported = sum(r['value'] for r in recs if r['kind'] == 2)
    charged_total = sum(settled['charged'].values())
    overflow_total = sum(settled['overflow'].values())
    raw_total = sum(raw.values())

    data_quality = {
        'captures': [
            capture_reconciliation(reference, reference_decoded, registry),
            capture_reconciliation(control, decode_capture(
                control, guard_table, engine_tag, window_depth, sentinel, roster), registry),
            capture_reconciliation(season_cap, season_dec, registry),
        ],
        'reference_transcript_check': {
            'present': transcript is not None,
            'file': transcript_name,
            'records_compared': len(transcript) if transcript is not None else 0,
            'field_by_field_agreement': transcript_ok,
        },
        'residuals': {
            'segment_count_residual': len(season_cap.segments) - season_dec['declared_segments'],
            'record_count_residual': season_dec['record_count_residual'],
            'roster_size_residual': len(per_squad) - season_dec['declared_roster'],
            'attribution_residual': season_gross_total - conference_gross_total,
            'output_ledger_residual': (season_gross_total + sum(voided.values())) - output_reported,
            'upkeep_ledger_residual': (charged_total + overflow_total) - raw_total,
            'backward_tick_places': season_dec['backward_places'],
        },
        'attribution_residual_detail': {
            'season_gross_output': season_gross_total,
            'sum_of_conference_gross_outputs': conference_gross_total,
            'carried_by_unregistered_squads': sum(gross[k] for k in unregistered),
            'unregistered_squads': unregistered,
        },
        'unregistered_squads': [
            {'squad_key': k, 'gross_output': gross[k], 'voided_output': voided[k],
             'raw_upkeep': raw[k], 'charged_upkeep': settled['charged'][k],
             'overflow_upkeep': settled['overflow'][k],
             'season_score': settled['season'][k],
             'standing_points': settled['standing'][k],
             'matches': settled['played'][k], 'tier': tiers[k]}
            for k in unregistered],
        'absent_entrants': [
            {'squad_key': k, 'entrant_id': registry.entrant_id(k),
             'division': registry.division(k), 'conference': registry.conference(k),
             'licence_class': registry.licence(k), 'segment': registry.segment(k)}
            for k in absent],
        'kind_ledger': collections.OrderedDict(
            (KIND_NAMES[k], sum(1 for r in recs if r['kind'] == k)) for k in sorted(KIND_NAMES)),
        'notes': [
            'The declared roster size counts only the squads of a capture that appear in '
            'the entrant registry, so a non zero roster size residual is the number of '
            'squads that played without an entry.',
            'The attribution residual is the season gross output that no conference '
            'report carries, namely the gross output of the unregistered squads.',
            'A residual that is not zero is reported here as a finding, never silenced.',
        ],
    }

    # -- finals bracket
    eligible = sorted(k for k in per_squad
                      if not per_squad[k]['handed_entry_away']
                      and per_squad[k]['own_effective_owner']
                      and settled['played'][k] >= policy['eligibility_match_floor'])

    def bracket_constraints(members):
        names = ['member_in_a_prominent_division',
                 'both_entrant_segments_present',
                 'every_member_at_the_standing_points_floor',
                 'no_two_members_share_a_conference',
                 'a_ranked_entrant_present',
                 'combined_ladder_weight_at_the_floor',
                 'no_member_carries_overflow_upkeep',
                 'a_champion_tier_member_present',
                 'combined_gross_output_at_the_floor',
                 'no_member_handed_its_entry_away']
        vals = [
            any(registry.ladder_weight(m) >= policy['ladder_weight_prominence'] for m in members),
            all(s in {registry.segment(m) for m in members} for s in registry.segments),
            all(settled['standing'][m] >= policy['standing_points_floor'] for m in members),
            len({registry.conference(m) for m in members}) == len(members),
            any(registry.is_ranked(m) for m in members),
            sum(registry.ladder_weight(m) for m in members) >= policy['combined_ladder_weight_floor'],
            all(settled['overflow'][m] == 0 for m in members),
            any(tiers[m] == 'champion' for m in members),
            sum(gross[m] for m in members) >= policy['combined_gross_output_floor'],
            all(m not in settled['hand'] for m in members)]
        return list(zip(names, vals))

    bracket_space = []
    for combo in itertools.combinations(eligible, 3):
        sat = bracket_constraints(combo)
        bracket_space.append((sum(1 for _, ok in sat if ok),
                              sum(settled['charged'][m] for m in combo),
                              combo, sat))
    if not bracket_space:
        raise SystemExit('no squad is eligible for the finals bracket')
    best_count = max(b[0] for b in bracket_space)
    tied = sorted([b for b in bracket_space if b[0] == best_count], key=lambda b: (b[1], b[2]))
    bracket = tied[0]
    bracket_members = list(bracket[2])
    bracket_report = {
        'size': 3,
        'eligibility': 'a squad that is its own effective owner and that took part in at '
                       'least the eligibility match floor of matches',
        'eligible_pool': eligible,
        'eligible_pool_size': len(eligible),
        'candidate_space_size': len(bracket_space),
        'best_satisfied_constraint_count': best_count,
        'satisfied_constraint_count': bracket[0],
        'constraints': [{'index': i + 1, 'name': name, 'satisfied': ok}
                        for i, (name, ok) in enumerate(bracket[3])],
        'tie_break': 'least combined charged upkeep, then the member keys sorting first',
        'tie_break_combined_charged_upkeep': bracket[1],
        'members': [per_squad[m] | {'ladder_weight': registry.ladder_weight(m),
                                    'levy_rate': registry.levy_rate(m)}
                    for m in bracket_members],
        'members_by_conference': {
            c: [m for m in bracket_members if registry.conference(m) == c]
            for c in sorted({registry.conference(m) for m in bracket_members})},
        'combined_ladder_weight': sum(registry.ladder_weight(m) for m in bracket_members),
        'combined_gross_output': sum(gross[m] for m in bracket_members),
        'combined_charged_upkeep': sum(settled['charged'][m] for m in bracket_members),
    }

    # -- review docket
    match_rows = []
    for logical in sorted(settled['matches']):
        m = settled['matches'][logical]
        registered = [(k, m['gross'][k]) for k in m['participants'] if k in registry.by_key]
        if registered:
            top = max(v for _, v in registered)
            leader = sorted(k for k, v in registered if v == top)[0]
            conf = registry.conference(leader)
        else:
            leader, conf = None, None
        match_rows.append({
            'match_id': m['match_id'], 'logical_segment': logical,
            'conference': conf, 'conference_of': leader,
            'voided_output': sum(m['voided'].values()),
            'escape_directive_records': m['escape_records'],
            'review_index': sum(m['voided'].values()) + policy['review_weight'] * m['escape_records'],
        })
    match_rows.sort(key=lambda r: r['match_id'])
    cap = policy['docket_conference_cap']
    pool = collections.defaultdict(list)
    for row in match_rows:
        pool[row['conference']].append(row)
    reduced = []
    for conf, rows in pool.items():
        rows.sort(key=lambda r: (-r['review_index'], r['match_id']))
        if len(rows) > cap:
            cut = rows[cap - 1]['review_index']
            reduced.extend(r for r in rows if r['review_index'] >= cut)
        else:
            reduced.extend(rows)
    size = policy['docket_size']
    best = None
    for combo in itertools.combinations(reduced, size):
        counts = collections.Counter(r['conference'] for r in combo)
        if any(v > cap for v in counts.values()):
            continue
        total = sum(r['review_index'] for r in combo)
        match_ids = tuple(sorted(r['match_id'] for r in combo))
        if best is None or (-total, match_ids) < (-best[0], best[1]):
            best = (total, ids, combo)
    if best is None:
        raise SystemExit('no review docket satisfies the conference cap')
    docket_matches = sorted(best[2], key=lambda r: r['match_id'])
    docket_report = {
        'size': size,
        'conference_cap': cap,
        'review_weight': policy['review_weight'],
        'matches_considered': len(match_rows),
        'candidate_space_size': len(reduced),
        'election': 'the largest total review index, ties settled by match identifiers '
                    'sorting first',
        'total_review_index': best[0],
        'conference_spread': dict(collections.Counter(r['conference'] for r in docket_matches)),
        'matches': docket_matches,
    }

    summary = {
        'season': {
            'capture': season_cap.filename,
            'capture_identifier': season_cap.capture_id,
            'guard_register': season_cap.guard_register,
            'guards_active': sorted(n for n, on in season_dec['guards'].items() if on),
            'matches': len(settled['matches']),
            'records_recovered': len(recs),
            'distinct_squads': len(per_squad),
            'registered_squads': len(per_squad) - len(unregistered),
            'unregistered_squads': unregistered,
            'absent_entrants': absent,
        },
        'governing_quantities': {
            'engine_tag': engine_tag, 'window_depth': window_depth,
            'escape_sentinel': sentinel, 'season_budget': budget,
            'promotion_threshold': threshold,
        },
        'totals': {
            'gross_output': season_gross_total,
            'voided_output': sum(voided.values()),
            'raw_upkeep': raw_total, 'charged_upkeep': charged_total,
            'overflow_upkeep': overflow_total,
        },
        'kind_ledger': data_quality['kind_ledger'],
        'attribution': {
            'settlement_passes': settled['sweeps'],
            'handovers': {k: settled['hand'][k] for k in sorted(settled['hand'])},
            'closed_sets': closed_sets(settled['hand']),
            'effective_owners': {k: settled['owner'][k] for k in sorted(settled['owner'])
                                 if settled['owner'][k] != k},
        },
        'tier_counts': {t: sum(1 for k in tiers if tiers[k] == t) for t in TIER_ORDER},
        'ranking': ranking,
        'finals_bracket': bracket_report,
        'review_docket': docket_report,
    }

    def axis_scan(values, kind):
        """Try every candidate of one space with the other quantities pinned."""
        out = []
        for v in values:
            tag = engine_tag if kind != 'engine_tag' else v
            depth = window_depth if kind != 'window_depth' else v
            sent = sentinel if kind != 'escape_sentinel' else v
            bud = budget if kind != 'season_budget' else v
            thr = threshold if kind != 'promotion_threshold' else v
            row = {'value': v, 'verdict': 'rejected', 'falsifier': None,
                   'reproduces': None}
            try:
                dec = decode_capture(control, guard_table, tag, depth, sent, roster)
                metrics = compute_control_metrics(dec, registry, bud, thr, roster)
            except DecodeError as exc:
                cid = exc.figure if exc.figure in figures else ids[0]
                row['falsifier'] = {'figure': cid, 'label': figures[cid][0],
                                    'reason': str(exc)}
                out.append(row)
                continue
            failed = first_failure(metrics, ids)
            row['reproduces'] = {cid: metrics.get(metric_key(figures[cid][0]))
                                 for cid in ids}
            if failed is None:
                row['verdict'] = 'accepted'
            else:
                row['falsifier'] = {'figure': failed[0], 'label': failed[1],
                                    'published': failed[2], 'reproduced': failed[3]}
            out.append(row)
        return out

    sentinel_texts = {}
    for line in guards_doc.splitlines():
        if line.strip().startswith('Sentinel roster:'):
            for t, v in zip(backticks(line), sentinel_roster):
                sentinel_texts[v] = t.strip()

    def shown(value, kind):
        return sentinel_texts.get(value, value) if kind == 'escape_sentinel' else value

    audit = {
        'reference_transcript_check': {
            'present': transcript is not None,
            'file': transcript_name,
            'records_compared': len(transcript) if transcript is not None else 0,
            'field_by_field_agreement': transcript_ok,
        },
        'roster_dictionary': {
            'source_capture': reference.filename,
            'capture_identifier': reference.capture_id,
            'size': len(roster),
            'order': roster,
        },
        'unstated_quantities': collections.OrderedDict([
            ('engine_tag', {
                'document': 'replay-guards.md', 'published_roster': engine_roster,
                'candidate_space_size': len(engine_roster),
                'selected': shown(engine_tag, 'engine_tag'),
                'candidates': axis_scan(engine_roster, 'engine_tag'),
                'search': 'every candidate is tried against the control capture with the '
                          'window depth and the escape sentinel held at their pinned values'}),
            ('window_depth', {
                'document': 'replay-guards.md', 'published_roster': depth_roster,
                'candidate_space_size': len(depth_roster),
                'selected': window_depth,
                'candidates': axis_scan(depth_roster, 'window_depth'),
                'search': 'every candidate is tried against the control capture with the '
                          'engine tag and the escape sentinel held at their pinned values'}),
            ('escape_sentinel', {
                'document': 'replay-guards.md', 'published_roster': sentinel_roster,
                'candidate_space_size': len(sentinel_roster),
                'selected': shown(sentinel, 'escape_sentinel'),
                'candidates': axis_scan(sentinel_roster, 'escape_sentinel'),
                'search': 'every candidate is tried against the control capture with the '
                          'engine tag and the window depth held at their pinned values'}),
            ('season_budget', {
                'document': 'upkeep-policy.md', 'published_roster': budget_roster,
                'candidate_space_size': len(budget_roster),
                'selected': budget,
                'candidates': axis_scan(budget_roster, 'season_budget'),
                'search': 'every candidate is tried against the control capture with the '
                          'guard quantities and the promotion threshold held at their '
                          'pinned values'}),
            ('promotion_threshold', {
                'document': 'standings-policy.md', 'published_roster': threshold_roster,
                'candidate_space_size': len(threshold_roster),
                'selected': threshold,
                'candidates': axis_scan(threshold_roster, 'promotion_threshold'),
                'search': 'every candidate is tried against the control capture with the '
                          'guard quantities and the season budget held at their pinned '
                          'values'}),
        ]),
        'guard_candidate_space': {
            'axes': ['engine_tag', 'window_depth', 'escape_sentinel'],
            'combinations_evaluated': len(guard_combinations),
            'combinations_accepted': len(guard_survivors),
            'combinations': guard_combinations,
        },
        'budget_candidate_space': {
            'candidates_evaluated': len(budget_rows),
            'candidates': budget_rows,
        },
        'threshold_candidate_space': {
            'candidates_evaluated': len(threshold_rows),
            'candidates': threshold_rows,
        },
        'attribution_settlement': {
            'pass_count': settled['sweeps'],
            'method': 'a sweep replaces the current owner of every squad by the squad '
                      'that the current owner hands to; closed sets settle on their '
                      'alphabetically first member before sweeping',
            'handovers': {k: settled['hand'][k] for k in sorted(settled['hand'])},
            'closed_sets': closed_sets(settled['hand']),
            'effective_owners': {k: settled['owner'][k] for k in sorted(settled['owner'])
                                 if settled['owner'][k] != k},
        },
        'bracket_candidate_space': {
            'candidate_space_size': len(bracket_space),
            'best_satisfied_constraint_count': best_count,
            'candidates_reaching_the_best': len(tied),
            'constraint_count': 10,
            'constraint_names': [name for name, _ in bracket[3]],
            'tie_break': bracket_report['tie_break'],
            'selected_members': bracket_members,
        },
        'docket_search': {
            'matches_considered': len(match_rows),
            'conference_cap': cap,
            'docket_size': size,
            'total_review_index': docket_report['total_review_index'],
            'selected_match_ids': [r['match_id'] for r in docket_matches],
        },
        'calibration': {
            'control_capture': control.filename,
            'figures_published': len(figures),
            'rows': calibration,
            'all_agree': all(r['agrees'] for r in calibration),
        },
        'policy_figures_in_force': policy,
    }

    write_json(os.path.join(out_dir, 'summary.json'), summary)
    write_json(os.path.join(out_dir, 'data_quality.json'), data_quality)
    write_json(os.path.join(out_dir, 'policy_audit.json'), audit)
    for name, rep in conference_reports.items():
        write_json(os.path.join(out_dir, 'region_%s.json' % name), rep)
    for name, rep in segment_reports.items():
        write_json(os.path.join(out_dir, 'segment_%s.json' % name), rep)
    return {
        'summary': summary, 'data_quality': data_quality, 'audit': audit,
        'conferences': conference_reports, 'segments': segment_reports,
        'bracket': bracket_report, 'docket': docket_report,
        'files_written': (['summary.json', 'data_quality.json', 'policy_audit.json']
                          + ['region_%s.json' % c for c in sorted(conference_reports)]
                          + ['segment_%s.json' % s for s in sorted(segment_reports)]),
    }


def main(argv):
    if len(argv) != 3:
        sys.stderr.write('usage: %s DATA_DIR OUT_DIR\n' % argv[0])
        return 2
    result = run(argv[1], argv[2])
    for name in result['files_written']:
        print('wrote %s' % name)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
