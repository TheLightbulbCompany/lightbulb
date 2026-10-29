Keep exactly one record for each entity and attribute. A new statement about a slot that already has a record updates that record; it never adds a second one. Messages from every human speaker count. The speaker's name is in the event text as `<Name>: ...`; keep it in the record.

A claim is `<entity> <attribute>: <value>`. The entity is the named thing; the attribute is its property. "Acme renewal date: 2026-11-30" is entity Acme, attribute renewal date. "Launch owner: Ana" is entity Launch, attribute owner. Below, `<claim>` always means the full `<entity> <attribute>: <value>`, never the bare value.

When a value changes (the speaker says it changed, moved, is now different, or replaces an earlier value), rewrite the record as:
`<Name>: <claim> (since <date>; was <old claim>, <who>, until <date>)`
`<Name>` is the speaker of the current claim. Dates are YYYY-MM-DD. If the record already has history, keep it after the first "was", separated by semicolons, most recent first. Example with two history entries:
`Ben: Acme renewal date: 2026-12-15 (since 2026-11-02; was Acme renewal date: 2026-11-30, Ana, until 2026-11-02; was Acme renewal date: 2026-10-31, Ana, until 2026-10-20)`

When two people give different values for the same slot and neither says anything is changing, do not choose one. Rewrite the record so that `Contested:` is the very first token on the line, followed by the full claims with their speakers:
`Contested: <Name1>: <claim1> (<who>, <date>) vs <Name2>: <claim2> (<who>, <date>)`
When one of them later confirms or changes their value, resolve it with the "since" form above.

Write the status and the dates into the record text in exactly these forms. They are read back from the text later, so do not paraphrase them. Never rename or drop the AddMemory and UpdateMemory operations; only change the text you write into them.
