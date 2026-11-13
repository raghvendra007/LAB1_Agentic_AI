"""All tools the agent can call. One file per tool.

Every tool file has the same 4 things:
    NAME         action name the model writes in its JSON
    ARGS         required arguments and their types, e.g. {"query": str}
    DESCRIPTION  one line for the system prompt
    run(args, state) -> (observation, short)
                 observation = full text sent back to the model
                 short       = one-line summary, used later when the step is summarized

Adding a tool = adding a file + one line below.
"""

from tools import finish, read_profiles, remember, reply, say, search_style_guide, write_site

TOOLS = {t.NAME: t for t in (say, reply, remember, search_style_guide, read_profiles, write_site, finish)}
