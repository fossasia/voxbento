# Meeting reports

After a session, VoxBento can ask an AI service to read a room's stored
transcript and write meeting notes: a title, a one or two sentence overview,
and sections whose headings the AI chooses to fit that meeting (for example the
products, decisions or questions discussed), each with short points. Decisions
and follow-ups get their own section only when there were some. This is phase 1
of #1080: reports are written only when an event owner asks for one. Nothing
runs automatically when a session ends.

## Turning it on

Meeting reports are off for every event until an event owner turns them on:

1. Open the event's **API keys** page and find **Meeting Reports**.
2. Tick **Enable meeting reports for this event**.
3. Choose the **AI service** and, optionally, a **Model**. Leave the model empty
   to use the recommended one.

| Service | Recommended model | Key |
| --- | --- | --- |
| Local (Ollama) | `gemma3:4b` | none |
| OpenAI | `gpt-4o-mini` | the event's OpenAI translation key |
| Anthropic Claude | `claude-haiku-4-5-20251001` | the event's Anthropic key |
| Google Gemini | `gemini-3.8-flash` | the event's Gemini key |
| Groq | `openai/gpt-oss-120b` | the event's Groq key |
| OpenRouter | `meta-llama/llama-3.3-70b-instruct` | the event's OpenRouter key |

Cloud services reuse the encrypted translation keys already stored on the
event; no new secret is stored. Local (Ollama) sends the transcript only to
`MEETING_ANALYSIS_OLLAMA_BASE_URL` (default `http://localhost:11434`). If that
server doesn't answer `/api/chat` (HTTP 404), VoxBento tries its
OpenAI-compatible `/v1/chat/completions` instead, so LM Studio, vLLM or
llama.cpp servers also work.

Ollama requests ask for a context window of `MEETING_ANALYSIS_OLLAMA_NUM_CTX`
tokens (default 32768), enough for the longest transcript VoxBento sends plus
the report. The OpenAI-compatible fallback can't set this per request, so give
that server a context of at least the same size (for Ollama itself,
`OLLAMA_CONTEXT_LENGTH`), or the model will see only part of the transcript.

## Writing a report

Open the room and choose **View reports** (or go to
`/workspace/events/{event_id}/rooms/{room_id}/analysis/`). The page lists the
channels that have a stored transcript: the floor audio and each booth. Choose
one and select **Generate report**. The report is written in the background and
the page refreshes when it is ready.

Open a finished report to see it on its own page with two tabs:

- **Summary**: the overview and the sections, with an estimated reading time.
- **Transcript**: the transcript lines the report was written from, with their
  time from the start of the session. Lines stored after the report was written
  are not shown.

Each finished report can also be downloaded as Markdown.

- Only one report per room is written at a time.
- Very long sessions are cut to the first 60,000 characters of transcript; the
  report says when this happened.
- A report still marked as being written 30 minutes after it was requested
  (for example because the server restarted) is marked failed. Generate it
  again.

## Who can see what

| | Event owner / Admin | Room coordinator | Listeners |
| --- | :-: | :-: | :-: |
| Turn reports on and choose the service | ✓ | | |
| Generate a report | ✓ | | |
| Read and download reports | ✓ | ✓ | |

Reports are never shown on listener, embed or overlay pages.

## Safety

- The transcript is wrapped in a `<transcript>` block and the model is told to
  treat it as data; text inside a segment can't close that block.
- API keys are never logged, returned or included in error messages. Errors
  stored on a report only name the service and the HTTP status.
- Model output is stored as plain text and rendered escaped.

## Data

Migration `039_meeting_analysis.py` adds `events.meeting_analysis_enabled`,
`events.meeting_analysis_provider` and `events.meeting_analysis_model`, and the
`meeting_reports` table. Reports are deleted with their room or event.

## Not in phase 1

Automatic reports when a session ends, per-language report variants from the
translated segments, glossary terms in the prompt, and a Mission Control panel.
