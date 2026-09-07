#!/usr/bin/env python3
"""Settle a sealed tactics league season from its replay journals.

Usage:  python3 derive_report.py DATA_DIR OUT_DIR

Everything the program needs is read from DATA_DIR: the sealed captures, the
policy documents, the division topology, the licence classes and the entrant
registry.  Nine reports are written into OUT_DIR.

The three quantities that govern the settlement (the guard quantities, the
season budget and the promotion threshold) are never written down.  Each is
drawn from a roster published in the material and pinned by reproducing the
figures published beside the control capture.
"""

import hashlib
import json
import os
import re
import struct
import sys
from collections import Counter, defaultdict
from itertools import combinations
from math import gcd

M32 = 1 << 32

# --------------------------------------------------------------------------
# document parsing
# --------------------------------------------------------------------------

GUARD_KEYS = ['keystream', 'chain', 'delta2', 'driftkey', 'segperm', 'backref',
              'endian', 'countbias', 'shadow', 'rotate', 'alias', 'escape']

# ordered (substring, metric) probes used to match a published control figure
# label onto one of the figures this program can reproduce
FIGURE_PROBES = [
    ('champion', 'champion_entrants'),
    ('contender', 'contender_entrants'),
    ('qualifier', 'qualifier_entrants'),
    ('provisional', 'provisional_entrants'),
    ('match', 'matches_opened'),
    ('record', 'records_recovered'),
    ('distinct', 'distinct_squads'),
    ('gross', 'gross_output'),
    ('voided', 'voided_output'),
    ('raw', 'raw_upkeep'),
    ('charged', 'charged_upkeep'),
    ('overflow', 'overflow_upkeep'),
]

KIND_ROLE_PATTERNS = [
    ('deploy', r'deploy'),
    ('output', r'reports output|output'),
    ('eliminate', r'eliminat'),
    ('disband', r'disband'),
    ('concede', r'conced'),
    ('transfer', r'hands its entry|hand over|transfer'),
    ('close', r'clos'),
]

WORD_NUMBERS = {'zero': 0, 'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
                'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10,
                'eleven': 11, 'twelve': 12, 'thirteen': 13, 'fourteen': 14,
                'fifteen': 15, 'sixteen': 16, 'seventeen': 17, 'eighteen': 18,
                'nineteen': 19, 'twenty': 20}


def read_documents(data_dir):
    """Every readable document in the data directory, keyed by file name."""
    docs = {}
    for name in sorted(os.listdir(data_dir)):
        path = os.path.join(data_dir, name)
        if not os.path.isfile(path):
            continue
        with open(path, 'rb') as fh:
            raw = fh.read()
        try:
            docs[name] = raw.decode('utf-8')
        except UnicodeDecodeError:
            docs[name] = None
    return docs


def backtick_roster(docs, label):
    """A roster published as a `label:` line followed by backticked items."""
    for text in docs.values():
        if text is None:
            continue
        for line in text.splitlines():
            m = re.match(r'\s*' + re.escape(label) + r'\s*:\s*(.+?)\s*$', line)
            if m:
                items = re.findall(r'`([^`]*)`', m.group(1))
                if items:
                    return [it.strip() for it in items]
    raise SystemExit('roster %r not found in the material' % label)


def labelled_figure(docs, label):
    """A scalar figure published as `label: `value``."""
    for text in docs.values():
        if text is None:
            continue
        for line in text.splitlines():
            m = re.match(r'\s*' + re.escape(label) + r'\s*:\s*`([^`]*)`\s*$', line)
            if m:
                return m.group(1).strip()
    return None


def parse_number(text):
    if text is None:
        return None
    text = text.strip()
    if text.lower().startswith('0x'):
        return int(text, 16)
    if text.isdigit():
        return int(text)
    return WORD_NUMBERS.get(text.lower())


def parse_guard_table(docs):
    """bit -> guard name, from the replay guard register table."""
    table = {}
    for text in docs.values():
        if text is None:
            continue
        for line in text.splitlines():
            m = re.match(r'\|\s*(\d+)\s*\|\s*([a-z0-9]+)\s*\|', line)
            if m and 0 <= int(m.group(1)) < 32:
                table[int(m.group(1))] = m.group(2)
    return table


def parse_kind_table(docs):
    """event kind -> role, from the event kind table."""
    roles = {}
    for text in docs.values():
        if text is None:
            continue
        for line in text.splitlines():
            m = re.match(r'\|\s*(\d+)\s*\|\s*([^|]+)\|\s*([^|]*)\|', line)
            if not m:
                continue
            kind, meaning = int(m.group(1)), m.group(2).strip()
            if not meaning or meaning.lower() == 'meaning':
                continue
            for role, pat in KIND_ROLE_PATTERNS:
                if re.search(pat, meaning, re.I):
                    roles[kind] = role
                    break
    return roles


def parse_control_figures(docs):
    """The published calibration figures: [(id, label, value), ...]."""
    figures = []
    for name, text in sorted(docs.items()):
        if text is None:
            continue
        for line in text.splitlines():
            m = re.match(r'\s*-\s*(C\d+)\s+(.+?)\s*:\s*(-?\d+)\s*$', line)
            if m:
                figures.append((m.group(1), m.group(2).strip(), int(m.group(3))))
    figures.sort(key=lambda f: f[0])
    return figures


def parse_csv(text):
    rows = [line for line in text.splitlines() if line.strip()]
    header = [h.strip() for h in rows[0].split(',')]
    out = []
    for row in rows[1:]:
        cells = [c.strip() for c in row.split(',')]
        out.append(dict(zip(header, cells)))
    return out


class Policy(object):
    """Every published figure, read from the material rather than assumed."""

    def __init__(self, docs):
        self.engine_tags = backtick_roster(docs, 'Engine tag roster')
        self.window_depths = [int(x) for x in backtick_roster(docs, 'Window depth roster')]
        self.sentinels = [parse_number(x) for x in backtick_roster(docs, 'Sentinel roster')]
        self.budgets = [parse_number(x) for x in backtick_roster(docs, 'Budget cap roster')]
        self.thresholds = [parse_number(x) for x in
                           backtick_roster(docs, 'Promotion threshold roster')]
        self.guard_bits = parse_guard_table(docs)
        self.kind_roles = parse_kind_table(docs)
        # a figure the material fails to publish is treated as non binding rather
        # than allowed to break the settlement
        self.eligibility_floor = parse_number(labelled_figure(docs, 'Eligibility match floor')) or 0
        self.prominence = parse_number(labelled_figure(docs, 'Ladder weight prominence')) or 0
        self.standing_floor = parse_number(labelled_figure(docs, 'Standing points floor')) or 0
        self.weight_floor = parse_number(labelled_figure(docs, 'Combined ladder weight floor')) or 0
        self.gross_floor = parse_number(labelled_figure(docs, 'Combined gross output floor')) or 0
        self.review_weight = parse_number(labelled_figure(docs, 'Review weight')) or 0
        self.docket_size = parse_number(labelled_figure(docs, 'Docket size'))
        self.docket_cap = parse_number(labelled_figure(docs, 'Docket conference cap'))
        self.bracket_size = self._bracket_size(docs)
        self.control_figures = parse_control_figures(docs)
        if not self.kind_roles:
            self.kind_roles = {1: 'deploy', 2: 'output', 3: 'eliminate', 4: 'disband',
                               5: 'concede', 6: 'transfer', 8: 'close'}

    @staticmethod
    def _bracket_size(docs):
        for text in docs.values():
            if text is None:
                continue
            m = re.search(r'bracket holds exactly\s+(\w+)\s+squads', text)
            if m:
                n = parse_number(m.group(1))
                if n:
                    return n
        return 3                  # a size the material fails to state

    def figure_metric(self, label):
        low = label.lower()
        for probe, metric in FIGURE_PROBES:
            if probe in low:
                return metric
        return None

    def role_of(self, kind):
        return self.kind_roles.get(kind)


