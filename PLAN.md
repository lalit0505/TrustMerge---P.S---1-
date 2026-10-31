# TrustMerge build plan

Each task is one pull request.

4. **Git helper layer**: run_git wrapper, branch listing, is-ancestor, patch-id. Done when: tests pass on the fixture repo.
5. **Identity matching**: patch-id, cherry-pick trailers, Django/CPython backport messages, fuzzy overlap. Done when: the edited backport is matched to its original hotfix.
6. **Reachability and evidence levels**: PROD, UAT, CI-PASSED, UNVERIFIED, UNKNOWN (scan command). Done when: scan shows the right level for every fixture commit.
7. **Notes store**: init, mark, sync under refs/notes/trustmerge, with inference fallback when notes are missing. Done when: a fresh clone without notes still gets an answer.
8. **Conflict parsing**: origin of each side via git ls-files -u and git blame. Done when: the fixture conflict shows both origin commits.
9. **analyze report**: rich output, no AI. Done when: output shows origin, branches reached and evidence for each side.
10. **Gemma client + fake client**: fuzzy same-change judge. Done when: tests pass with the fake, and a manual run works with real Gemma.
11. **Gemma conflict briefing**: explanation based only on collected evidence. Done when: the briefing never invents test claims.
12. **finish command**: normal commit with trailers and a note. Done when: plain git log shows an ordinary commit.
13. **Realistic histories**: tests on Django/CPython-style backport messages. Done when: messy real-world messages are handled.
14. **examples/ and README**: real output from running Gemma locally. Done when: someone can run it from the README alone.
