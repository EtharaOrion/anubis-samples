# What survives of the Relay scoring notes

## 1. What this document is

This is what survives of the scoring notes the Relay inference gateway ran its
batch scorer under. The sections that survive are exact and complete, and a
scorer must obey them to the letter. Other sections were lost, and the pages that
replace them say only which decision that section governed. Those settlements are
recorded nowhere in this document, and nobody wrote down how many of them there
were.

They are recoverable. The archive holds batches that were sealed while the lost
sections were still in force, and every seal in it was taken with the same
settlements in place.

The scorer shipped beside these notes still runs. Its scoreboards do not seal to
what the archive holds, so some of the settlements it carries are the wrong ones.

## 2. The batch

A batch names two relations. Every row of either carries `item_id` or `winner` as
noted below, a whole number, and further columns of whole numbers or short
strings. A cell may be absent, which the notes call a blank.

The `items` relation carries one row per scored item. Each row has an `item_id`
counting from one, a `logits` list, and a `gate`. The `logits` list holds one
entry per candidate class, each entry a `class` id and a whole-number `logit`.

The `routes` relation carries one row per class the gateway routes. Each row has a
`winner` class id and the `bucket` that class is routed into.

## 3. The winning class of an item

Each item is scored for the class that holds its largest logit. The winning
logit is that largest value. The runner-up logit is the largest logit among the
remaining classes, or blank when the item lists only one class. The item's
`margin` is the winning logit less the runner-up logit, or the winning logit
itself when there is no runner-up.

The lost text settled which class wins when two classes tie for the largest logit.

## 4. The abstain floor

Each batch carries a whole-number `floor`. An item whose winning logit is at or
above the floor survives; an item below it is abstained and scored nowhere.

The lost text settled whether the floor is applied before the item is routed to
its bucket or after.

## 5. Which relation drives the routing

The scorer drives the routing from one of the two relations and probes the other.
The relation it drives from is reported as `drive` and the other as `probe`. Each
surviving item is matched to the route whose `winner` equals the item's winning
class, and the matched row is given a join identifier: the item's `item_id` times
one thousand, plus its winning class id.

The lost text settled which of the two relations the scorer drives from.

## 6. Buckets

Matched items are gathered by the `bucket` their winning class routes into. Each
bucket reports `key`, `n` for the items it holds, `total` for the sum of the item
margins it holds, `mean`, `top_row` for the largest join identifier it holds or
blank when it holds none, and `confidence`.

The lost text settled whether a bucket that ends with no surviving item still
reports.

## 7. The reported mean

The mean is the total of the item margins in the bucket, divided by how many items
the bucket holds, as a whole number. A bucket with no item reports a blank mean.

The lost text settled how that division becomes a whole number.

## 8. The reported confidence

Each bucket reports a `confidence`, a single whole number taken over the items it
holds.

The lost text settled whether the confidence is the sum of the winning logits of
the items it holds or the sum of their margins.

## 9. The order buckets come back in

Buckets come back ordered by their mean.

The lost text settled where a bucket with a blank mean sorts, and it settled how
two buckets holding the same mean order between themselves.

## 10. The scoreboard document

A scoreboard reports `drive`, `probe`, `abstain`, `empty_bucket`, `null_order`,
`tie_order`, `confidence` and `buckets` for how many buckets came back. The result
is the ordered list of buckets. Both travel together, and a seal is taken over the
two as one.
