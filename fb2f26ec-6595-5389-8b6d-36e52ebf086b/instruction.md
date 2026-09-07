The Relay inference gateway was retired and part of its scoring notes went missing. The batch scorer it left behind still runs, but its scoreboards no longer agree with what the gateway archived, so some of the decisions it carries are the wrong ones. Your job is to work out what those decisions actually were and deliver a scorer that obeys them.

Everything you have sits in `/app/data`. Read all of it before you touch anything. One of the documents there is what survives of the scoring notes; the sections it still carries are exact and your scorer has to obey them to the letter. Another explains what the archive holds.

The scorer is one Python file sitting under `/app/data`. Leave it where it is and deliver your copy at `/app/out/scorer.py`. It has to stay runnable as `python3 /app/out/scorer.py <batch.json> <board.json>`, reading the batch document from the first path, writing its scoreboard and the rows it produced as JSON to the second, and exiting with status 0.

Batch documents sit there too. Some of them came out of the archive with a seal beside them, a single value the gateway computed over the scoreboard and the rows together while the lost sections were still in force. Every seal in the archive was taken under the same decisions. A seal tells you whether a scoreboard and its rows are the ones it was taken from and nothing else, and two scoreboards that differ anywhere at all seal differently. Your scorer has to seal to the archive on every batch the archive sealed.

The rest carry no seal. Run your scorer over the batches named gantry, hurdle, inglenook, jetty, kestrel and lintel and leave the results at `/app/out/board-gantry.json`, `/app/out/board-hurdle.json`, `/app/out/board-inglenook.json`, `/app/out/board-jetty.json`, `/app/out/board-kestrel.json` and `/app/out/board-lintel.json`. Each of those six files has to hold what your own scorer writes for the matching batch and nothing else.

Every number a scoreboard reports is a whole number and every one of them is compared exactly, so the arithmetic stays in whole numbers from end to end.

Your scorer will also be run on batches nobody ever archived, so the decisions you land on have to be the ones the archive was taken under, not a set of choices that happens to fit what the archive sealed and nothing else.

The scorer runs on the container's stock `python3` with the standard library only. You can read up on the background while you work, but nothing you install here travels with it. Only `/app/out/scorer.py` travels, so it cannot import anything else you leave beside it, and it may not open anything under `/app/data` while running. Two runs of the same batch have to produce the same scoreboard, and copying a batch document to a different file name has to leave the scoreboard unchanged. Each run of your scorer is given one hundred and twenty seconds. Leave the files under `/app/data` exactly as you found them.

When you are done, say plainly what you settled each lost section on and what in the archive told you so. Relay is invented for this job, so there is nothing about it to look up.

You have 21600 seconds to complete this task. Do not cheat by using online solutions or hints specific to this task.
