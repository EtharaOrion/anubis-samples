# Installation dossier derivation rules

This is the authoritative rule set for turning the shipped loan, object, gallery,
crate, conservation, dependency and policy records into the corrected installation
dossier. Read it together with `schema.md` (record shapes and serialization),
`constants.md` (scale factors and quanta), `reason_codes.md` (the code enums) and
`worked_example.md` (one tiny worked case). No answer for the shipped snapshot lives
in any file; you derive everything here.

Everything is integer or exact rational. No floating point value is ever produced,
compared or written. Lengths are carried in micrometres, mass in grams, money in
cents, time in integer day indices and whole seconds. A ratio is a reduced pair of
integers. After any stage that forms a rational quantity you round it to that
stage's quantum with round half up (a value exactly halfway between two quanta goes
to the larger), and the rounded integer is what the next stage consumes. Recomputing
a later stage from the unrounded value is wrong.

## The spine

The spine is the per object installation state. Each object starts as a loan record
and is carried through canonical identity, normalized geometry, orientation
admissibility, bay placement, conservation adjustment, crate assignment, transport,
dependency resolution, install order, crew and window reconciliation, and finally
the emitted dossier. Nearly every artifact is a projection of that evolving state, so
one wrong decision early changes many later figures.

## Stage 1 canonicalize identity and units

Object identity is read from the master record. The master carries a stale duplicate
row for one object id with a conflicting lender; keep the first occurrence of each id
and discard any later row, so the first lender and material win. The object version is
the highest version recorded for that id in the version map, which carries two rows for
the duplicated object. Convert every declared length to micrometres and
every declared mass to grams using the scale factors in `constants.md`, keyed by the
per object unit codes. Declared value is converted to cents by multiplying by one
hundred. The canonical object record carries identity, lender, material, resolved
version, value in cents and mass in grams.

## Stage 2 normalize geometry

From the micrometre width, height and depth derive the clearance envelope. The
clearance width adds the handling class clearance margin on both sides, the clearance
depth adds its margin on both sides, and the clearance height adds its margin once on
top. All margins are declared in millimetres in the handling class rules and are
converted to micrometres before they are added. The aspect ratio is height over width
as a reduced pair; when width is zero the ratio is the sentinel zero over one.

## Stage 3 orientation admissibility

The admissible orientations of an object begin with the base orientation. A rotation
about the vertical axis is admissible only when rotation is allowed for that object. A
tip that lays the object on a horizontal axis is admissible only when tipping is
allowed. A loan condition that forbids rotation or tipping removes those orientations,
so an object with neither allowed keeps only the base orientation.

Orientations are numbered by a fixed convention. Index 0 is the base orientation, with
footprint the clearance width by the clearance depth and vertical extent the clearance
height. Index 1 is the rotation about the vertical, with footprint the clearance depth
by the clearance width and the same vertical extent. Indices 2 and 3 are the two tipped
orientations that bring the clearance depth vertical: index 2 has footprint the clearance
width by the clearance height, and index 3 has footprint the clearance height by the
clearance width. The admissible orientation list is index 0 always, then index 1 when
rotation is allowed, then indices 2 and 3 when tipping is allowed, in that order. A bay
candidate or a placement records the smallest admissible orientation index that fits. An
object with no placement records orientation minus one and an empty string for its bay.

## Stage 4 bay candidates

For each object and bay, the object fits in an orientation when its oriented footprint
and vertical extent sit inside the bay interior. The width axis is inclusive, so the
oriented footprint width must be less than or equal to the bay interior width. The
depth axis is exclusive, so the oriented footprint depth must be strictly less than the
bay interior depth. The vertical axis is inclusive, so the oriented vertical extent
must be less than or equal to the bay interior height. Bay interior dimensions are
declared in millimetres and converted to micrometres before the comparison. The bay
candidate matrix records, per object and bay, whether any admissible orientation fits
and the smallest admissible orientation index that fits.

## Stage 5 selected placement

The primary placement of an object is chosen by scanning bays in ascending bay
priority then ascending bay id and taking the first bay in which some admissible
orientation fits, with the smallest fitting orientation index. An object with no
fitting bay is rejected; its reason is no orientation when it had only the base
orientation available and no fit otherwise. This stage uses geometry only.

## Stage 6 conservation adjustment

Re run the placement scan of stage 5 but now also honour conservation. A bay is
excluded for an object when the bay lux level exceeds the object material maximum lux,
when the object material conservation class is blocked in that bay by a venue
exception, or when the object material needs dark and the bay lux level is above zero.
The conservation adjusted placement is the first bay in priority then id order that
both fits geometrically and passes conservation. An object that fits somewhere
geometrically but nowhere after conservation is blocked. A placement that was valid at
stage 5 can therefore move to a different bay or disappear at stage 6.

## Stage 7 crate candidates