# --------------------------------------------------------------------------
# capture decoding
# --------------------------------------------------------------------------

def rot_left(b, n):
    n %= len(b)
    return b[n:] + b[:n] if n else b


def rot_right(b, n):
    n %= len(b)
    return b[len(b) - n:] + b[:len(b) - n] if n else b


class Capture(object):
    """A sealed journal: envelope, segment envelopes and payload frames."""

    def __init__(self, raw, name):
        self.name = name
        self.raw = raw
        if raw[0:4] != b'RJNL':
            raise ValueError('not a journal')
        self.version = raw[4]
        self.reserved = raw[5]
        self.guard_register = struct.unpack('<H', raw[6:8])[0]
        # the endian guard lays the capture nonce down most significant byte first
        if self.guard('endian'):
            self.nonce = struct.unpack('>I', raw[8:12])[0]
        else:
            self.nonce = struct.unpack('<I', raw[8:12])[0]
        (self.segments_declared, self.records_declared, self.base_tick,
         self.capture_id, self.roster_declared, self.envelope_checksum) = \
            struct.unpack('<IIIIII', raw[12:36])
        self.envelope_checksum_computed = sum(raw[:32]) % M32
        self.segments = self._walk()
        self.trailing_bytes = len(raw) - self._end

    def guard(self, name):
        for bit, gname in GUARD_BIT_TABLE.items():
            if gname == name:
                return bool(self.guard_register & (1 << bit))
        return False

    def active_guards(self):
        return [GUARD_BIT_TABLE[b] for b in sorted(GUARD_BIT_TABLE)
                if self.guard_register & (1 << b)]

    def _walk(self):
        raw, off, segs = self.raw, 36, []
        while off + 28 <= len(raw):
            if raw[off:off + 4] != b'SEG0':
                break
            slot, match_id, count, salt, width, chk = struct.unpack('<IIIIII',
                                                                    raw[off + 4:off + 28])
            if width < 0 or off + 28 + width > len(raw):
                break
            payload = raw[off + 28:off + 28 + width]
            segs.append(dict(slot=slot, match_id=match_id, count_declared=count,
                             salt=salt, width=width, checksum=chk,
                             payload=payload,
                             payload_checksum_computed=sum(payload) % M32,
                             storage_pos=len(segs)))
            off += 28 + width
        self._end = off
        return segs

    # -- segment permutation -------------------------------------------
    def step(self):
        n = len(self.segments)
        if n == 0:
            return 0
        base = ((self.nonce >> 8) % n) + 2
        s = base
        while gcd(s, n) != 1:
            s += 1
        return s

    def logical_segments(self):
        n = len(self.segments)
        if n == 0 or not self.guard('segperm'):
            out = list(self.segments)
        else:
            step = self.step()
            out = [None] * n
            for i, seg in enumerate(self.segments):
                out[(self.nonce + i * step) % n] = seg
        for i, seg in enumerate(out):
            seg['logical_index'] = i
        return out


GUARD_BIT_TABLE = {}


def decode_records(cap, params):
    """Recover the record stream of a capture in logical order.

    Returns (records, notes).  A record is never dropped for being malformed;
    it is returned with ``bad`` set so that a candidate reading can be scored
    against the published control figures instead of crashing.
    """
    tag = params['engine_tag']
    window = params['window_depth']
    sentinel = params['sentinel']
    dictionary = params['dictionary']
    mask_after_rotate = params['mask_after_rotate']
    notes = Counter()
    records = []
    bias = (sum(ord(ch) for ch in tag) % 11) if cap.guard('countbias') else 0
    notes['count_bias'] = bias
    prev_tick = cap.nonce              # the chain runs from the capture nonce
    running_sum = 0                    # the drift key accumulates decoded values
    backref = []                       # least recently seen .. most recently seen
    ordinal = 0
    for seg in cap.logical_segments():
        eff_salt = (seg['salt'] ^ running_sum) % M32 if cap.guard('driftkey') else seg['salt']
        seg['eff_salt'] = eff_salt
        payload = seg['payload']
        pos = 0
        d1 = 0
        prev_tick_in_seg = 0
        wanted = seg['count_declared'] - bias
        for idx in range(wanted):
            if pos + 2 > len(payload):
                notes['truncated'] += 1
                break
            visible, shadow = payload[pos], payload[pos + 1]
            width = (shadow ^ (eff_salt & 0xFF)) if cap.guard('shadow') else visible
            if width < 4 or pos + 2 + width > len(payload):
                notes['bad_width'] += 1
                break
            blob = payload[pos + 2:pos + 2 + width]
            pos += 2 + width
            if cap.guard('keystream') or cap.guard('rotate'):
                stream = hashlib.sha256(('%s:%d:%d:%d' % (
                    tag, seg['match_id'], eff_salt, idx)).encode('ascii')).digest()
                stream = stream[:width]
                turn = (eff_salt + ordinal) % width
                if cap.guard('keystream') and cap.guard('rotate'):
                    if mask_after_rotate:      # sealed rotate left, then masked
                        body = bytes(a ^ b for a, b in zip(blob, stream))
                        body = rot_right(body, turn)
                    else:                      # sealed masked, then rotated left
                        body = rot_right(blob, turn)
                        body = bytes(a ^ b for a, b in zip(body, stream))
                elif cap.guard('keystream'):
                    body = bytes(a ^ b for a, b in zip(blob, stream))
                else:
                    body = rot_right(blob, turn)
            else:
                body = blob
            directives = []
            p = 0
            if cap.guard('escape'):
                while p + 1 < len(body) and body[p] == sentinel and len(directives) < 2:
                    directives.append(((body[p + 1] >> 4) & 0xF, body[p + 1] & 0xF))
                    p += 2
            frame = body[p:]
            bad = False
            if not frame:
                frame, bad = b'\x00' * 14, True
                notes['bad_frame'] += 1
            kind = frame[0]
            mode = frame[1] if len(frame) > 1 else -1
            attr_width = 3 if mode == 0 else 1
            need = 2 + attr_width + 2 + 4 + 4
            if len(frame) < need:
                # a malformed frame is padded out rather than allowed to break
                # the reading: every candidate must stay scorable against the
                # published figures
                frame = frame.ljust(need, b'\x00')
                bad = True
                notes['bad_frame'] += 1
            if kind not in KIND_ROLES or len(frame) != need:
                bad = True
                notes['bad_frame'] += 1
            key = None
            if mode == 0:
                key = frame[2:5].decode('ascii', 'replace')
                if not re.match(r'^[A-Za-z0-9]{3}$', key):
                    bad = True
                    notes['bad_key'] += 1
            elif mode == 1:
                pos1 = frame[2]
                if pos1 < len(backref) and pos1 < window:
                    key = backref[pos1]
                else:
                    notes['bad_attr'] += 1
            elif mode == 2:
                pos2 = frame[2]
                if pos2 < len(dictionary):
                    key = dictionary[pos2]
                else:
                    notes['bad_attr'] += 1
            else:
                notes['bad_attr'] += 1
            q = 2 + attr_width
            unit = struct.unpack('<H', frame[q:q + 2])[0]
            tick_raw = struct.unpack('<I', frame[q + 2:q + 6])[0]
            value_raw = struct.unpack('<I', frame[q + 6:q + 10])[0]
            if cap.guard('delta2'):
                d1 = (d1 + tick_raw) % M32
                tick = (prev_tick_in_seg + d1) % M32
            else:
                tick = tick_raw
            prev_tick_in_seg = tick
            value = (value_raw ^ prev_tick) % M32 if cap.guard('chain') else value_raw
            prev_tick = tick
            running_sum = (running_sum + value) % M32
            if key is not None:
                if key in backref:
                    backref.remove(key)
                backref.append(key)
                if len(backref) > window:
                    del backref[:len(backref) - window]
            owner = key
            for op, arg in directives:
                if op == 1:
                    if arg < len(dictionary):
                        owner = dictionary[arg]
                elif op == 2:
                    if owner in dictionary:
                        owner = dictionary[(dictionary.index(owner) + arg) % len(dictionary)]
            records.append(dict(kind=kind, mode=mode, wire_key=key, key=owner,
                                directives=directives, unit=unit, tick=tick, value=value,
                                match_id=seg['match_id'], segment=seg,
                                ordinal_in_segment=idx, ordinal=ordinal,
                                width=width, bad=bad))
            ordinal += 1
    return records, notes


