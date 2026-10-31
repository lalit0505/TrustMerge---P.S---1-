# UnpaidInterns

## Team
- Mittal Lalit (@lalit0505)
- Lakshay Goyal (@lakshay122007)
- Akshat Shukla (@Akshatshukla-25)

## Problem statement
Problem 1: Intelligent Git Conflict Resolution with Testing Provenance

## What we're building
`TrustMerge`, a Git extension that shows where each side of a merge conflict came from, whether there is evidence it was tested, and finishes with a normal Git commit.

## AI models used
| Model | How we run it | What it does | Why |
|---|---|---|---|
| gemma4:e4b | Ollama, running locally | In the product: judges whether two patches are the same change, extracts references from commit messages, and explains conflicts from collected evidence | Open weights, runs on a laptop, reliable structured JSON output |
| qwen2.5-coder:7b | Ollama, running locally | Development only: generates implementation code through Aider | Strong small open-weight coder that fits laptop memory |

## AI coding assistants
Aider with qwen2.5-coder:7b (via Ollama) writes most implementation code. Claude (claude.ai chat) wrote the scaffold, test suites and specs.