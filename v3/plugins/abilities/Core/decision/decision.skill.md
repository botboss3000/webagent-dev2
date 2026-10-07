# Decision

Use decide_with_jev for any bounded classification, comparison, routing, or
relevance judgment from evidence already in hand. The agent defines the
purpose, options, rubric, and number of questions at call time; the tool has
no file-specific or workflow-specific decision rules. Pass one JSON string
containing state and named questions, following TypeSafe's System One schema.
The model is selected from this agent's assigned Decision role; an optional
model field must match it.

Question types are choice (named criteria options), score (ordered rubric
levels starting at zero), and noul (probability of a yes answer). For file
triage, list candidate paths and brief descriptions in state, then use a
choice question to choose what to inspect first or one score question per
candidate. Jev cannot read files or execute tools. Check selected files with
ordinary source tools before reporting where code lives.

Use score_directory_with_jev when the agent knows a directory and search
criterion but has not enumerated its entries. Rust requires the current
session's automatic file_list permission, then the isolated worker lists at
most 32 names per page. Jev sees those names and their kinds, not file
contents. The result includes every page entry and its 0–4 score, confidence,
and a next_offset for larger directories. Read promising files to verify.

The result returns typed answers, model, and usage. Confidence and
probabilities describe uncertainty, not verified facts. If the Decision
role or TypeSafe key is unavailable, report it rather than inventing a
classification. The call sends supplied state to TypeSafe; include only
necessary data.
