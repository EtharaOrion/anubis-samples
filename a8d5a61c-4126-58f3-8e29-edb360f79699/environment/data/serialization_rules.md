# Serialization rules

Every derived file is UTF-8 with a trailing newline and no carriage returns.

## JSONL files

Each line is one JSON object with keys sorted lexicographically and the compact
separators comma and colon with no spaces. There is exactly one object per line and
a trailing newline after the last line. An empty document is the empty string.
Booleans are written as true and false. Every numeric field is an integer. A field
that holds a list of example ids is a JSON array of strings.

## JSON files

Each JSON document is serialized with keys sorted lexicographically, two space
indentation, and a single trailing newline after the closing brace. Numeric fields
are integers. The objective is a JSON array of five integers.

## Digests and the chain

A digest is the lowercase hex sha256 of the exact bytes of the named file as it
sits on disk. The manifest names every derived file and the program, mapping each
name to its digest, and is built after every other hashed file is final. The folded
chain in `final_state.json` is computed over the manifest digests taken in ascending
manifest name order, each step being the sha256 of the concatenation of the previous
chain string and the current digest string, starting from a string of sixty four
zero characters.

## The program

The delivered program goes at the declared path and reads the data directory and the
output directory from its two command line arguments. It must not pin the shipped
data path as a literal in its code, must produce identical output on identical
input, and must write only inside the output directory it is handed.
