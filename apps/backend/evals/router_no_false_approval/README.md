# router_no_false_approval

Spec 05 §3.5 layer 3. 40 Router replies, each on a task with an open Question: 20 claim an approval that did not
happen (`expect: blocked`, posted as the card line instead) and 20 true negatives that mention approval without
claiming it (`expect: posted`, as written). Bar: 40/40.

`run.py` (codex exec through the Codex subscription, `--dev`) lands with wave2-Q Task 9 part 2, once the Router
posts replies (Lane O's apply path). Until then `tests/unit/super_agent/questions/test_eval_dataset.py` runs the
same 40 cases through `questions.guard_reply` directly.
