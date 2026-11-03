# Domain Docs

## Before exploring

Read root `GLOSSARY.md` and ADRs in `docs/adr/` relevant to the work.

If these do not exist, proceed silently without suggesting their creation
upfront. The domain-modeling skill creates them lazily when terms or
decisions are resolved.

## Layout

This repository uses a single context:
- `GLOSSARY.md`: shared domain vocabulary.
- `docs/adr/`: architectural decision records.

## Vocabulary

Use glossary terms in issue titles, proposals, hypotheses, and tests.
If a concept is missing, reconsider whether it belongs to the domain;
note genuine gaps for domain-modeling.

## ADR conflicts

Explicitly flag proposals that contradict existing ADRs, naming the ADR
and explaining why it should be revisited.
