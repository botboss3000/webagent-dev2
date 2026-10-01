Overwrites a file with new content, backing up the previous version automatically.

- Reserve it for whole-file work: new files, or a deliberate full rewrite.
- For a small change prefer `edit_source`/`patch_source`, so unrelated user changes
  elsewhere in the same file survive untouched.
- Never overwrite merely because a backup exists — verify the target and the need first.
