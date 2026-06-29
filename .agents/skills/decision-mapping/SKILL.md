---
name: decision-mapping
description: Turn a loose idea into a sequenced map of investigation tickets, then drive them to resolution one at a time.
disable-model-invocation: true
---

This skill is invoked when a loose idea requires more than one agent session to turn into a plan. It creates a stateful decision map in a markdown file, and drives the user through a sequence of tickets to resolve the open questions - which may require either prototyping, research or discussion.

## The Decision Map

The decision map is a single compact Markdown file, one per planning effort, git-tracked alongside the project. It is the canonical artifact — the **whole map is loaded as context into every session**, so it must stay compact.

Assets created during tickets should be linked to from the map, not duplicated within it.

### Structure

Numbered entries ("tickets"), each its own section keyed by its number:

```markdown
## #1: Relational Or Non-Relational Database?

Blocked by: #<ticket-number>, #<ticket-number>
Type: Research | Prototype | Grilling

### Question

<question-here>

### Answer

<answer-here>
```

Each ticket must be sized to one 100K token agent session.

## Ticket Types

There are three types of tickets:

- **Research**: Reading documentation, third-party API's, or local resources like knowledge bases. Creates a markdown summary as an asset. Use this when knowledge outside the current working directory is required.
- **Prototype**: Writing UI or logic code to test a hypothesis, or to explore a design space. Uses the /prototype skill. Creates a prototype as an asset. Use this when "how should it look" or "how should it behave" is the key question.
- **Grilling**: Conversation with the agent. Uses the /grilling and /domain-modeling skills. Asks one question at a time. The default case.

## Fog of war

The map is _deliberately_ incomplete beyond the frontier. Your job is to investigate the frontier, and to resolve tickets in order to push the frontier forward. Push back the fog of war, one node at a time.

At some point, the fog of war should have been pushed back far enough that the path to the finish line is clear. At that point, no more tickets will be required and the decision map can be considered 'done'.

## Invocation

There are two ways this skill can be invoked: **bootstrap** and **resume**.

### Bootstrap

User invokes with a loose idea.

1. Run a /grilling + /domain-modeling session to surface the open decisions. Ask one question at a time.
2. Write a new decision map — mostly fog, frontier identified, trivially-decidable entries resolved inline.
3. Stop. Map-building is one session's work; do not also resolve tickets.

### Resume

User invokes with a path to an existing map and a ticket number.

1. Load the **whole map** as context.
2. Run a session to resolve the ticket, invoking skills as needed. If in doubt, use `/grilling` and `/domain-modeling`.
3. Record what the session resolved in the ticket's body.
4. Add newly-discovered tickets (with correct `blocked_by` edges).
5. Stop.

If the decisions made invalidate other parts of the map, update or delete those nodes.

## Parallelism

The user may choose to run tickets in parallel, so expect other agents to make changes to the map.

## Skipping The Decision Map

Many times, the initial grilling will result in no fog of war. No unresolved tickets. Nothing to do, except implement.

In those situations, you should offer the user the chance to skip the decision map - since the decision map is only needed if multi-session decisions need to be made.

If they skip it, you should recommend either implementing directly or using `/to-prd` to schedule a multi-session implementation.