The handling envelope of an object adds the handling class handle margin on all three
axes to the micrometre dimensions. A crate fits when the three sorted handling
dimensions are each less than or equal to the three sorted crate interior dimensions,
which allows the crate to be packed in its best orientation. Crate interior dimensions
are declared in millimetres and converted to micrometres. The residual of a fitting
crate is its interior volume minus the handling volume. Considered alone, the primary
crate for an object is the fitting crate with the smallest residual, ties broken by
lowest crate class id.

## Stage 8 shared crate capacity

Crates are a stateful shared resource. The available count of a crate class is its
catalogue count minus the quantity already consumed by prior state. Objects claim
crates in installation order, so a later object sees only the crates earlier installs
left. For each object in that order, among its fitting crate classes with remaining
availability, the chosen class is decided by this priority: a class the object lender
already holds a reservation for outranks all others, then lower residual, then lower
crate class id. Consuming a class decrements its remaining count and, when a matching
lender reservation exists, one reservation. When the chosen class is the object's
primary the assignment is primary, otherwise the object was displaced from its locally
optimal crate and the displacement is reported. An object with no fitting class left is
recorded with no crate and becomes unschedulable. The crate assignment and contention
report rows are emitted in object id order even though the consumption runs in install
order.

## Stage 9 transport legs

Each crated object ships in the transport mode of its assigned crate class. When the
object lender forbids that mode, the mode falls back to the declared fallback and the
leg is marked as a routing exception.

## Stage 10 dependency edges

Build a directed dependency graph over all objects. Every edge carries a weight used only
to break ties when a cycle is resolved. An explicit dependency rule adds an edge whose
weight is its declared edge priority. Within a bay, for every ordered pair of conservation
placed objects the object with the larger handling volume is installed before the smaller
(ties by lower object id), adding a bay size edge whose weight is the handling volume in
cubic micrometres of that earlier object. Within a bay, an object of a more sensitive
conservation class is installed before one of a less sensitive class, adding a conservation
edge whose weight is the handling volume in cubic micrometres of that earlier object. When
two edges share the same from and to, keep only the one of highest precedence kind, where
an explicit rule outranks a conservation edge which outranks a bay size edge.

## Stage 11 cycle resolution

While the graph has a cycle, remove one edge from that cycle and record it, then repeat
until the graph is acyclic. The edge removed is the lowest priority edge of the cycle under
a three key ordering: first the kind, where a bay size edge is lower priority than a
conservation edge which is lower priority than an explicit rule, then the numeric edge
weight, then from id, then to id. A conservation dependency therefore survives a conflict
with an ordinary presentation preference.

## Stage 12 install order

Order the objects by repeated selection from the acyclic graph. At each step the objects
with no unmet predecessor are the ready set; install the ready object with the smallest
rounded setup seconds, ties broken by object id. Setup seconds are the crew setup value of
stage 13.

## Stage 13 crew, setup and windows

The crew class of an object is its handling class crew class. Setup seconds are the round
half up value of the handling class setup base times the object mass in grams over the
setup quantum. The install window is the intersection of the object material conservation
window, the venue access window of its bay and the crew availability window. The window low
is the largest of the three starts. The window high is the smallest of the three window
ends where the venue access end is treated as exclusive by subtracting one and the
conservation and crew ends are inclusive. The number of days available is high minus low
plus one. The number of days required is the setup seconds divided by the seconds available
per day, rounded up. An object is scheduled only when the window is non empty and the days
required do not exceed the days available; otherwise it is unschedulable, with an empty
window recorded when the window itself is empty and a short window recorded when the window
exists but is too few days for the setup.

## Stage 14 dossier

Gallery occupancy lists, per bay, the conservation placed objects in object id order and
their count. The unschedulable penalty sums a per value class penalty over the unschedulable
objects; the contention penalty sums a per value class penalty over the displaced objects;
the value class of an object is the highest class whose minimum value it meets. Insurance in
cents for an object is the round half up value of its value in cents times the lender
insurance rate numerator over the rate denominator. The objective tuple is placed object
count, unschedulable penalty, contention penalty, cycles broken and total residual, in that
order, maximising the first and minimising the rest. The placed object count, which is also
the placed_objects field of the final state, is the number of scheduled objects, those with
an S_OK install window and equivalently the objects the unschedulable report does not list;
it is not the conservation placed count that the gallery occupancy uses. The integrity manifest maps each hashed
artifact name to the sha256 hex digest of its bytes; the hashed set is every jsonl artifact
plus the penalty summary, the objective tuple and the program. The final state folds the
manifest into a chain by starting from a seed of sixty four zeros and, for each manifest name
in sorted order, replacing the chain with the sha256 hex digest of the current chain
concatenated with that name's digest. The final state also reports whether the repair
converged, how many cycle edges were removed, the final cycle count, the displacement count,
the unschedulable count, the placed object count, the total setup seconds, the total
insurance cents and the objective.

## What is deliberately not spelled out

Some row orders, container shapes and a small number of secondary tie breaks are left for you
to determine from the data and from consistency with the rest of the dossier. Where a choice
is not stated here, pick the one that keeps the artifacts internally consistent and
reproducible, and confirm it by re running your own program on a second snapshot.
