Stores state for a recurring automation to read on its **next** fire. Pass the
state to keep (an object or string); this **replaces** the prior memory for that
automation, so include everything the next run still needs. It only works
*during* an automation run — in normal chat it isn't available. Use it for
counters, "last seen" markers, or anything the next run would otherwise have to
recompute.