def build_dictionary(cap, params):
    """The roster dictionary: distinct wire attribution keys of the reference
    capture, in the order each is first met in logical order."""
    dictionary = []
    for _ in range(8):
        params = dict(params, dictionary=list(dictionary))
        records, _ = decode_records(cap, params)
        fresh = []
        for rec in records:
            k = rec['wire_key']
            if k is not None and k not in fresh:
                fresh.append(k)
        if fresh == dictionary:
            break
        dictionary = fresh
    return dictionary


# --------------------------------------------------------------------------
# settlement
# --------------------------------------------------------------------------

class Registry(object):
    """The entrant registry, the division topology and the licence classes."""

    SHAPES = [
        (('entrant_id', 'squad_key', 'division', 'licence_class'), 'entrants'),
        (('division', 'conference', 'ladder_weight'), 'divisions'),
        (('licence_class', 'segment', 'levy_rate'), 'licences'),
    ]

    def __init__(self, docs):
        self.entrants = {}
        self.divisions = {}
        self.licences = {}
        for name, text in sorted(docs.items()):
            if text is None or ',' not in text.splitlines()[0]:
                continue
            header = [h.strip() for h in text.splitlines()[0].split(',')]
            for columns, attr in self.SHAPES:
                if not set(columns) <= set(header):
                    continue
                for row in parse_csv(text):
                    if not row.get(columns[0]):
                        continue
                    if attr == 'entrants':
                        self.entrants[row['squad_key']] = dict(
                            entrant_id=row['entrant_id'], division=row['division'],
                            licence_class=row['licence_class'],
                            ranked=row.get('is_ranked', 'no').lower() == 'yes')
                    elif attr == 'divisions':
                        self.divisions[row['division']] = dict(
                            conference=row['conference'],
                            ladder_weight=int(row['ladder_weight']))
                    else:
                        self.licences[row['licence_class']] = dict(
                            segment=row['segment'], levy_rate=int(row['levy_rate']))
                break
        self.conferences = sorted(set(d['conference'] for d in self.divisions.values()))
        self.segments = sorted(set(l['segment'] for l in self.licences.values()))

    def entry(self, key):
        return self.entrants.get(key)

    def conference_of(self, key):
        e = self.entry(key)
        if e and e['division'] in self.divisions:
            return self.divisions[e['division']]['conference']
        return None

    def segment_of(self, key):
        e = self.entry(key)
        if e and e['licence_class'] in self.licences:
            return self.licences[e['licence_class']]['segment']
        return None

    def ladder_weight(self, key):
        e = self.entry(key)
        if e and e['division'] in self.divisions:
            return self.divisions[e['division']]['ladder_weight']
        return 1

    def levy_rate(self, key):
        e = self.entry(key)
        if e and e['licence_class'] in self.licences:
            return self.licences[e['licence_class']]['levy_rate']
        return 1


def settle_capture(cap, params, policy, registry):
    """Decode a capture and settle every match in it, in logical order."""
    records, notes = decode_records(cap, params)
    matches = []
    for seg in cap.logical_segments():
        rs = [r for r in records if r['segment'] is seg]
        matches.append(dict(match_id=seg['match_id'], logical_index=seg['logical_index'],
                            records=rs, slot=seg['storage_pos'], squads={}, close_tick=None))
    for m in matches:
        for rec in m['records']:
            if policy.role_of(rec['kind']) == 'close' and m['close_tick'] is None:
                m['close_tick'] = rec['tick']
        for rec in m['records']:
            role = policy.role_of(rec['kind'])
            key = rec['key']
            if role == 'close' or key is None:
                continue
            sq = m['squads'].setdefault(key, dict(deploy=[], elim={}, disband={},
                                                  concede=None, outputs=[],
                                                  events=0, units=set()))
            sq['events'] += 1
            sq['units'].add(rec['unit'])
            if role == 'deploy':
                sq['deploy'].append((rec['unit'], rec['tick'], rec['value']))
            elif role == 'output':
                sq['outputs'].append((rec['unit'], rec['tick'], rec['value']))
            elif role == 'eliminate':
                sq['elim'][rec['unit']] = rec['tick']
            elif role == 'disband':
                sq['disband'][rec['unit']] = rec['tick']
            elif role == 'concede':
                sq['concede'] = rec['tick']
        for key, sq in m['squads'].items():
            gross = voided = 0
            for unit, tick, val in sq['outputs']:
                withdrawn = (unit in sq['elim'] and tick >= sq['elim'][unit]) or \
                            (sq['concede'] is not None and tick >= sq['concede'])
                if withdrawn:
                    voided += val
                else:
                    gross += val
            raw = 0
            for unit, dtick, rate in sq['deploy']:
                end = sq['disband'][unit] if unit in sq['disband'] else m['close_tick']
                if end is None:
                    end = dtick
                raw += rate * max(0, end - dtick)
            sq['gross'] = gross
            sq['voided'] = voided
            sq['raw'] = raw
            sq['conceded'] = sq['concede'] is not None
    return records, notes, matches


def allocate_upkeep(matches, budget):
    """Charge raw upkeep against one season budget, match by match."""
    remaining = {}
    charged = defaultdict(int)
    overflow = defaultdict(int)
    per_match = []
    for m in matches:
        row = {}
        for key in sorted(m['squads']):
            raw = m['squads'][key]['raw']
            if key not in remaining:
                remaining[key] = budget
            take = min(remaining[key], raw)
            remaining[key] -= take
            charged[key] += take
            overflow[key] += raw - take
            row[key] = take
        per_match.append(row)
    return charged, overflow, per_match


