# SQL Databases — live, approved business knowledge

Use this ability for facts that live in a connected business database and may
change independently of the conversation. Use Memory for durable conversational
facts and the Native Wiki for curated shared articles.

## Safe query workflow

1. Call `sql_list_connections` to see the configured sources and their approved
   tables.
2. Call `sql_describe` before composing a query unless the required columns are
   already known from the current turn.
3. Use `sql_search` for natural-language lookup over an administrator-configured
   knowledge dataset.
4. Use `sql_query` for structured questions. Only a single read-only SELECT/CTE
   statement is accepted, only approved schema-qualified tables may be
   referenced, and a server row cap is always applied.
5. In the answer, identify the connection and table/view used. Include a stable
   row identity when the result provides one.

Never claim that a database result came from Memory or the Wiki. Never infer
write access: this ability is deliberately read-only. If a needed table is not
approved, explain which table or view an administrator needs to expose.
