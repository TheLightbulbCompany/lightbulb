# Read in full on 2026-10-02 (owner solved the captchas in Chrome)

## Skantze 2021 — Turn-taking in Conversational Systems and HRI: A Review (CSL 67, 101178; CC BY)
Source: https://www.sciencedirect.com/science/article/pii/S088523082030111X — all sections 1–8 read.
- Sacks et al. 1974 rules at each transition-relevant place (TRP): (1) current speaker may select next (gaze, address term); (2) else anyone may self-select, first to start wins; (3) else current speaker may continue. Multi-party makes transitions more optional → harder to predict.
- Human gaps ~200 ms; systems use 700–1000 ms silence thresholds. Pauses within turns are on average LONGER than gaps between turns → silence is a bad end-of-turn signal. Prediction (not reaction) is needed; verbal/pragmatic completion matters most (de Ruiter 2006: removing words hurt prediction; flattening intonation didn't).
- Ford & Thompson: ~half of syntactic completions are pragmatic completions; ~half of TRPs see actual turn shifts. TurnGPT (transformer LM with dialogue context) beats final-POS/LSTM for TRP prediction.
- Wake words: clear turn-start cue + addressee identification + easy barge-in, but make interaction less conversational (Woodruff & Aoki 2003).
- Barge-in: false barge-ins from noise/coughs/backchannels; predict turn-start vs backchannel 100–500 ms after onset; resume after a false barge-in (filled pause, restart from phrase boundary). Context risk: a "Yeah" meant for the previous utterance gets read as an answer to a just-started question → system must monitor its own speech when interpreting user input. Systems may refuse barge-in for important content (signal by raising volume).
- Generating cues: during processing delay users think the system won't speak and continue → use turn-initial/holding cues (filled pause, in-breath, gaze aversion); gaze aversion + filled pause halved the chance the user starts speaking (Skantze et al. 2015). Shallow processing to decide to respond, start a turn-initial cue, then produce the real answer.
- Multi-party (§2.4, §7): addressee selection ≠ next-speaker selection (Auer 2018). Roles: speaker, addressee, side participant, bystander, eavesdropper. Addressee detection: gaze/head pose strongest; machine-addressed speech is louder/more clearly articulated (Shriberg 2012). Johansson & Skantze 2015: label turn-taking appropriateness on a scale "not appropriate → possible if needed → appropriate → obliged" (obliged = looked at + direct question); combining head pose + prosody + verbal features best. 3D vs 2D face: correct-addressee turn-taking 84% vs 53%. Robots addressing the less dominant speaker reduce participation imbalance; open floor (self-select) increases imbalance.
- §8.3 Utility: people take the turn because they have something to say — high utility ("there is a fire") may speak regardless of TRP; low utility less prone; but a strong TRP ("What do you think?") obliges a turn. Decision-theoretic models: Raux & Eskenazi 2009, Bohus & Horvitz 2011.
- §8.2 Natural timing: too-fast responses read as insincere; open/wh-questions get longer human response times (300–450 ms) than yes/no (100–180 ms).
- Cues are additive and redundant (Duncan); combining modalities improves robustness.

## Eshghi & Healey 2016 — Collective Contexts in Conversation: Grounding by Proxy (Cognitive Science 40(2))
Source: https://onlinelibrary.wiley.com/doi/full/10.1111/cogs.12225 (Free Access) — abstract through conclusions + appendix read.
- Corpus (AMI, 10 meetings, avg 54 min, 425 turns): 165 sequences where one participant is peripheral (no feedback). On re-entry, peripheral participants used context-dependent expressions (ellipsis/anaphora) MORE than baseline (75% vs 51%); last speakers addressing them did so 65% → peripheral participants have the same access to shared context despite giving no feedback.
- Experiment (84 participants, 26 triads, 3-way maze task over text chat via DiET; Duo shares goals/info, Solo differs): Duo forms a coalition (Duo-scope "we" 5.7 vs triad 1.7 per participant; 87% of plural "you" from Solo addresses the Duo); Duo members each type less than the Solo (41 vs 52 words/maze) → one member's contribution stands for both.
- Downgrading one Duo member's acknowledgements ("okay" → "erm", 592 turns, 9.5%) made the OTHER Duo member produce fewer positive grounding cues (0.11 vs 0.15) and address each other by name more (307 vs 217) — the coalition repairs collectively.
- Conclusion: by default coalition members' responses stand proxy for one another; an acknowledgement or answer from one member grounds for the coalition; when that fails they compensate by conferring. Coalitions are transient and topic-bound (Lerner markers: "you guys", "we", conferring, "they").

### For Lightbulb
- One human's acknowledgement/answer counts for their group (B3/D-type tests): agents should not seek confirmation from every member.
- "we"/"you guys" mark coalitions → treat a "we need…" request as shared; reply once, to the group.
- Peripheral members are still "in the context": an agent can address a silent member assuming they followed.
- Skantze: Jev's decision should be graded (not appropriate / possible / appropriate / obliged), not binary; a direct question to an agent = obliged; utility can justify speaking without a turn offer (urgent correction).
- Voice: during the agent's thinking time, emit a turn-holding cue (our "joining"/filler) so people don't keep talking over the slot; don't let a "yeah" meant for the previous line be read as an answer to the agent's new question.