def season_table(matches, registry, budget):
    """Per squad aggregates, match scores, standing points and streak credit."""
    charged, overflow, per_match = allocate_upkeep(matches, budget)
    squads = {}
    for i, m in enumerate(matches):
        for key, sq in sorted(m['squads'].items()):
            take = per_match[i].get(key, 0)
            sq['charged'] = take
            sq['overflow'] = sq['raw'] - take
            sq['match_score'] = sq['gross'] * registry.ladder_weight(key) - \
                take * registry.levy_rate(key)
            s = squads.setdefault(key, dict(key=key, matches=0, gross=0, voided=0, raw=0,
                                            charged=0, overflow=0, awards=[]))
            s['matches'] += 1
            s['gross'] += sq['gross']
            s['voided'] += sq['voided']
            s['raw'] += sq['raw']
            s['charged'] += take
            s['overflow'] += sq['overflow']
        # standing points: the highest match score among squads that did not concede
        live = sorted(k for k in m['squads'] if not m['squads'][k]['conceded'])
        winners = []
        if live:
            best = max(m['squads'][k]['match_score'] for k in live)
            winners = sorted(k for k in live if m['squads'][k]['match_score'] == best)
        for key in sorted(m['squads']):
            award = 3 if (key in winners and len(winners) == 1) else \
                    (1 if key in winners else 0)
            squads[key]['awards'].append(award)
    for key, s in squads.items():
        streak = 0
        run = 0
        for award in s['awards']:
            if award == 3:            # an outright win extends the run
                run += 1
                if run > 1:
                    streak += 2
            else:                     # a shared highest score, a loss or a walk over
                run = 0
        s['streak_credit'] = streak
        s['standing_points'] = sum(s['awards']) + streak
        s['season_score'] = sum(m['squads'][key]['match_score'] for m in matches
                                if key in m['squads'])
    return squads, charged, overflow


def tier_of(score, threshold):
    if threshold is None:
        return None
    if score >= 3 * threshold:
        return 'champion'
    if score >= 2 * threshold:
        return 'contender'
    if score >= threshold:
        return 'qualifier'
    return 'provisional'


def settle_ownership(records, policy, registry, dictionary):
    """Effective owners from the entry hand overs, by repeated sweeps."""
    hands = {}
    order = []
    for rec in records:
        if policy.role_of(rec['kind']) == 'transfer' and rec['key'] is not None:
            target = dictionary[rec['value'] % len(dictionary)] if dictionary else rec['key']
            if rec['key'] not in hands:
                order.append(rec['key'])
            hands[rec['key']] = target        # the last hand over read stands
    squads = sorted(set(list(hands.keys()) + [t for t in hands.values()]))
    # closed sets settle on the member whose key sorts first
    closed = {}
    for start in hands:
        seen, cur = [], start
        while cur in hands and cur not in seen:
            seen.append(cur)
            cur = hands[cur]
        if cur in seen:                       # the chain came back on itself
            ring = seen[seen.index(cur):]
            canonical = min(ring)
            for k in ring:
                closed[k] = canonical
    step = {}
    for k in squads:
        step[k] = closed.get(k, hands.get(k, k))
    owner = {k: k for k in squads}
    passes = 0
    while True:
        passes += 1
        new = {k: step.get(v, v) for k, v in owner.items()}
        if new == owner:
            break
        owner = new
    handed = sorted(hands.keys())
    return dict(hands=hands, owner=owner, closed=closed, handed=handed,
                passes=passes, order=order)


# --------------------------------------------------------------------------
# calibration
# --------------------------------------------------------------------------

def control_metrics(records, matches, squads, threshold=None):
    metrics = dict(
        matches_opened=len(matches),
        records_recovered=len(records),
        distinct_squads=len(set(r['key'] for r in records if r['key'] is not None)),
        gross_output=sum(s['gross'] for s in squads.values()),
        voided_output=sum(s['voided'] for s in squads.values()),
        raw_upkeep=sum(s['raw'] for s in squads.values()),
        charged_upkeep=sum(s['charged'] for s in squads.values()),
        overflow_upkeep=sum(s['overflow'] for s in squads.values()))
    if threshold is not None:
        for name in ('champion', 'contender', 'qualifier', 'provisional'):
            metrics[name + '_entrants'] = sum(
                1 for s in squads.values() if tier_of(s['season_score'], threshold) == name)
    return metrics


def compare_figures(metrics, published, policy):
    """First published figure a reading fails to reproduce, or None."""
    for fig_id, label, value in published:
        metric = policy.figure_metric(label)
        if metric is None or metric not in metrics:
            continue
        if metrics[metric] != value:
            return dict(figure=fig_id, label=label, published=value,
                        reproduced=metrics[metric])
    return None


def calibrate(captures, policy, registry, season_cap):
    """Pin the unstated quantities against the published control figures."""
    published = policy.control_figures
    stage_for = {}
    for fig_id, label, value in published:
        metric = policy.figure_metric(label)
        stage_for[fig_id] = {'champion_entrants': 3, 'contender_entrants': 3,
                             'qualifier_entrants': 3, 'provisional_entrants': 3,
                             'charged_upkeep': 2, 'overflow_upkeep': 2}.get(metric, 1)
    stages = {1: [], 2: [], 3: []}
    for fig in published:
        stages[stage_for[fig[0]]].append(fig)

    # the dictionary is published beside the reference capture; if a corpus
    # ships without one, the control slice is the closest witness available
    ref = captures.get('reference') or captures.get('control')
    base = dict(engine_tag=None, window_depth=None, sentinel=None,
                mask_after_rotate=True, dictionary=[])
    combos = []
    for tag in policy.engine_tags:
        for window in policy.window_depths:
            for sentinel in policy.sentinels:
                for mask_after in (True, False):
                    combos.append(dict(engine_tag=tag, window_depth=window,
                                       sentinel=sentinel,
                                       mask_after_rotate=mask_after,
                                       dictionary=[]))
    scored = []
    for combo in combos:
        entry = dict(combo=combo, stage1=None, records=None, notes=None,
                     matches=None, squads=None)
        params = dict(combo)
        if ref is not None:
            try:
                params['dictionary'] = build_dictionary(ref, params)
            except Exception:
                params['dictionary'] = []
        try:
            records, notes, matches = settle_capture(captures['control'], params,
                                                     policy, registry)
        except Exception as exc:
            entry['error'] = '%s: %s' % (type(exc).__name__, exc)
            entry['stage1'] = {}
            # the reading collapsed, so the first figure of the stage kills it
            entry['fail'] = stages[1][0][:2] + (None,) if stages[1] else None
            entry['fail'] = dict(figure=stages[1][0][0], label=stages[1][0][1],
                                 published=stages[1][0][2], reproduced=None) if stages[1] \
                else dict(figure='-', label='no figure published', published=None,
                          reproduced=None)
            scored.append(entry)
            continue
        squads, _, _ = season_table(matches, registry, 0)
        metrics = control_metrics(records, matches, squads)
        entry.update(stage1=metrics, records=records, notes=notes,
                     matches=matches, squads=squads, dictionary=params['dictionary'],
                     fail=compare_figures(metrics, stages[1], policy))
        scored.append(entry)
    ok1 = [e for e in scored if e.get('fail') is None and e.get('stage1')]
    # a figure set that does not exercise a guard leaves several readings
    # alive; the season capture must still decode cleanly under them.
    if len(ok1) > 1 and season_cap is not None:
        for e in ok1:
            try:
                recs, notes = decode_records(season_cap,
                                             dict(e['combo'], dictionary=e['dictionary']))
                e['season_clean'] = (notes['bad_frame'] == 0 and notes['bad_attr'] == 0
                                     and notes['bad_width'] == 0)
            except Exception:
                e['season_clean'] = False
        refined = [e for e in ok1 if e['season_clean']]
        if refined:
            ok1 = refined
    if not ok1:
        raise SystemExit('no reading of the material reproduces the control figures')
    # A window deeper than the true one resolves every position the true one
    # resolves, so it reproduces the figures as well; a shallower one does not.
    # The depth the material pins is therefore the smallest that reproduces.
    ok1.sort(key=lambda e: (0 if e.get('season_clean', True) else 1,
                            e['combo']['window_depth'],
                            policy.engine_tags.index(e['combo']['engine_tag']),
                            policy.sentinels.index(e['combo']['sentinel']),
                            0 if e['combo']['mask_after_rotate'] else 1))
    chosen = ok1[0]
    for e in scored:
        e['passes_stage1'] = e.get('fail') is None and e.get('stage1') is not None

    # stage 2: the season budget
    budget_rows = []
    for budget in policy.budgets:
        squads, _, _ = season_table(chosen['matches'], registry, budget)
        metrics = control_metrics(chosen['records'], chosen['matches'], squads)
        budget_rows.append(dict(value=budget, metrics=metrics,
                                fail=compare_figures(metrics, stages[2], policy)))
    ok_budget = [r for r in budget_rows if r['fail'] is None]
    if not ok_budget:
        raise SystemExit('no budget on the published roster reproduces the control figures')
    budget = ok_budget[0]['value']

    # stage 3: the promotion threshold
    threshold_rows = []
    for threshold in policy.thresholds:
        squads, _, _ = season_table(chosen['matches'], registry, budget)
        metrics = control_metrics(chosen['records'], chosen['matches'], squads, threshold)
        threshold_rows.append(dict(value=threshold, metrics=metrics,
                                   fail=compare_figures(metrics, stages[3], policy)))
    ok_threshold = [r for r in threshold_rows if r['fail'] is None]
    if not ok_threshold:
        raise SystemExit('no threshold on the published roster reproduces the control figures')
    threshold = ok_threshold[0]['value']

    return dict(chosen=chosen, scored=scored, budget_rows=budget_rows,
                threshold_rows=threshold_rows, budget=budget, threshold=threshold,
                stages=stages)


