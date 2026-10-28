Write every fact in the shapes below. These rules add to the instructions above.

Speaker. Start each line with the speaker's name and a colon: `<Name>: ...`. Use the name shown in the conversation, never "the user" or "someone". Keep who said it, because later records depend on it.

Claims. State a fact about a thing as `<entity> <attribute>: <value>`, for example `Acme renewal date: 2026-11-30`. Use the same entity and attribute wording each time the same slot comes up, so a later statement lands on the same record.

Dates. Resolve every relative date ("tomorrow", "next Friday", "end of the month") to a calendar day in the form YYYY-MM-DD, using the date the message was sent. Never store a relative date.

Rules. Write a standing rule as `When <condition>, <action>. Applies to <scope>.` Name the scope: a person, a room, a customer, or "everyone".

One fact per line. If a message holds three facts, write three lines.

Requests. When someone asks for something, also write the fact the request implies. "Please move the launch to the 14th" implies `Launch date: <resolved date>`; write that line as well as the request.

Leave out greetings, thanks, and chatter that carries no fact.
