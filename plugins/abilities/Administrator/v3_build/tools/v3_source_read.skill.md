# v3_source_read

Read bounded lines from an open isolated task's source snapshot to review or rebase a patch. This reads the broker's current task workspace, including changes that may be absent from the older V3 Workspace adapter seed. Use the returned workspace digest for a checked apply. No build or deployment is started.