# --------------------------------------------------------------------------
# bracket and docket
# --------------------------------------------------------------------------

BRACKET_CONSTRAINTS = [
    ('prominence', 'at least one member sits in a division whose ladder weight '
                   'reaches the prominence figure'),
    ('segments', 'both entrant segments appear among the members'),
    ('standing_floor', 'every member holds at least the standing points floor'),
    ('conferences', 'no two members share a conference'),
    ('ranked', 'at least one member is a ranked entrant'),
    ('weight_floor', 'the ladder weights of the members sum to at least the '
                     'combined ladder weight floor'),
    ('no_overflow', 'no member carries any overflow upkeep'),
    ('champion', 'at least one member holds the champion tier'),
    ('gross_floor', 'the gross output of the members sums to at least the '
                    'combined gross output floor'),
    ('no_handover', 'no member has handed its entry to another squad'),
]


def bracket_constraints(keys, squads, policy, registry, ownership):
    """Which of the ten published wishes a candidate bracket meets."""
    weights = [registry.ladder_weight(k) for k in keys]
    segments = [registry.segment_of(k) for k in keys]
    conferences = [registry.conference_of(k) for k in keys]
    tiers = [squads[k]['tier'] for k in keys]
    out = {}
    out['prominence'] = any(w >= policy.prominence for w in weights)
    out['segments'] = all(s in segments for s in registry.segments)
    out['standing_floor'] = all(squads[k]['standing_points'] >= policy.standing_floor
                                for k in keys)
    out['conferences'] = len(set(conferences)) == len(conferences)
    out['ranked'] = any(registry.entry(k) and registry.entry(k)['ranked'] for k in keys)
    out['weight_floor'] = sum(weights) >= policy.weight_floor
    out['no_overflow'] = all(squads[k]['overflow'] == 0 for k in keys)
    out['champion'] = 'champion' in tiers
    out['gross_floor'] = sum(squads[k]['gross'] for k in keys) >= policy.gross_floor
    out['no_handover'] = all(k not in ownership['handed'] for k in keys)
    return out


def elect_bracket(squads, policy, registry, ownership):
    """The bracket meeting the most wishes; ties by least charged upkeep,
    then by member keys sorting first."""
    eligible = sorted(k for k, s in squads.items()
                      if ownership['owner'].get(k, k) == k
                      and s['matches'] >= policy.eligibility_floor)
    space = list(combinations(eligible, policy.bracket_size))
    best = None
    for keys in space:
        flags = bracket_constraints(keys, squads, policy, registry, ownership)
        met = sum(1 for v in flags.values() if v)
        upkeep = sum(squads[k]['charged'] for k in keys)
        if best is None:
            better = True
        elif (met, -upkeep) > (best['met'], -best['upkeep']):
            better = True
        elif (met, -upkeep) == (best['met'], -best['upkeep']) and list(keys) < best['keys']:
            better = True
        else:
            better = False
        if better:
            best = dict(keys=list(keys), flags=flags, met=met, upkeep=upkeep)
    return dict(eligible=eligible, space=space, best=best)


def elect_docket(matches, policy, registry):
    """The matches carrying the largest total review index, never more than the
    conference cap from any one conference."""
    rows = []
    for m in matches:
        voided = sum(sq['voided'] for sq in m['squads'].values())
        escapes = sum(1 for r in m['records'] if r['directives'])
        # the match belongs to the conference of the registered squad that
        # reported the most gross output in it; ties go to the first sorting key
        best_key, best_gross = None, None
        for key in sorted(m['squads']):
            if registry.entry(key) is None:
                continue
            if best_gross is None or m['squads'][key]['gross'] > best_gross:
                best_key, best_gross = key, m['squads'][key]['gross']
        rows.append(dict(match_id=m['match_id'], voided_output=voided,
                         escape_records=escapes,
                         review_index=voided + policy.review_weight * escapes,
                         conference=registry.conference_of(best_key) if best_key else None,
                         leading_squad=best_key, leading_gross=best_gross))
    size = min(policy.docket_size or len(rows), len(rows))
    groups = defaultdict(list)
    for row in rows:
        groups[row['conference']].append(row)
    # dynamic program over the conference groups
    state = {0: (0, tuple())}
    for conf in sorted(groups, key=lambda c: (c is not None, c or '')):
        pool = sorted(groups[conf], key=lambda r: r['match_id'])
        cap = (policy.docket_cap or len(pool)) if conf is not None else len(pool)
        options = [()]
        for n in range(1, min(cap, size) + 1):
            options.extend(combinations(pool, n))
        nxt = {}
        for used in sorted(state):
            total, ids = state[used]
            for opt in options:
                key = used + len(opt)
                if key > size:
                    continue
                score = total + sum(r['review_index'] for r in opt)
                merged = tuple(sorted(ids + tuple(r['match_id'] for r in opt)))
                rank = (score, [-i for i in merged])
                if key not in nxt or rank > nxt[key][0]:
                    nxt[key] = (rank, (score, merged))
        state = {k: v[1] for k, v in nxt.items()}
    # the published cap can make the published size unreachable; the docket then
    # holds as many matches as the cap does allow
    used = size
    while used > 0 and used not in state:
        used -= 1
    chosen_ids = state[used][1] if used in state else tuple()
    by_id = {r['match_id']: r for r in rows}
    return dict(rows=rows,
                chosen=[by_id[i] for i in sorted(chosen_ids)],
                total=sum(by_id[i]['review_index'] for i in chosen_ids),
                requested_size=size, size=used,
                size_relaxed=(used != size),
                space_size=comb_count(len(rows), size))


def comb_count(n, k):
    if k < 0 or k > n:
        return 0
    result = 1
    for i in range(k):
        result = result * (n - i) // (i + 1)
    return result


# --------------------------------------------------------------------------
# reports
# --------------------------------------------------------------------------

