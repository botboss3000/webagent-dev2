Fuzzy-matching text replacement — it tolerates whitespace and minor differences,
which makes it the better choice when `edit_source` won't match.

- If a patch fails **twice**, stop retrying the same shape: reread the exact region
  and use a smaller patch or a precise replacement.
