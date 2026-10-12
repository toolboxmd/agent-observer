# Reference judge: corrections

This is a reference prompt for an outside LLM judge that labels
corrections through Agent Observer. Observer runs no model. The judge runs
anywhere it can call `agent-observer`, for example as a contextmd job.
Copy the prompt and adapt it; Observer does not read this file.

The judge works in two passes: pass 1 labels each moment, and pass 2
groups the corrections. Use the same judge id in both passes, because a
verdict's key is its moment, behavior and judge.

## Pass 1: label

```text
You label moments where a person acted in a coding-agent session.
Behavior: correction. Judge id: <model>/correction-v1.

1. List your work:
   agent-observer moments --since <date> --until <date> --unjudged correction --json
2. Read moments in batches of about 10:
   agent-observer moment show <id> <id> ... --json
   Each moment has the person's full text (`text`), the agent activity
   before and after it, and the previous and next prompt. A rejection has
   no text; its `tool` names the refused tool call.
3. For each moment decide:
   - yes: the person tells the agent that something it did, said or was
     about to do is wrong, unwanted or not what they asked. This includes
     stopping it (an interrupt or a rejected tool call) when the context
     shows why, repeating an instruction the agent ignored, and complaints
     that the agent made the same mistake again.
   - no: a new request, an answer to the agent's question, an approval, a
     clarification of the person's own earlier words, or an interrupt
     followed by "continue" or by an unrelated request.
   - unclear: the context cannot tell, for example an interrupt with no
     later prompt and no visible reason.
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
   {"moment": "<id>", "behavior": "correction", "judge": "<judge id>", "verdict": "yes", "problem": "<line>"}
   {"moment": "<id>", "behavior": "correction", "judge": "<judge id>", "verdict": "no"}
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
   {"moment": "<id>", "behavior": "correction", "judge": "<judge id>", "verdict": "yes", "problem": "<line>", "group": "<group id>"}
```

## Report

```sh
agent-observer recurrence --behavior correction --since <date> --until <date>
```

The report counts the corrections, the problems, the problems that recur
in two or more sessions, and the repeats per group, with first and last
seen, sessions and hosts. It also shows how many moments are still
unjudged, by origin, and the hook and rule denials, which never count as
corrections.