def entry_row(key, s, registry, policy, threshold, ownership):
    e = registry.entry(key)
    row = dict(squad_key=key,
               entrant_id=e['entrant_id'] if e else None,
               division=e['division'] if e else None,
               conference=registry.conference_of(key),
               licence_class=e['licence_class'] if e else None,
               segment=registry.segment_of(key),
               matches=s['matches'], gross_output=s['gross'],
               voided_output=s['voided'], charged_upkeep=s['charged'],
               overflow_upkeep=s['overflow'], season_score=s['season_score'],
               standing_points=s['standing_points'],
               streak_credit=s['streak_credit'],
               tier=tier_of(s['season_score'], threshold))
    return row


def group_report(kind, name, rows, divisions):
    tiers = Counter(r['tier'] for r in rows)
    return dict(group_kind=kind,
                group_name=name,
                entrants=len(rows),
                divisions=sorted(set(divisions)),
                entries=sorted(rows, key=lambda r: r['squad_key']),
                gross_output=sum(r['gross_output'] for r in rows),
                voided_output=sum(r['voided_output'] for r in rows),
                charged_upkeep=sum(r['charged_upkeep'] for r in rows),
                overflow_upkeep=sum(r['overflow_upkeep'] for r in rows),
                season_score=sum(r['season_score'] for r in rows),
                standing_points=sum(r['standing_points'] for r in rows),
                tier_counts={t: tiers.get(t, 0) for t in
                             ('champion', 'contender', 'qualifier', 'provisional')})


