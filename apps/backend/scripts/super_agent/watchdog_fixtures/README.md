# Watchdog E6 fixtures (spec 11 §8.4, wave2-W Task 8)

20 task prompts that should make a Worker loop, drift, claim completion without evidence, or hit an external
block. Each file is one prompt; the HTML comment on line 1 names the expected judge evidence. Start each as a task
in a dev workspace with the switch on, let it run ≥ 20 min, then label every watchdog proposal with
`uv run python -m scripts.watchdog_ctl --label <findingId> tp|fp|unsure --by <platform admin>` and read
`--report`. These are positives; the healthy tasks a normal dev day produces are the negatives.
