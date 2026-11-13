# OpenCode sessions: prompts, tokens, cost

Model: `openai.gpt-6-luna` on the UPB AI Gateway. Cost in USD as reported by OpenCode (`opencode export`).
Tokens and cost come from the JSON exports; sessions with only a Markdown export have prompts and tool calls only.

| Session | Who | Export | Prompts | Tool calls | Input tokens | Output (+ reasoning) | Cached read | Cost |
|---|---|---|---|---|---|---|---|---|
| 00 hello + 01 website (`ses_e3e3…`, 09.–10.11) | Al Fahad | `session_00_hello.md`, `session_01.md` | 1 + 2 | 2 + 37 | TODO (JSON export) | TODO | TODO | TODO |
| hello (`ses_e297…`, 13.11) | Raghvendra | `session-ses_e297_raghu.md`, `../hello-agent/session-log.json` | 6 | 12 | 2,048 | 722 (+1,073) | 70,098 | $0.0018 |
| 02 hello (`ses_e2a8…`, 13.11) | Rafee | `session_02_hello_rafee.md` / `.json` | 2 | 2 | 13,609 | 131 (+113) | 40,425 | $0.0019 |
| 03 agent went wrong (`ses_e2a7…`, 13.11) | Rafee | `session_03.md` / `.json` | 2 | 18 | 70,451 | 3,081 (+4,920) | 373,493 | $0.0148 |

Session 03: the agent added an external Font Awesome link (check "no external resources" failed, 23/24); we steered it back ("Stop: … use inline SVG … run check_site.py"). The session ended before the fix was applied, so the website in `site/` is the version from session 01.

What we observed: most tokens are re-sent context: the cached reads are 3× to 34× the new input, because every turn sends the whole conversation again.