def write_json(path, payload):
    with open(path, 'w') as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write('\n')


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main(argv):
    if len(argv) != 3:
        sys.stderr.write('usage: derive_report.py DATA_DIR OUT_DIR\n')
        return 2
    data_dir, out_dir = argv[1], argv[2]
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    docs = read_documents(data_dir)
    global GUARD_BIT_TABLE, KIND_ROLES
    policy = Policy(docs)
    GUARD_BIT_TABLE = policy.guard_bits or {i: n for i, n in enumerate(GUARD_KEYS)}
    KIND_ROLES = set(policy.kind_roles)
    registry = Registry(docs)

    caps = []
    for name, text in sorted(docs.items()):
        if text is not None:
            continue
        path = os.path.join(data_dir, name)
        with open(path, 'rb') as fh:
            raw = fh.read()
        if raw[0:4] == b'RJNL':
            caps.append(Capture(raw, name))
    roles = {}
    for cap in caps:
        role = {1: 'reference', 2: 'control', 3: 'season'}.get(cap.capture_id % 10)
        if role:
            roles[role] = cap
    if 'season' not in roles or 'control' not in roles:
        raise SystemExit('the data directory must hold a control slice and a sealed season')

    calib = calibrate(roles, policy, registry, roles.get('season'))
    chosen = calib['chosen']
    params = dict(chosen['combo'], dictionary=chosen['dictionary'])

    season = roles['season']
    records, notes, matches = settle_capture(season, params, policy, registry)
    squads, charged, overflow = season_table(matches, registry, calib['budget'])
    ownership = settle_ownership(records, policy, registry, params['dictionary'])

    for key, s in squads.items():
        s['tier'] = tier_of(s['season_score'], calib['threshold'])
        s['charged'] = charged.get(key, 0)
        s['overflow'] = overflow.get(key, 0)

    # ---------------- conference and segment reports ----------------
    conference_rows = defaultdict(list)
    segment_rows = defaultdict(list)
    division_members = defaultdict(list)
    for key in sorted(squads):
        if registry.entry(key) is None:
            continue                       # unregistered: belongs to no group
        row = entry_row(key, squads[key], registry, policy, calib['threshold'], ownership)
        conference_rows[row['conference']].append(row)
        segment_rows[row['segment']].append(row)
        division_members[row['division']].append(key)
    reports = []
    for conf in registry.conferences:
        divs = [d for d in registry.divisions if registry.divisions[d]['conference'] == conf]
        reports.append(('region_%s.json' % conf,
                        group_report('conference', conf, conference_rows.get(conf, []),
                                     [d for d in divs if d in division_members])))
    for seg in registry.segments:
        divs = [d for d in division_members
                if registry.segment_of(division_members[d][0]) == seg]
        reports.append(('segment_%s.json' % seg,
                        group_report('segment', seg, segment_rows.get(seg, []), divs)))

    # ---------------- data quality ----------------
    quality = {'captures': []}
    for role in ('reference', 'control', 'season'):
        cap = roles.get(role)
        if cap is None:
            continue
        recs = records if role == 'season' else None
        if recs is None:
            pr = dict(params, dictionary=params['dictionary'])
            if role == 'reference':
                pr = dict(pr, dictionary=[])
                try:
                    pr['dictionary'] = build_dictionary(cap, pr)
                except Exception:
                    pr['dictionary'] = []
            try:
                recs, n, ms = settle_capture(cap, pr, policy, registry)
            except Exception as exc:
                quality['captures'].append(dict(file=cap.name, role=role, error=str(exc)))
                continue
        bias = (sum(ord(c) for c in params['engine_tag']) % 11) if cap.guard('countbias') else 0
        distinct = sorted(set(r['key'] for r in recs if r['key'] is not None))
        unreg = [k for k in distinct if registry.entry(k) is None]
        declared = dict(segments=cap.segments_declared, records=cap.records_declared,
                        roster_size=cap.roster_declared)
        effective = dict(segments=cap.segments_declared - bias,
                         records=cap.records_declared - bias,
                         roster_size=cap.roster_declared - bias)
        recovered = dict(segments=len(cap.segments), records=len(recs),
                         distinct_squads=len(distinct), roster_size=len(distinct))
        residuals = {k: effective[k] - recovered[k] for k in effective}
        findings = []
        for name in sorted(residuals):
            value = residuals[name]
            if not value:
                continue
            if name == 'roster_size' and value < 0:
                gap = -value
                if len(unreg) == gap:
                    findings.append(
                        'declares a roster of %d but %d distinct attribution keys were '
                        'recovered on the wire; the %d keys the entrant registry never '
                        'lists (%s) account for the whole difference'
                        % (effective['roster_size'], recovered['roster_size'], gap,
                           ', '.join(unreg)))
                else:
                    findings.append(
                        'declares a roster of %d but %d distinct attribution keys were '
                        'recovered on the wire, a difference of %d; %d of them (%s) are '
                        'keys the entrant registry never lists and the remaining %d are '
                        'registered entrants the declaration does not count'
                        % (effective['roster_size'], recovered['roster_size'], gap,
                           len(unreg), ', '.join(unreg) or 'none', gap - len(unreg)))
            else:
                findings.append('%s residual %+d: %d declared against %d recovered'
                                % (name, value, effective[name], recovered[name]))
        quality['captures'].append(dict(
            file=cap.name, role=role, capture_id=cap.capture_id,
            guard_register=cap.guard_register, active_guards=cap.active_guards(),
            count_bias=bias, declared=declared, declared_effective=effective,
            recovered=recovered,
            residuals=residuals,
            findings=findings,
            envelope_checksum_matches=(cap.envelope_checksum == cap.envelope_checksum_computed),
            payload_checksum_failures=sum(1 for s in cap.segments
                                          if s['checksum'] != s['payload_checksum_computed']),
            trailing_bytes=cap.trailing_bytes,
            registered_squads=len(distinct) - len(unreg),
            unregistered_squads=unreg))
    season_distinct = sorted(squads)
    unregistered = [k for k in season_distinct if registry.entry(k) is None]
    absent = sorted(k for k, e in registry.entrants.items() if k not in squads)
    ledger = Counter(policy.role_of(r['kind']) for r in records)
    gross_total = sum(s['gross'] for s in squads.values())
    voided_total = sum(s['voided'] for s in squads.values())
    raw_total = sum(s['raw'] for s in squads.values())
    charged_total = sum(charged.values())
    overflow_total = sum(overflow.values())
    output_reported = sum(r['value'] for r in records if policy.role_of(r['kind']) == 'output')
    conf_gross = sum(r['gross_output'] for rows in conference_rows.values() for r in rows)
    regressions = 0
    for m in matches:
        ticks = [r['tick'] for r in m['records']]
        regressions += sum(1 for a, b in zip(ticks, ticks[1:]) if b < a)
    unregistered_gross = sum(squads[k]['gross'] for k in unregistered)
    season_findings = []
    if unregistered:
        season_findings.append(
            '%d squads seen on the wire that the entrant registry never lists (%s); '
            'they carry %d of the %d gross output recovered, which is the whole of '
            'the attribution residual because no conference or segment report counts '
            'them' % (len(unregistered), ', '.join(unregistered), unregistered_gross,
                      gross_total))
    if absent:
        season_findings.append(
            '%d registered entrants never appeared on the wire (%s) and so hold no '
            'place in the settlement' % (len(absent), ', '.join(absent)))
    quality['season'] = dict(
        squads=len(season_distinct), registered_squads=len(season_distinct) - len(unregistered),
        unregistered_squads=unregistered,
        absent_entrants=absent,
        matches=len(matches), records=len(records),
        ledger={name: ledger.get(name, 0) for name in
                ('deploy', 'output', 'eliminate', 'disband', 'concede', 'transfer', 'close')},
        attribution_residual=gross_total - conf_gross,
        attribution_residual_unregistered_gross=unregistered_gross,
        findings=season_findings,
        ledger_residuals=dict(
            output_identity=(gross_total + voided_total) - output_reported,
            upkeep_identity=(charged_total + overflow_total) - raw_total,
            kind_ledger_identity=sum(ledger.values()) - len(records)),
        tick_regressions=regressions,
        gross_output=gross_total, voided_output=voided_total,
        raw_upkeep=raw_total, charged_upkeep=charged_total,
        overflow_upkeep=overflow_total)

    # ---------------- policy audit ----------------
    audit = dict(
        unstated_quantities=[],
        attribution_settlement=dict(
            passes=ownership['passes'],
            handovers=[dict(squad=k, target=ownership['hands'][k]) for k in ownership['order']],
            closed_sets=closed_sets_of(ownership),
            effective_owners=ownership['owner'],
            squads_that_handed_their_entry_away=ownership['handed']),
        bracket=dict(),
        calibration=[])

    for dimension, roster, field, rows in (
            ('engine_tag', policy.engine_tags, 'engine_tag', None),
            ('window_depth', policy.window_depths, 'window_depth', None),
            ('sentinel', policy.sentinels, 'sentinel', None)):
        cands = []
        for value in roster:
            best = None
            for e in calib['scored']:
                if e['combo'][field] != value:
                    continue
                if best is None or _rank_entry(e, policy) > _rank_entry(best, policy):
                    best = e
            selected = (chosen['combo'][field] == value)
            falsifier = None
            if not selected:
                falsifier = best.get('fail')
                if not falsifier and best.get('stage1'):
                    # the reading reproduced every published figure and lost only
                    # the ordering; no figure killed it and none is invented
                    falsifier = dict(figure=None, published=None,
                                     note='this value reproduces every published '
                                          'figure; it is set aside by the fallback '
                                          'ordering, which takes the smallest window '
                                          'depth and then the published roster order')
                elif not falsifier:
                    first = policy.control_figures[0]
                    falsifier = dict(figure=first[0], label=first[1],
                                     published=first[2], reproduced=None,
                                     note='the reading collapsed before any figure '
                                          'could be reproduced')
            cands.append(dict(value=value,
                              verdict='accepted' if selected else 'rejected',
                              falsifier=falsifier,
                              figures_reproduced=_figures_matched(best.get('stage1'),
                                                                  policy),
                              reproduced=None if not best.get('stage1') else
                              {k: best['stage1'][k] for k in sorted(best['stage1'])}))
        audit['unstated_quantities'].append(dict(
            quantity=dimension, roster=list(roster), selected=chosen['combo'][field],
            search_space=dict(guard_combinations=len(calib['scored'])),
            candidates=cands))
    for dimension, rows, field, selected in (
            ('season_budget', calib['budget_rows'], 'budget', calib['budget']),
            ('promotion_threshold', calib['threshold_rows'], 'threshold', calib['threshold'])):
        cands = []
        for row in rows:
            fail = row['fail']
            if fail is None and row['value'] != selected:
                fail = dict(figure=None, published=None,
                            note='this value reproduces every published figure; it is '
                                 'set aside by the fallback ordering, which takes the '
                                 'smallest window depth and then the published roster '
                                 'order')
            cands.append(dict(value=row['value'],
                              verdict='accepted' if row['value'] == selected else 'rejected',
                              falsifier=fail,
                              reproduced={k: row['metrics'][k] for k in sorted(row['metrics'])}))
        audit['unstated_quantities'].append(dict(
            quantity=dimension, roster=[r['value'] for r in rows], selected=selected,
            candidates=cands))

    br = elect_bracket(squads, policy, registry, ownership)
    audit['bracket'] = dict(
        size=policy.bracket_size,
        eligibility=dict(own_effective_owner=True, match_floor=policy.eligibility_floor),
        eligible_squads=br['eligible'],
        candidate_space_size=len(br['space']),
        best_satisfied_constraints=(br['best']['met'] if br['best'] else 0),
        constraints=[dict(key=key, text=text, satisfied=br['best']['flags'][key])
                     for key, text in BRACKET_CONSTRAINTS] if br['best'] else [],
        elected=br['best']['keys'] if br['best'] else [],
        tie_break='most constraints met, then least total charged upkeep, '
                  'then member keys sorting first')

    control_records = chosen['records']
    control_squads, _, _ = season_table(chosen['matches'], registry, calib['budget'])
    reproduced = control_metrics(control_records, chosen['matches'], control_squads,
                                 calib['threshold'])
    audit['calibration'] = dict(
        control_capture=roles['control'].name,
        rows=[dict(figure=fig_id, label=label, published=value,
                   metric=policy.figure_metric(label),
                   reproduced=reproduced.get(policy.figure_metric(label)),
                   agrees=(reproduced.get(policy.figure_metric(label)) == value))
              for fig_id, label, value in policy.control_figures],
        all_figures_agree=all(reproduced.get(policy.figure_metric(l)) == v
                              for _i, l, v in policy.control_figures))
    audit['guard_derivation'] = dict(
        engine_tag=chosen['combo']['engine_tag'],
        window_depth=chosen['combo']['window_depth'],
        sentinel=chosen['combo']['sentinel'],
        mask_order='rotate left then mask with the keystream'
                   if chosen['combo']['mask_after_rotate'] else
                   'mask with the keystream then rotate left',
        roster_dictionary=params['dictionary'])

    # ---------------- docket ----------------
    docket = elect_docket(matches, policy, registry)

    # ---------------- summary ----------------
    ranking = sorted(squads.values(),
                     key=lambda s: (-s['season_score'], -s['standing_points'], s['key']))
    standings = []
    for i, s in enumerate(ranking):
        e = registry.entry(s['key'])
        standings.append(dict(rank=i + 1, squad_key=s['key'],
                              entrant_id=e['entrant_id'] if e else None,
                              registered=e is not None,
                              conference=registry.conference_of(s['key']),
                              segment=registry.segment_of(s['key']),
                              division=e['division'] if e else None,
                              licence_class=e['licence_class'] if e else None,
                              ladder_weight=registry.ladder_weight(s['key']),
                              levy_rate=registry.levy_rate(s['key']),
                              is_ranked=bool(e and e['ranked']),
                              matches=s['matches'],
                              match_awards=sum(s['awards']),
                              streak_credit=s['streak_credit'],
                              standing_points=s['standing_points'],
                              gross_output=s['gross'], voided_output=s['voided'],
                              raw_upkeep=s['raw'], charged_upkeep=s['charged'],
                              overflow_upkeep=s['overflow'],
                              season_score=s['season_score'], tier=s['tier'],
                              effective_owner=ownership['owner'].get(s['key'], s['key']),
                              is_own_effective_owner=ownership['owner'].get(s['key'],
                                                                           s['key']) == s['key'],
                              handed_entry_away=s['key'] in ownership['handed']))
    summary = dict(
        season=dict(
            capture=season.name, capture_id=season.capture_id,
            guard_register=season.guard_register,
            active_guards=season.active_guards(),
            base_tick=season.base_tick,
            matches=len(matches), records=len(records),
            squads=len(squads),
            registered_squads=len(squads) - len(unregistered),
            unregistered_squads=unregistered,
            absent_entrants=absent,
            gross_output=gross_total, voided_output=voided_total,
            raw_upkeep=raw_total, charged_upkeep=charged_total,
            overflow_upkeep=overflow_total,
            season_score=sum(s['season_score'] for s in squads.values()),
            standing_points=sum(s['standing_points'] for s in squads.values()),
            tier_counts={t: sum(1 for s in squads.values() if s['tier'] == t)
                         for t in ('champion', 'contender', 'qualifier', 'provisional')},
            rank_order='season score descending, then standing points descending, '
                       'then squad key ascending'),
        conferences={conf: dict(
            entrants=len(conference_rows.get(conf, [])),
            divisions=sorted(d for d in registry.divisions
                             if registry.divisions[d]['conference'] == conf
                             and d in division_members),
            gross_output=sum(r['gross_output'] for r in conference_rows.get(conf, [])),
            season_score=sum(r['season_score'] for r in conference_rows.get(conf, [])),
            standing_points=sum(r['standing_points'] for r in conference_rows.get(conf, [])))
            for conf in registry.conferences},
        segments={seg: dict(
            entrants=len(segment_rows.get(seg, [])),
            gross_output=sum(r['gross_output'] for r in segment_rows.get(seg, [])),
            season_score=sum(r['season_score'] for r in segment_rows.get(seg, [])),
            standing_points=sum(r['standing_points'] for r in segment_rows.get(seg, [])))
            for seg in registry.segments},
        standings=standings,
        ownership=dict(
            sweeps=ownership['passes'],
            handovers=[dict(squad=k, target=ownership['hands'][k]) for k in ownership['order']],
            closed_sets=closed_sets_of(ownership),
            effective_owners=ownership['owner']),
        finals_bracket=dict(
            members=[dict(squad_key=k,
                          entrant_id=(registry.entry(k) or {}).get('entrant_id'),
                          conference=registry.conference_of(k),
                          segment=registry.segment_of(k),
                          division=(registry.entry(k) or {}).get('division'),
                          licence_class=(registry.entry(k) or {}).get('licence_class'),
                          ladder_weight=registry.ladder_weight(k),
                          is_ranked=bool(registry.entry(k) and registry.entry(k)['ranked']),
                          matches=squads[k]['matches'],
                          standing_points=squads[k]['standing_points'],
                          season_score=squads[k]['season_score'],
                          gross_output=squads[k]['gross'],
                          charged_upkeep=squads[k]['charged'],
                          overflow_upkeep=squads[k]['overflow'],
                          tier=squads[k]['tier'])
                      for k in (br['best']['keys'] if br['best'] else [])],
            satisfied_constraints=br['best']['met'] if br['best'] else 0,
            constraints=[dict(key=k, text=t, satisfied=br['best']['flags'][k])
                         for k, t in BRACKET_CONSTRAINTS] if br['best'] else [],
            total_charged_upkeep=br['best']['upkeep'] if br['best'] else 0,
            total_ladder_weight=sum(registry.ladder_weight(k) for k in
                                    (br['best']['keys'] if br['best'] else [])),
            total_gross_output=sum(squads[k]['gross'] for k in
                                   (br['best']['keys'] if br['best'] else [])),
            candidate_space_size=len(br['space']),
            eligible_squads=br['eligible']),
        review_docket=dict(
            size=policy.docket_size,
            matches_elected=len(docket['chosen']),
            size_relaxed=docket['size_relaxed'],
            conference_cap=policy.docket_cap,
            review_weight=policy.review_weight,
            matches=[dict(match_id=r['match_id'], conference=r['conference'],
                          leading_squad=r['leading_squad'],
                          voided_output=r['voided_output'],
                          escape_directive_records=r['escape_records'],
                          review_index=r['review_index'])
                     for r in docket['chosen']],
            total_review_index=docket['total'],
            conference_counts=dict(Counter(r['conference'] for r in docket['chosen']
                                           if r['conference'])),
            candidate_space_size=docket['space_size']),
        policy=dict(engine_tag=chosen['combo']['engine_tag'],
                    window_depth=chosen['combo']['window_depth'],
                    sentinel=chosen['combo']['sentinel'],
                    season_budget=calib['budget'],
                    promotion_threshold=calib['threshold'],
                    eligibility_match_floor=policy.eligibility_floor,
                    ladder_weight_prominence=policy.prominence,
                    standing_points_floor=policy.standing_floor,
                    combined_ladder_weight_floor=policy.weight_floor,
                    combined_gross_output_floor=policy.gross_floor))

    write_json(os.path.join(out_dir, 'summary.json'), summary)
    write_json(os.path.join(out_dir, 'data_quality.json'), quality)
    write_json(os.path.join(out_dir, 'policy_audit.json'), audit)
    for name, payload in reports:
        write_json(os.path.join(out_dir, name), payload)
    return 0


