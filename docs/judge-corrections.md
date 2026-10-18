# Reference judge: corrections

This is a reference prompt for an outside LLM judge that labels
corrections through Agent Observer. Observer runs no model. The judge runs
anywhere it can call `agent-observer`, for example as a contextmd job.
Copy the prompt and adapt it; Observer does not read this file.

The judge works in two passes: pass 1 labels each intervention, and pass 2
groups the corrections. Use the same judge id in both passes, because a
verdict's key is its intervention, behavior and judge.

## Pass 1: label

```text
You label interventions where someone acted on a coding agent: a person, or
another agent (a parent, reviewer or planner writing to a child). The
intervention's `created_by` says which.
Behavior: correction. Judge id: <model>/correction-v1.

1. List your work:
   agent-observer interventions --since <date> --until <date> --unjudged correction --judge <judge id> --json
2. Read interventions in batches of about 10:
   agent-observer intervention show <id> <id> ... --json
   Each intervention has the person's full text (`text`) and whole turns around
   it: `before` (the turn it answers, opened by the previous prompt) and
   `after` (the agent's reply, closed by the next prompt). Add
   `--turns-before N --turns-after N` when one turn is not enough, and
   record the window you used in your judge id (for example
   `<model>/correction-v1@1x1`). A rejection has no text; its `tool`
   names the refused tool call.
3. For each intervention decide:
   - yes: the sender tells the agent that something it did, said or was
     about to do is wrong, unwanted or not what they asked. From another
     agent this includes review findings that the child must fix and a
     parent re-sending an instruction the child ignored. This includes
     stopping it (an interrupt or a rejected tool call) when the context
     shows why, repeating an instruction the agent ignored, and complaints
     that the agent made the same mistake again.
   - no: a new request or brief, an answer to the agent's question, an
     approval, a clarification of the sender's own earlier words, a
     status or completion notice, or an interrupt followed by "continue"
     or by an unrelated request.
   - unclear: the context cannot tell, for example an interrupt with no
     later prompt and no visible reason.
   An intervention with `opening: true` came before anything the agent did in
   its session (a first prompt or a child's brief), so it cannot correct
   that agent. Read only its text (`--turns-before 0 --turns-after 0`):
   it is no unless the text itself says an earlier agent or attempt got
   something wrong (a retry brief naming the failure), which is yes.
   `opening: null` means unknown, including sessions with no recorded
   assistant messages or tool calls. Read the available context and judge
   normally; null is not evidence of a first prompt or a reason to answer no.
   Only prompts can have `opening: true`; interrupts and rejections are false.
   Not understanding the agent is yes only when the person says the
   explanation itself failed ("I don't understand a word", "talk
   simpler", "still don't get it" after a re-explanation). A first plain
   "i don't get it" that asks for clarification is no.
   For yes, write `problem`: one line of at most 200 characters naming the
   agent's behavior in general terms that would match the same mistake in
   another session ("Started many agents without approval"), not the
   session's details ("ran 74 judges for step 0").
4. Store each batch as JSON lines:
   agent-observer verdict add --json < batch.jsonl
   {"intervention": "<id>", "behavior": "correction", "judge": "<judge id>", "verdict": "yes", "problem": "<line>"}
   {"intervention": "<id>", "behavior": "correction", "judge": "<judge id>", "verdict": "no"}
   Observer stores a batch only when every line is valid. Fix the lines
   named in `errors` and send the batch again.
```

## Pass 2: group

```text
Group the corrections that are the same underlying problem: the same
agent behavior that one stored rule or fact could prevent. Different
symptoms of one behavior belong together; the same words about different
behaviors do not.

1. Read every yes verdict and its problem line:
   agent-observer verdict list --behavior correction --judge <judge id> --verdict yes --json
   and the groups that already exist:
   agent-observer verdict groups --behavior correction --judge <judge id> --json
   Reuse an existing group id when it fits.
2. Give each new group a short kebab-case id, for example
   `parallel-agents-without-approval`.
3. Add each yes verdict again with its group:
   {"intervention": "<id>", "behavior": "correction", "judge": "<judge id>", "verdict": "yes", "problem": "<line>", "group": "<group id>"}
```

## Report

```sh
agent-observer recurrence --behavior correction --since <date> --until <date>
```

The report counts the corrections, the problems, the problems that recur
in two or more sessions, and the repeats per group, with first and last
seen, sessions and hosts. It also shows how many interventions are still
unjudged, by `created_by`, and the hook and rule denials, which never count as
corrections.
