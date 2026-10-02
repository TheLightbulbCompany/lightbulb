# Martinez & Kennedy 2020 — A Multiparty Chat-Based Dialogue System with Concurrent Conversation Tracking and Memory (CUI '20, Disney Research)
Read in full 2026-10-02 from the ACM free-access PDF (doi 10.1145/3405755.3406121).

- Problem: multiparty systems either treat a group as independent simultaneous dyadic chats (loses cross-talk) or merge everyone into one group intent (can't engage individuals or remember them).
- **Conversation manager**: every open sub-conversation is a quadruple (i = intent that opened it, t = topic, u = user the agent should direct its reply to, e = expected replies). When a sub-conversation starts, one quadruple is created PER group member (identical except u), so the agent listens for replies from anyone.
- **Reply-from flags on every topic graph root**: *Reply from Any* (one person's reply completes it → dequeue all members' quadruples for that topic), *Reply from All* (revisit each member until all answered, e.g. greetings), *Reply from None* (end).
- **Conversation queue (FIFO)**: an incoming utterance is matched against ALL open quadruples' expected replies → out-of-turn replies are attributed to the right sub-conversation with no heuristics; if none match, enqueue a new quadruple for the most similar topic. When the agent takes the floor it peeks the queue head to pick topic + addressee.
- **Interruption stack (LIFO)**: events needing one specific user (someone walks in → greet them) are pushed; the agent serves them before resuming the pre-interruption context from the queue. Above ~7 simultaneous users, accumulated interruptions made context switches too fast for humans to follow.
- **Memory intents**: per-user records (intent, user_id); at branch points the manager checks memory and follows a memory edge (e.g. recall "you said you were going to Disneyland") instead of re-asking. Slot-fill retrieval by relevance + recency. Utterance selector maximises recalled detail but never repeats an utterance within an interaction.
- **Addressee clarity**: agent names the addressee whenever it switches ("{UserName}, do you like to dance?"); randomly rotating the addressee kept it from collapsing into a dyad. Proposed global intent: answer "Who are you talking to?".
- **Failure observed (Table 2)**: two people answered at once — the system took the first and silently dropped the overlapping second, then addressed the "other, less talkative human" → participants lost track of who was being addressed. Overlap handling is listed as future work.
- Catch-all "Other" intent avoids hanging on ASR garbage (graceful segue).
- Case study only (12 participants, 6 pairs × 3 interactions); rule-based Rasa NLU + dialogue graphs; no quantitative evaluation.

## For Lightbulb
- Directly maps to our gap: keep an explicit ledger of open requests per room, each with requester, addressee/agent, and a reply policy (any / all / none). A human answer to an "any" request closes it → the agent must not repeat (Addlesee D, grounding by proxy).
- Match every incoming line against open requests to attribute out-of-turn replies (E: missing info supplied by someone else → continue the original request).
- Name the addressee when an agent switches who it's talking to, especially in voice.
- Overlapping speech in Live: don't silently drop the second speaker; route/queue both.
- Per-person memory keyed by user id (our AgentCore memory is one owner-wide namespace) — supports the deferred "per-person recall" item.