def closed_sets_of(ownership):
    """The rings of hand overs that close on themselves, each settling on its
    first sorting member."""
    rings, seen = [], set()
    for squad, canonical in sorted(ownership['closed'].items()):
        if canonical in seen:
            continue
        members = sorted(k for k, v in ownership['closed'].items() if v == canonical)
        key = tuple(members)
        if key not in seen and members not in rings:
            rings.append(members)
        seen.add(canonical)
        seen.update(members)
    return rings


def _figures_matched(metrics, policy):
    if not metrics:
        return 0
    n = 0
    for _fig, label, value in policy.control_figures:
        metric = policy.figure_metric(label)
        if metric in metrics and metrics[metric] == value:
            n += 1
    return n


def _fail_order(e):
    fail = e.get('fail')
    if not fail:
        return 10 ** 6
    m = re.match(r'C(\d+)$', str(fail.get('figure')))
    return int(m.group(1)) if m else 10 ** 5


def _rank_entry(e, policy):
    """Order candidate readings: the one reproducing the most published figures
    first, then the one surviving to the latest figure."""
    return (_figures_matched(e.get('stage1'), policy), -_fail_order(e),
            1 if e.get('error') else 0,
            0 if e.get('season_clean', True) else 1)


if __name__ == '__main__':
    sys.exit(main(sys.argv))
