# Addlesee et al. 2023 — Data Collection for Multi-party Task-based Dialogue in Social Robotics (IWSDS 2023)

Read in full on 2026-10-02 from the author's PDF: https://drive.google.com/file/d/1gUsMOlYHDzreB17CYNFxDOu-dWCtBQ4M/view (10 pages; references 33–38 on p.10 not captured). Authors: Addlesee, Sieińska, Gunson, Hernández García, Dondrup, Lemon (Heriot-Watt, EU H2020 SPRING, grant 871245).

## Claims (verbatim-faithful notes)
- Robots lack multi-party abilities: differentiate speakers, identify addressees, understand complex social situations, adapt behaviour. No corpus exists to evaluate whether systems can track individual or SHARED goals of multiple users.
- Multi-party data must include humans speaking to EACH OTHER, not only utterances directed at the robot.
- Pictograms instead of scripted role-play → more natural, spontaneous dialogue (Novikova et al. 2016).
- Phenomena: people talk at the same time, deliberately interrupt, finish each other's sentences (split utterances); different and conflicting goals and information must be tracked.
- All existing multi-party corpora are human-human (Mahajan & Shaikh 2021); behaviour differs talking to a robot vs a human.
- Three standard MPC tasks: (1) speaker recognition, (2) addressee recognition (individual, whole group, or a sub-group), (3) response selection/generation — "who says what to whom" (Gu et al. IJCAI-22 survey).
- Robots that identify conversational roles in a group (including their own) and adapt get higher sociability/social-presence/attentiveness ratings and can influence participant involvement.
- Turns are NOT sequential in MPCs: a group question can spark side-discussions before anyone answers it; graph-based models outperform for response generation (GSN, HeterMPC); MPC-specific pretraining (MPC-BERT). Sub-conversations on different topics need tracking (Martinez & Kennedy 2020). Long-dialogue model DialogLM.
- DST (dialogue state tracking) is crucial but has never been applied to MPCs (DSTC2/3, MultiWOZ-style datasets are dyadic).
- People may form shared goals, have different goals, or disagree due to contrasting information/beliefs.

## Setup
Hospital memory-clinic reception; ARI robot as receptionist (FAQs: schedules, catering, directions). 3-way: patient + companion + robot. Robot verbal behaviour is Wizard-of-Oz: operator picks from 31 canned responses (task answers, greetings, yes/no, please/thank you, "I don't know") whenever the robot is addressed. 40 recordings of older adults collected; annotating speaker, addressee, task goals, utterance-level dialogue acts; multi-party goal-state-tracking task to be released.

## Six conditions (A–F, 4 task sets each, order randomised)
- **A Helpful Companion** — patient has a goal; companion only supports. Either may ask the robot.
- **B Shared Goals** — both have the same goal; elicits more split utterances. Table 1: P "We are a bit lost umm…" {request(help)} / C "And we are trying to find the cafeteria." {request(location(cafe))} / P "Yeah, that's right." {affirm} / R "The cafe is just up the stairs behind you." {inform}. First two turns = split utterance → shared goal.
- **C Reluctant Patient** — patient told NOT to talk to the robot; expresses goals to the companion. Robot must listen to humans giving goals to each other and decide when it is being engaged.
- **D Different Goals** — separate goals; robot must track progress on multiple goals. Goals can be completed by OTHER participants. Table 2: P "What time is my appointment?" Goal(P,get-info(appt-time)) / C "Can you tell me where the toilet is first?" Goal(C,locate(toilet)) / P "It's beside the lift we came in." Answer_Goal(C,locate(toilet)) / R "Your appointment is at 1pm." Answer_Goal(P,…).
- **E Missing Info** — robot can't know something (privacy); companion has it. Table 3: P "Where is my appointment?" / R "Sorry, I don't have access to that information." {cant-help} / C "It's with Dr Smith." {inform(doctor)} / R "Dr Smith is in room 17."
- **F Disagreement** — companion holds info contradicting the robot (misremembered); robot should identify the conflict, re-supply correct info, reassure.

## Annotation
Speaker, addressee, task goals with Goal(who, intent) / Answer_Goal(...) and dialogue acts (request, inform, affirm, cant-help).

## Key refs to follow
Bohus & Horvitz 2011 (multiparty turn-taking, SIGDIAL); Skantze 2021 (turn-taking review, CSL); Gu, Tao, Ling 2022 (Who says what to whom survey, IJCAI); Le et al. 2019 (addressee identification, EMNLP); MPC-BERT (ACL 2021); HeterMPC (ACL 2022); GSN (IJCAI 2019); Martinez & Kennedy 2020 (concurrent conversation tracking + memory, CUI); Mahajan & Shaikh 2021 (multi-party corpora survey, SIGDIAL); Johansson, Skantze, Gustafson 2014 (human-human vs human-robot turn-taking); Eshghi & Healey 2016 (grounding by proxy); Oertel et al. 2021 (engagement-aware attentive listener).
